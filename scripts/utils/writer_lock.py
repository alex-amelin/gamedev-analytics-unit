"""Дисциплина одного писателя — эксклюзивный ``.writer.lock`` на уровне хранилища.

Контекст-менеджер :func:`writer_lock` берёт **неблокирующий** эксклюзивный лок на файле
``{GDAU_DATA_ROOT}/.writer.lock`` перед любой записью в хранилище и **гарантированно
освобождает** его в ``finally`` на выходе из блока ``with`` (успех, исключение,
прерывание). Лок занят живым писателем → немедленный fail-fast
(:class:`WriterLockHeldError`) без ожидания и без записи (FR-15). Залипший лок от
**умершего** писателя невозможен: лок ведёт ядро ОС и снимает его при смерти процесса
(крэш/``kill -9``), поэтому новый писатель просто берёт лок успешно — никакой проверки
живости PID, никакого ручного reclaim (решение Шефа: вариант A — OS advisory-lock).

**Это последний страж целостности перед оркестратором.** DuckDB — single-writer движок,
а сырьё пишется через temp→``os.replace``: два конкурентных писателя портят базу. Лок —
дешёвая гарантия «ровно один писатель в каждый момент» без серверных процессов.

**Границы (что делает ДРУГОЙ компонент, не этот).** Путь лок-файла резолвит
:func:`scripts.utils.paths.get_writer_lock_path` (2.1); запись Parquet-партиций — 2.2;
жёсткую сверку строк — 2.3; мету ``load_state`` — 2.4; типизированные view — 2.6;
**оркестрацию и scope лока** (один захват на весь прогон обновления) — p81 (2.7). Читатели
(MCP-чтение 3.1, view 2.6) лок **НЕ берут** — read-канал просто не зовёт ``writer_lock``.
Модуль знает **только** про файл-лок: он **НЕ** открывает ``gdau.duckdb``, **НЕ** пишет
данные и **НЕ** зависит от ``database_manager``/``parquet_store``/``load_state``/``duckdb``
(независимый примитив — чисто тестируется на ``tmp_path`` без сети и без рабочей БД).

**Кросс-платформенность под ``sys.platform`` (НЕ ``os.name``).** ``fcntl`` есть только на
POSIX, ``msvcrt`` — только на Windows; CI гоняет обе ОС с ``mypy --strict``. mypy сужает
типы по ``sys.platform`` (и ``sys.version_info``), но НЕ по ``os.name`` — поэтому условный
импорт и все обращения к платформенному модулю держатся под одним и тем же
``sys.platform``-гардом: на чужой ОС ветка считается недостижимой и strict не падает на
«module not found».
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path

from scripts.utils.paths import get_writer_lock_path

if sys.platform == "win32":  # mypy сужает по sys.platform (НЕ os.name) — риск №8
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

__all__ = ["WriterLockError", "WriterLockHeldError", "writer_lock"]


class WriterLockError(RuntimeError):
    """Инцидент окружения/целостности writer-лока (обёртка сырого ``OSError``).

    Наследует :class:`RuntimeError` (а не :class:`ValueError`): это сбой окружения/ОС, не
    ошибка аргумента. Наружу **никогда** не выпускается сырой ``OSError`` — он
    заворачивается в этот класс (или его подкласс) с контекстом (путь лок-файла).
    """


class WriterLockHeldError(WriterLockError):
    """Лок удержан **живым** писателем → fail-fast (AC #2), без ожидания и без записи.

    Выделенный тип даёт оркестратору p81 (2.7) точечный ``except`` «уже идёт запись» —
    отличить «хранилище занято другим писателем» от прочих ошибок окружения лока.
    """


@contextlib.contextmanager
def writer_lock(*, lock_path: Path | None = None) -> Iterator[None]:
    """Контекст-менеджер эксклюзивного ``.writer.lock`` на уровне хранилища (AC #1, #2, #4, #5).

    На входе берёт **неблокирующий** эксклюзивный лок ядра: занят живым писателем →
    немедленный :class:`WriterLockHeldError` (AC #2, fail-fast, без ожидания/записи). На
    выходе из блока ``with`` лок освобождается в ``finally`` при **любом** исходе — успех,
    исключение, прерывание (AC #4). Залипший лок умершего писателя невозможен: ядро снимает
    advisory-лок при смерти процесса, поэтому остаточный файл сам по себе не блокирует —
    новый писатель берёт лок успешно (AC #5 по исходу, без проверки PID).

    **Не реентерабелен:** повторный захват того же ``lock_path`` тем же процессом (вложенный
    ``with`` на тот же путь) конфликтует сам с собой и даёт :class:`WriterLockHeldError` —
    лок привязан к открытому дескриптору, не к процессу. p81 (2.7) берёт лок **один раз** на
    весь прогон обновления и не вкладывает захваты.

    :param lock_path: инъектируемый шов — путь к файлу-замку. ``None`` (прод-путь) →
        :func:`scripts.utils.paths.get_writer_lock_path` (= ``{GDAU_DATA_ROOT}/.writer.lock``);
        резолвер fail-loud-ит при незаданном/несуществующем корне хранилища ДО любого
        ``os.open`` — мусорный лок-файл в dev-репо не создаётся (риск №6). Тесты дают
        ``tmp_path / ".writer.lock"`` (без сети и без рабочей БД).
    :raises ValueError: битый корень хранилища (наследуется из ``paths``, до ``os.open``).
    :raises WriterLockHeldError: лок удержан живым писателем (AC #2).
    :raises WriterLockError: иной ОС-сбой открытия файла-замка (обёртка сырого ``OSError``).
    """
    # Резолюция пути fail-loud-ит при битом корне ДО os.open (никакого side-effect, риск №6).
    path = lock_path if lock_path is not None else get_writer_lock_path()

    # O_CREAT: файл-замок создаётся при отсутствии; mkdir НЕ делаем — корень провалидирован
    # paths (его создаёт init 4.3), лок лежит прямо под корнем (риск №6).
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise WriterLockError(
            f"Не удалось открыть файл-замок писателя {path}: {exc}"
        ) from exc

    try:
        _acquire_nonblocking(fd, path)
    except BaseException:
        # Захват не удался (лок занят / иной сбой) — не утечь дескриптором. suppress на
        # close, чтобы редкий сбой закрытия не маскировал исходный WriterLockHeldError.
        with contextlib.suppress(OSError):
            os.close(fd)
        raise

    logger.info("Захвачен .writer.lock: %s (pid %d)", path, os.getpid())
    try:
        yield
    finally:
        _release(fd, path)


def _acquire_nonblocking(fd: int, path: Path) -> None:
    """Взять неблокирующий эксклюзивный лок на ``fd`` или fail-fast (AC #1, #2).

    Лок занят → системный вызов сразу бросает ``OSError`` → :class:`WriterLockHeldError`
    (немедленно, без ожидания/записи). Платформенный вызов под ``sys.platform``-гардом
    (риск №8): POSIX — ``fcntl.flock(LOCK_EX | LOCK_NB)``; Windows — ``msvcrt.locking
    (LK_NBLCK, 1)`` (1 байт от позиции 0 — после ``os.open`` позиция = 0). Остаточный лок
    умершего писателя ядро уже сняло → вызов проходит успешно (AC #5, без reclaim-кода).
    """
    if sys.platform == "win32":
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise WriterLockHeldError(
                f"Хранилище занято другим писателем (лок {path}). Дождись завершения "
                f"записи или проверь, не висит ли процесс."
            ) from exc
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WriterLockHeldError(
                f"Хранилище занято другим писателем (лок {path}). Дождись завершения "
                f"записи или проверь, не висит ли процесс."
            ) from exc


def _release(fd: int, path: Path) -> None:
    """Снять лок и закрыть ``fd`` (AC #4). Симметрично захвату, под ``sys.platform``-гардом.

    ``OSError`` из шага снятия/закрытия **не маскирует** исходное исключение тела ``with`` (мы
    его не пробрасываем), но и **не глотается молча**: подавленный сбой пишется WARNING —
    честный аудит-след для integrity-critical примитива. ``os.close`` само снимает лок,
    поэтому лок считается освобождённым тогда и только тогда, когда закрытие дескриптора
    прошло: именно по нему пишется INFO «Освобождён», а не безусловно. Лок-файл **не удаляем**
    (пустой ``.writer.lock`` под корнем безвреден и в ``.gitignore``; удаление вносило бы гонки).
    """
    try:
        if sys.platform == "win32":
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError as exc:
                logger.warning(
                    "Сбой снятия .writer.lock %s: %s (лок снимет закрытие fd)", path, exc
                )
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                logger.warning(
                    "Сбой снятия .writer.lock %s: %s (лок снимет закрытие fd)", path, exc
                )
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            logger.warning(
                "Не удалось закрыть дескриптор .writer.lock %s: %s — лок может остаться "
                "удержанным",
                path,
                exc,
            )
        else:
            logger.info("Освобождён .writer.lock: %s", path)
