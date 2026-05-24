"""Offline-тесты дисциплины одного писателя — ``.writer.lock`` (история 2.5).

Покрывают именно дисциплину целостности, а не happy-path: лок берётся на входе (AC #1),
второй конкурентный писатель падает **немедленно** без ожидания/записи (AC #2), лок
освобождается в ``finally`` и на успехе, и на исключении (AC #4), остаточный лок-файл
умершего писателя сам по себе не блокирует нового (AC #5), битый корень хранилища fail-loud
ДО любого ``os.open`` без побочных эффектов (риск №6), и границы независимости модуля:
read-канал (``database_manager``) лок НЕ берёт (AC #3) + сам модуль не тянет тяжёлый
аналитический стек / directaiq-инфру / рабочую БД (риск №7) — всё по реальным import-узлам
через ``ast``, не по подстроке.

Без сети, без DuckDB, без партиций. Кросс-платформенно (``tmp_path``/``pathlib``): CI гоняет
ubuntu + windows, лок-семантика обязана работать на обеих. Конфликт двух захватов
воспроизводится **внутри одного процесса**: каждый ``writer_lock`` делает свой ``os.open`` →
``fcntl.flock`` конфликтует между разными open-file-description одного файла даже в одном
процессе, а ``msvcrt.locking`` той же области из второго дескриптора → ``ERROR_LOCK_VIOLATION``.

Live-набор осознанно отсутствует: ``writer_lock`` в сеть не ходит — только файловый лок
([[realapi-smoke-tests]] — opt-in live только для внешнего API Logs API), как 2.1/2.2/2.3/2.4.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.utils.env_reader import DATA_ROOT_ENV
from scripts.utils.writer_lock import (
    WriterLockError,
    WriterLockHeldError,
    writer_lock,
)


def _imported_modules(module_filename: str) -> set[str]:
    """Реальные import-узлы модуля по корню имени (через ``ast``, не по подстроке).

    Возвращает множество имён из ``import x`` / ``from x import y`` (и модуль, и
    импортируемые имена), чтобы проверять запреты по корню (``name.split('.')[0]``).
    """
    source = Path(module_filename).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)
    return imported


# --- AC #1: лок берётся на входе -------------------------------------------------------


def test_lock_acquired_creates_file_inside_block(tmp_path: Path) -> None:
    """Вход в ``with writer_lock(lock_path=p)`` создаёт файл-замок; внутри блока он есть (AC #1)."""
    lock = tmp_path / ".writer.lock"
    assert not lock.exists()

    with writer_lock(lock_path=lock):
        assert lock.is_file()  # лок взят, файл-замок на месте


def test_lock_acquired_via_prod_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прод-путь: ``lock_path=None`` → ``get_writer_lock_path()`` = ``{root}/.writer.lock`` (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with writer_lock():
        # Путь резолвится под корнем хранилища, не в dev-репо.
        assert (tmp_path / ".writer.lock").is_file()


# --- AC #2: fail-fast при удержании живым писателем ------------------------------------


def test_second_writer_fails_fast_when_held(tmp_path: Path) -> None:
    """Лок удержан → вложенный захват → немедленный WriterLockHeldError, без ожидания (AC #2)."""
    lock = tmp_path / ".writer.lock"

    with writer_lock(lock_path=lock):
        # Второй захват того же лока (свой os.open → конфликт open-file-description /
        # ERROR_LOCK_VIOLATION) обязан упасть немедленно, не вставая в очередь.
        with pytest.raises(WriterLockHeldError) as exc_info:
            with writer_lock(lock_path=lock):
                pytest.fail("вложенный захват не должен был пройти — лок удержан")

    # Сообщение fail-loud содержит путь лок-файла (подстрокой, без regex — Windows-пути).
    assert str(lock) in str(exc_info.value)


def test_writer_lock_held_error_is_writer_lock_error() -> None:
    """WriterLockHeldError — подкласс WriterLockError (точечный except для p81 2.7; AC #2)."""
    assert issubclass(WriterLockHeldError, WriterLockError)
    assert issubclass(WriterLockError, RuntimeError)


# --- AC #4: освобождение в finally на успехе И на исключении ----------------------------


def test_lock_released_after_normal_exit(tmp_path: Path) -> None:
    """После штатного выхода из ``with`` повторный захват успешен — лок отпущен (AC #4)."""
    lock = tmp_path / ".writer.lock"

    with writer_lock(lock_path=lock):
        pass
    # Лок отпущен в finally — следующий писатель берёт его без ошибки.
    with writer_lock(lock_path=lock):
        assert lock.is_file()


def test_lock_released_after_exception_in_body(tmp_path: Path) -> None:
    """Исключение в теле блока → лок всё равно отпущен в finally, не «застрял» (AC #4)."""
    lock = tmp_path / ".writer.lock"

    with pytest.raises(RuntimeError, match="смоделированный сбой"):
        with writer_lock(lock_path=lock):
            raise RuntimeError("смоделированный сбой записи")

    # Несмотря на исключение в теле — лок свободен, новый захват проходит.
    with writer_lock(lock_path=lock):
        assert lock.is_file()


def test_lock_released_after_keyboard_interrupt_in_body(tmp_path: Path) -> None:
    """Прерывание (KeyboardInterrupt в теле) → лок отпущен в finally; буква AC #4 «прерывание».

    @contextmanager пробрасывает BaseException в точку yield, поэтому finally с _release
    срабатывает и на KeyboardInterrupt/SIGINT, не только на обычных Exception. Жёсткий
    SIGKILL finally не отработал бы — там лок снимает ядро (вариант A, двойная гарантия).
    """
    lock = tmp_path / ".writer.lock"

    with pytest.raises(KeyboardInterrupt):
        with writer_lock(lock_path=lock):
            raise KeyboardInterrupt  # имитация Ctrl-C / SIGINT во время записи

    # Лок отпущен несмотря на прерывание — новый захват проходит.
    with writer_lock(lock_path=lock):
        assert lock.is_file()


# --- os.open сбой → WriterLockError (обёртка сырого OSError, не contention) -------------


def test_os_open_failure_wrapped_in_writer_lock_error(tmp_path: Path) -> None:
    """Сбой os.open (путь = существующий каталог) → WriterLockError, не сырой OSError.

    Путь-каталог нельзя открыть на запись (os.open O_RDWR): POSIX → IsADirectoryError,
    Windows → PermissionError — оба подклассы OSError, обязаны завернуться в WriterLockError
    (а НЕ в WriterLockHeldError: это не «занято писателем», а сбой окружения). Доказывает
    гарантию «никогда сырой OSError наружу».
    """
    lock_dir = tmp_path / "lock_as_dir"
    lock_dir.mkdir()

    with pytest.raises(WriterLockError) as exc_info:
        with writer_lock(lock_path=lock_dir):
            pytest.fail("захват не должен был пройти — путь это каталог")

    assert not isinstance(exc_info.value, WriterLockHeldError)  # не contention
    assert str(lock_dir) in str(exc_info.value)


# --- AC #5: остаточный лок-файл умершего писателя не блокирует --------------------------


def test_stale_lock_file_does_not_block(tmp_path: Path) -> None:
    """Заранее лежащий, никем не удержанный лок-файл не мешает новому писателю (AC #5).

    Эмуляция остатка после умершего писателя: файл на диске есть, но ядро лок мёртвого
    процесса уже сняло. Доказывает: остаточный ``.writer.lock`` сам по себе не блокирует
    (в варианте A reclaim-кода нет — лок ведёт ядро, не содержимое файла).

    Полноценную «лок держал процесс, который умер» проверку дал бы subprocess-тест (взять
    лок в дочернем процессе, убить, затем взять из родителя), но кросс-платформенный
    subprocess-тест тяжёл и флака-прон — базовое покрытие через остаточный незанятый файл
    достаточно (см. Dev Agent Record: subprocess-вариант помечен как возможное усиление).
    """
    lock = tmp_path / ".writer.lock"
    lock.write_text("17324\n", encoding="utf-8")  # «остаток» от прошлого писателя
    assert lock.is_file()

    with writer_lock(lock_path=lock):  # не падает, не виснет
        assert lock.is_file()


# --- Битый корень: fail-loud ДО os.open, без побочных эффектов (риск №6) ----------------


def test_missing_root_fails_loud_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Битый корень (GDAU_DATA_ROOT не задан) → ValueError ДО os.open; ни одного файла (риск №6)."""
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)

    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        with writer_lock():  # lock_path=None → get_writer_lock_path() fail-loud
            pytest.fail("захват не должен был начаться при битом корне")

    # Резолюция падает ДО построения пути/os.open — ни одного лок-файла не создано.
    assert list(tmp_path.glob("*.writer.lock")) == []
    assert list(tmp_path.glob(".writer.lock")) == []


# --- AC #3 (граница): читатели лок НЕ берут — негативная гарантия по ast ----------------


def test_database_manager_does_not_import_writer_lock() -> None:
    """Read-канал (database_manager 2.1) НЕ импортирует writer_lock — читатели лок не берут (AC #3).

    Это граница истории, а не дыра покрытия: интеграционная проверка «MCP-чтение не
    блокируется во время записи» принадлежит 3.1 (там появляется MCP-канал). Здесь
    фиксируем по реальным import-узлам, что read-компонент не тянет writer-лок.
    """
    import scripts.utils.database_manager as db_mod

    imported = _imported_modules(db_mod.__file__)  # type: ignore[arg-type]
    offenders = {n for n in imported if "writer_lock" in n}
    assert not offenders, f"database_manager не должен импортировать writer_lock: {offenders}"


# --- Анти-зависимость: только файл-лок, без тяжёлого стека и рабочей БД (риск №7) -------


def test_no_heavy_or_directaiq_infra_imported() -> None:
    """Нет import pandas/polars/numpy/pyarrow и directaiq-инфры config_manager/base_script."""
    import scripts.utils.writer_lock as mod

    imported = _imported_modules(mod.__file__)  # type: ignore[arg-type]
    forbidden = {"pandas", "polars", "numpy", "pyarrow", "config_manager", "base_script"}
    offenders = {n for n in imported if n.split(".")[0] in forbidden}
    assert not offenders, f"запрещённые импорты в writer_lock: {offenders}"


def test_writer_lock_is_independent_primitive() -> None:
    """Модуль независим: НЕ импортирует duckdb/database_manager/parquet_store/load_state (риск №7).

    writer_lock знает только про файл-лок — он оркеструется снаружи (p81 берёт лок, *затем*
    открывает БД и пишет). Так примитив чисто тестируется на tmp_path без сети и без DuckDB.
    """
    import scripts.utils.writer_lock as mod

    imported = _imported_modules(mod.__file__)  # type: ignore[arg-type]
    forbidden_deps = {
        "duckdb",
        "scripts.utils.database_manager",
        "scripts.utils.parquet_store",
        "scripts.utils.load_state",
    }
    offenders = imported & forbidden_deps
    # Доп. защита: и по корню имени (на случай `from scripts.utils import database_manager`).
    offenders |= {n for n in imported if n in {"database_manager", "parquet_store", "load_state"}}
    assert not offenders, f"writer_lock не должен зависеть от записи/БД: {offenders}"
