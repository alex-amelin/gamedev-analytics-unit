"""Оркестратор разворачивания per-game хранилища — console-команда ``gdau-init``.

Роль. Одной командой ``gdau-init {game}`` поднимает рабочее пространство одной игры
рядом с dev-репо (каталог-сосед ``../{game}``) за один проход: проверка свободного имени
→ копирование шаблона хранилища (4.2) → симлинки на инфру dev-репо по контракту + preflight
(4.1) → генерация ``.env`` с корнем хранилища → ``uv sync --frozen`` → создание
``gdau.duckdb`` + типизированные view'ы из каталога (2.6) → ``git init`` + initial commit.
На выходе хранилище готово к первой выгрузке — владельцу остаётся вписать токен/счётчик в
``.env`` (FR-19).

Границы. Это **тонкий оркестратор**: механику он НЕ дублирует, а вызывает готовые примитивы —
``scaffold.copy_storage_template`` (4.2), ``symlinks.preflight_symlink_capability``/
``create_symlinks`` (4.1), ``DatabaseManager.connection`` (2.1), ``views.create_views`` (2.6),
``writer_lock`` (2.5). Оркестратор добавляет ровно то, что есть только на уровне сборки:
валидацию имени игры, резолюцию пути ``../{game}`` от корня dev-репо (не от cwd), генерацию
``.env``, ``uv sync``, ``git init`` и **полный откат всего хранилища** при сбое любого шага —
границу, которую примитивы 4.1/4.2 намеренно НЕ делают.

Расхождения с directaiq ``init_project.nu`` (осознанные, трассируемость): Python вместо
nushell + bash-обёрток (кросс-платформенно Win↔Linux, без ``activate.sh``); схема = view'ы из
каталога вместо системы миграций (``migrate.py``/``SKIP_AUTO_MIGRATE`` — не тянем); per-storage
``.venv`` через ``uv sync --frozen`` вместо shared-venv; относительные цели симлинков; **полный
откат хранилища** при сбое (directaiq оставлял частичный мусор). В сеть оркестратор не ходит
(Logs API в init нет), TSV не парсит, данные/партиции не пишет (только пустую БД + view'ы).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from scripts.init.scaffold import StorageTemplateError, copy_storage_template
from scripts.init.symlinks import (
    SymlinkContractError,
    SymlinkError,
    SymlinkPreflightError,
    create_symlinks,
    preflight_symlink_capability,
)
from scripts.utils import env_reader
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.views import create_views
from scripts.utils.writer_lock import writer_lock

logger = logging.getLogger(__name__)

__all__ = ["StorageInitError", "init_storage", "main"]

# Зарезервированные Windows-имена (case-insensitive): NAME_PATTERN их не отсекает (`CON`
# матчит `[A-Za-z0-9_-]+`) — нужен отдельный набор. Имя игры = имя каталога ФС обеих ОС.
RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Строгий шаблон имени игры (компилируется один раз): первый символ — буква/цифра, далее
# буквы/цифры/`_`/`-`, длина 1–64. Отсекает разделители пути, пробелы, спецсимволы,
# ведущую точку. `fullmatch` (не `match`) — иначе `$` пропустил бы хвостовой `\n`.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Потолок ожидания `uv sync --frozen` (сек) — против вечного зависания на сетевом резолве.
UV_SYNC_TIMEOUT = 300


class StorageInitError(RuntimeError):
    """Сбой оркестрации init: имя занято/невалидное либо сбой шага разворота.

    Наследует :class:`RuntimeError` — инцидент окружения/ввода, не дефект данных. Сырьё
    (``OSError``/``subprocess``-сбой/``duckdb``-ошибка) оборачивается сюда с путём/контекстом;
    «голое» stdlib-исключение наружу не выпускаем (паттерн ревью 2.1/4.1/4.2).
    """


class CommandRunner(Protocol):
    """Шов запуска внешних команд (``git``/``uv``) — для инъекции фейка в тестах.

    Совпадает по форме с нужной частью :func:`subprocess.run`: позиционный ``args`` +
    ключевые ``cwd``/``timeout``, возврат :class:`subprocess.CompletedProcess`. Прод-реализация
    — :func:`_default_runner`; тест подменяет на фейк (без сети/реальных процессов).
    """

    def __call__(
        self, args: list[str], *, cwd: Path, timeout: float | None = ...
    ) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    args: list[str], *, cwd: Path, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Прод-раннер: ``subprocess.run`` без shell, с захватом вывода (кросс-платформенно, AC #5).

    ``check=False`` + ручная проверка ``returncode`` → понятная ошибка вместо
    ``CalledProcessError``-трейсбека. ``text=True`` — stdout/stderr строками для диагностики.
    """
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _validate_game_name(name: str) -> str:
    """Проверить имя игры строгим шаблоном ДО любых действий (AC #7). Вернуть очищенное имя.

    Отвергает fail-loud: пустое/пробельное; содержащее разделители пути (``/``/``\\``/``os.sep``/
    ``os.altsep``) или ``..`` (traversal); не матчащее :data:`NAME_PATTERN` (ведущая точка/пробел/
    спецсимвол/длина > 64); зарезервированное Windows-имя (case-insensitive). Имя — это и имя
    каталога-соседа, и (через симлинки/``.gitignore``) часть путей → строгость защищает ФС обеих ОС.
    """
    candidate = name.strip()
    if not candidate:
        raise StorageInitError(
            "Имя игры пустое — укажи непустое имя (латиница/цифры/`_`/`-`, до 64 символов)."
        )
    separators = [sep for sep in (os.sep, os.altsep, "/", "\\") if sep]
    if any(sep in candidate for sep in separators) or ".." in candidate:
        raise StorageInitError(
            f"Имя игры не должно содержать разделителей пути или '..': {name!r} "
            f"(хранилище — простой каталог-сосед dev-репо)."
        )
    if not NAME_PATTERN.fullmatch(candidate):
        raise StorageInitError(
            f"Недопустимое имя игры: {name!r}. Разрешены латинские буквы, цифры, `_` и `-`; "
            f"первый символ — буква или цифра; длина 1–64 (без пробелов/спецсимволов/ведущей точки)."
        )
    if candidate.upper() in RESERVED_WINDOWS_NAMES:
        raise StorageInitError(
            f"Имя игры {name!r} зарезервировано в Windows (CON/PRN/AUX/NUL/COM1–9/LPT1–9) — "
            f"выбери другое."
        )
    return candidate


def _resolve_dev_repo_root() -> Path:
    """Корень dev-репо: ``Path(__file__).resolve().parents[2]`` (D2; сквозь симлинк хранилища).

    ``init_project.py → init → scripts → корень dev-репо``. ``.resolve()`` проходит сквозь
    симлинк (хранилище видит ``scripts`` симлинком на dev-репо), как ``catalog.DEFAULT_CATALOG_PATH``
    / ``symlinks.DEFAULT_CONTRACT_PATH``. НЕ от ``os.getcwd()`` — иначе запуск из произвольного
    каталога увёл бы хранилище не туда (AC #11).
    """
    return Path(__file__).resolve().parents[2]


def _resolve_storage_root(
    game: str, dev_repo_root: Path, storage_parent: Path | None
) -> Path:
    """Путь хранилища ``{storage_parent}/{game}`` — чистая резолюция (AC #11, D2).

    ``storage_parent=None`` → ``dev_repo_root.parent`` (хранилище — сосед dev-репо, ``../{game}``).
    Никакого ``os.getcwd()``: при разных cwd результат один и тот же (тестируется детерминированно).
    """
    parent = storage_parent if storage_parent is not None else dev_repo_root.parent
    return parent / game


def _preflight_environment() -> None:
    """Проверить наличие ``git`` и ``uv`` в PATH ДО создания хранилища (AC #9).

    Нет бинаря → :class:`StorageInitError` с инструкцией (откат не нужен — хранилища ещё нет).
    При штатном ``uv run gdau-init`` ``uv`` заведомо есть; проверка — для robustness.
    """
    if shutil.which("git") is None:
        raise StorageInitError(
            "git не найден в PATH. Установи git и повтори gdau-init "
            "(хранилищу игры нужен собственный git-репозиторий)."
        )
    if shutil.which("uv") is None:
        raise StorageInitError(
            "uv не найден в PATH. Установи uv и повтори gdau-init "
            "(окружение хранилища ставится через `uv sync`)."
        )


def _write_env(storage_root: Path) -> None:
    """Сгенерировать ``.env`` из ``.env.example`` + строка ``GDAU_DATA_ROOT`` (AC #1, #3).

    Берёт скопированный шаблоном ``.env.example`` и пишет его в ``.env``, дописывая активную
    строку ``GDAU_DATA_ROOT={abs storage_root}`` (резолвер ``paths`` требует абсолютный путь;
    закомментированный плейсхолдер в образце игнорируется). Токен/счётчик остаются пустыми —
    владелец вписывает после init (AC #3). Содержимое ``.env`` НЕ логируется (NFR-5); имя
    переменной — из :data:`env_reader.DATA_ROOT_ENV` (не литерал). ``OSError`` →
    :class:`StorageInitError` (откат у :func:`init_storage`).
    """
    example = storage_root / ".env.example"
    env_path = storage_root / ".env"
    try:
        base = example.read_text(encoding="utf-8")
        if base and not base.endswith("\n"):
            base += "\n"
        env_path.write_text(
            f"{base}{env_reader.DATA_ROOT_ENV}={storage_root}\n", encoding="utf-8"
        )
    except OSError as exc:
        raise StorageInitError(
            f"Не удалось сгенерировать .env в {storage_root}: {exc}"
        ) from exc


def _uv_sync(storage_root: Path, run: CommandRunner) -> None:
    """``uv sync --frozen`` в хранилище — поставить ``.venv`` строго по локу (AC #1, #9).

    ``--frozen`` обязателен: ``uv.lock`` приходит симлинком на dev-репо → без ``--frozen`` uv
    попытался бы перезаписать лок сквозь симлинк (D8/D11). Ненулевой код / таймаут / нет бинаря
    → :class:`StorageInitError` (откат у :func:`init_storage`); stderr захватывается для диагностики.
    """
    try:
        result = run(
            ["uv", "sync", "--frozen"], cwd=storage_root, timeout=UV_SYNC_TIMEOUT
        )
    except FileNotFoundError as exc:  # preflight which() ловит раньше; подстраховка
        raise StorageInitError(f"uv не найден при установке окружения: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StorageInitError(
            f"`uv sync --frozen` превысил таймаут {UV_SYNC_TIMEOUT}s в {storage_root}."
        ) from exc
    except OSError as exc:  # PermissionError / WinError 740 и пр. — не «голый» наружу
        raise StorageInitError(
            f"Не удалось запустить `uv sync --frozen` в {storage_root}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise StorageInitError(
            f"`uv sync --frozen` завершился с кодом {result.returncode} в {storage_root}.\n"
            f"{(result.stderr or '').strip()}"
        )


def _create_database(storage_root: Path) -> None:
    """Создать ``gdau.duckdb`` + типизированные view'ы из каталога под ``.writer.lock`` (AC #1, #14).

    Инъекция корня: ``paths`` читает ``GDAU_DATA_ROOT`` только из ``os.environ`` (нет
    шва-параметра) → выставляем его на ``storage_root`` на время шага и **восстанавливаем после**
    (чище, чем оставлять процесс-окружение мутированным; не пачкает остальной процесс/тесты).
    Затем под :func:`writer_lock` (DDL ``CREATE OR REPLACE VIEW`` пишет в каталог БД → запись
    только под локом, как p81 2.7): write-conn создаёт файл БД, :func:`create_views` строит
    пустые типизированные view'ы (партиций ещё нет → ``has_partitions=False``, AC #14).

    Любой сбой (битый каталог/ошибка DuckDB/лок занят) → :class:`StorageInitError` (откат у
    :func:`init_storage`). ``duckdb`` напрямую НЕ импортируем (анти-зависимость): ``DatabaseManager``
    оборачивает ошибки connect в ``RuntimeError``, но ``conn.execute(ddl)`` может бросить
    ``duckdb.Error`` (потомок ``Exception``, не ``RuntimeError``) → ловим ``Exception`` узко в
    этом шаге (его единственная задача — создать БД; любой сбой здесь = откат).
    """
    previous = os.environ.get(env_reader.DATA_ROOT_ENV)
    os.environ[env_reader.DATA_ROOT_ENV] = str(storage_root)
    try:
        with writer_lock():
            with DatabaseManager.connection(read_only=False) as conn:
                create_views(conn)
    except Exception as exc:
        raise StorageInitError(
            f"Не удалось создать gdau.duckdb + view'ы в {storage_root}: {exc}"
        ) from exc
    finally:
        if previous is None:
            os.environ.pop(env_reader.DATA_ROOT_ENV, None)
        else:
            os.environ[env_reader.DATA_ROOT_ENV] = previous


def _run_git(
    args: list[str], cwd: Path, run: CommandRunner
) -> subprocess.CompletedProcess[str]:
    """Запустить git-команду; ненулевой код → :class:`StorageInitError` со stderr."""
    result = run(args, cwd=cwd)
    if result.returncode != 0:
        raise StorageInitError(
            f"git завершился с кодом {result.returncode}: {' '.join(args)}\n"
            f"{(result.stderr or '').strip()}"
        )
    return result


def _git_init_commit(storage_root: Path, game: str, run: CommandRunner) -> None:
    """``git init`` + initial commit в хранилище, ``.env`` исключён (AC #1, #4, #8, #13).

    ``cwd=storage_root`` — хранилище сосед dev-репо (D2), ``git init`` создаёт независимый
    ``storage/.git`` (AC #8 — изоляция; вложенность в чужой репо git разруливает сам). Уже есть
    ``.git`` (resume) → пропускаем ``git init``, но add+commit идемпотентно. ``.env`` исключаем
    из индекса явным ``git reset -- .env`` (AC #4, пояс-и-подтяжки поверх ``.gitignore`` шаблона).
    Коммитим только при непустом индексе (AC #13: 4 файла шаблона минус ``.env`` → непусто). Сбой
    git → :class:`StorageInitError` (откат у :func:`init_storage`).
    """
    git_dir = storage_root / ".git"
    try:
        if not git_dir.exists():  # resume: уже репо → не повторяем init (AC #8)
            _run_git(["git", "init"], storage_root, run)
        _run_git(["git", "add", "-A"], storage_root, run)
        # .env вне initial commit (AC #4): reset снимает его из индекса, если попал.
        _run_git(["git", "reset", "--", ".env"], storage_root, run)
        # Непустой индекс? `git diff --cached --quiet`: код 0 = нет staged, !=0 = есть staged.
        diff = run(["git", "diff", "--cached", "--quiet"], cwd=storage_root)
        if diff.returncode == 0:
            raise StorageInitError(
                f"Нет файлов для initial commit в {storage_root} (индекс пуст) — "
                f"проверь шаблон хранилища."
            )
        _run_git(
            ["git", "commit", "-m", f"init: развёртывание хранилища игры {game}"],
            storage_root,
            run,
        )
    except FileNotFoundError as exc:  # preflight which() ловит раньше; подстраховка
        raise StorageInitError(
            f"git не найден при инициализации репозитория: {exc}"
        ) from exc
    except OSError as exc:  # PermissionError / WinError 740 и пр. — не «голый» наружу
        raise StorageInitError(
            f"Не удалось запустить git в {storage_root}: {exc}"
        ) from exc


def _rollback(storage_root: Path) -> None:
    """Полный откат: удалить созданное хранилище целиком (AC #6, #10, #12).

    ``shutil.rmtree`` снимает инфра-симлинки внутри дерева через ``os.unlink`` (НЕ рекурсирует в
    цель) → код/каталог dev-репо за симлинками НЕ удаляются (нативное безопасное поведение,
    проверяется тестом). ``storage_root`` создан ИМЕННО этим запуском (D4 — имя было свободно),
    поэтому полное удаление безопасно: данных владельца там ещё нет (токен он вписывает ПОСЛЕ
    init). Best-effort: rmtree не дочистил → WARNING + не маскируем исходную ошибку (её пробрасывает
    вызывающий); оператору сказано удалить остаток вручную (AC #10 resume).
    """
    if not storage_root.exists() and not storage_root.is_symlink():
        return  # валидация шаблона упала ДО создания каталога — чистить нечего
    try:
        shutil.rmtree(storage_root)
        logger.info("Откат: хранилище %s удалено целиком", storage_root)
    except OSError as exc:
        logger.warning(
            "Откат не дочистил хранилище %s: %s. Удали остаток вручную и повтори gdau-init "
            "(имя пока занято этим остатком).",
            storage_root,
            exc,
        )


def init_storage(
    game: str,
    *,
    dev_repo_root: Path | None = None,
    storage_parent: Path | None = None,
    runner: CommandRunner | None = None,
) -> Path:
    """Развернуть per-game хранилище за один проход; полный откат при сбое (AC #1, #6). Вернуть путь.

    Швы инъекции (дефолты — прод-резолюция, тест даёт ``tmp_path``): ``dev_repo_root`` (корень
    инфры, шаблон/контракт берутся из ``{repo}/templates/``), ``storage_parent`` (родитель папки
    игры; ``None`` → сосед dev-репо), ``runner`` (запуск ``git``/``uv``; ``None`` →
    :func:`_default_runner`). Порядок (D6): валидация имени → резолюция пути → «имя свободно» →
    preflight'ы (git/uv/симлинки) ДО создания → шаблон → симлинки → ``.env`` → ``uv sync`` →
    БД+view'ы → ``git``. Любой сбой шагов после копирования шаблона (включая ``KeyboardInterrupt``)
    → :func:`_rollback` (полное удаление хранилища) + проброс исходной ошибки.
    """
    name = _validate_game_name(game)  # AC #7 — чистая проверка ДО ФС
    repo_root = dev_repo_root if dev_repo_root is not None else _resolve_dev_repo_root()
    run: CommandRunner = runner if runner is not None else _default_runner
    storage_root = _resolve_storage_root(name, repo_root, storage_parent)  # AC #11 (не от cwd)

    # AC #2: имя занято (файл/каталог/симлинк, в т.ч. битый) → fail-loud ДО любых мутаций.
    if storage_root.exists() or storage_root.is_symlink():
        raise StorageInitError(
            f"Имя занято: уже существует {storage_root}. Выбери другое имя игры "
            f"или удали этот путь вручную."
        )

    # Preflight'ы ДО создания хранилища (AC #5, #9): непригодная платформа/окружение падают
    # «насухо», откат не нужен (хранилища ещё нет).
    _preflight_environment()
    preflight_symlink_capability()

    template_root = repo_root / "templates" / "external_storage"
    contract_path = repo_root / "templates" / "paths-to-symlink.csv"

    # С момента copy_storage_template хранилище — наше (D4 гарантировал: не пред-существовало) →
    # любой сбой шагов ниже → полный откат (D5). BaseException ловит и KeyboardInterrupt (AC #6).
    try:
        logger.info("Разворачиваю хранилище игры %r → %s", name, storage_root)
        copy_storage_template(storage_root=storage_root, template_root=template_root)
        create_symlinks(
            dev_repo_root=repo_root,
            storage_root=storage_root,
            contract_path=contract_path,
            run_preflight=False,  # preflight уже сделан выше — не повторяем (К1-паттерн 4.1)
        )
        _write_env(storage_root)
        logger.info("Ставлю окружение хранилища: uv sync --frozen")
        _uv_sync(storage_root, run)
        logger.info("Создаю gdau.duckdb и типизированные view'ы из каталога")
        _create_database(storage_root)
        logger.info("Инициализирую git-репозиторий хранилища (без .env)")
        _git_init_commit(storage_root, name, run)
    except BaseException:
        _rollback(storage_root)
        raise

    logger.info("Хранилище игры развёрнуто: %s", storage_root)
    return storage_root


def _create_parser() -> argparse.ArgumentParser:
    """argparse-парсер ``gdau-init`` — единственный позиционный ``game`` (форма directaiq)."""
    parser = argparse.ArgumentParser(
        prog="gdau-init",
        description=(
            "Развернуть per-game хранилище игры рядом с dev-репо: имя → шаблон → симлинки → "
            ".env → uv sync → gdau.duckdb + view'ы → git init. Имя занято → остановка без "
            "перезаписи; сбой любого шага → полный откат хранилища."
        ),
    )
    parser.add_argument(
        "game", help="имя игры (каталог-сосед dev-репо; латиница/цифры/`_`/`-`, до 64 символов)"
    )
    return parser


def main() -> None:
    """Точка входа console-команды ``gdau-init`` (AC #1, #5).

    Успех → INFO с инструкцией про токен/счётчик, неявный код 0. ``StorageInitError`` и доменные
    ошибки примитивов (``Symlink*Error``/``StorageTemplateError``) → ERROR + ``SystemExit(1)`` без
    трейсбека. ``KeyboardInterrupt`` посреди разворота → хранилище уже откатано в
    :func:`init_storage` → понятное сообщение + ``SystemExit(130)`` (как 2.9). Плохие аргументы /
    голый вызов → argparse сам ``SystemExit(2)``.
    """
    logging.basicConfig(level=logging.INFO)
    parser = _create_parser()
    args = parser.parse_args()
    try:
        storage_root = init_storage(args.game)
    except KeyboardInterrupt:
        logger.error(
            "Прервано оператором — частичное хранилище откатано; повтори gdau-init заново."
        )
        raise SystemExit(130) from None
    except (
        StorageInitError,
        SymlinkPreflightError,
        SymlinkContractError,
        SymlinkError,
        StorageTemplateError,
    ) as exc:
        logger.error("%s", exc)  # понятное сообщение, без трейсбека
        raise SystemExit(1) from None
    logger.info(
        "Хранилище %s готово. Впиши %s и %s в %s, затем запусти: "
        "uv run gdau-logs update --date1 ГГГГ-ММ-ДД --date2 ГГГГ-ММ-ДД --source both",
        storage_root,
        env_reader.TOKEN_ENV,
        env_reader.COUNTER_ENV,
        storage_root / ".env",
    )


if __name__ == "__main__":
    main()
