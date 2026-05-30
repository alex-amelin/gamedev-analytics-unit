"""Offline-тесты оркестратора разворачивания per-game хранилища (история 4.3).

Покрывают дисциплину сборки, а не только happy-path: строгую валидацию имени игры (AC #7),
резолюцию пути ``../{game}`` от корня dev-репо, НЕ от cwd (AC #11), fail-loud занятого имени без
перезаписи (AC #2), полный проход на мини-dev-репо с пустыми типизированными view'ами (AC #1, #14),
**полный откат** хранилища при сбое шага с критичной проверкой «откат не трогает цели инфра-симлинков»
(AC #6), генерацию ``.env`` без секретов + исключение его из initial commit (AC #3, #4, #13), preflight
``git`` (AC #9) и ast-анти-зависимость (только ``scripts.init.*``/``scripts.utils.*`` + stdlib, без
``duckdb``/тяжёлого стека/directaiq-инфры).

Инъекция швов (``dev_repo_root``/``storage_parent``/``runner``) — оркестратор тестируется на
``tmp_path`` без записи в реальный dev-репо и без сети: ``uv sync`` подменяется фейком, ``git``
— реальный (быстрый, offline; идентичность через ``GIT_*``-env). Реальные симлинки гейтятся
``preflight_symlink_capability`` (Windows без Developer Mode → ``skip``, не красный); покрытие
AC #1/#6/#14 даёт ubuntu-прогон. Live-набор осознанно отсутствует: 4.3 — ФС/процессы (``git``/
``uv``), без внешнего API ([[realapi-smoke-tests]] — opt-in live только для Logs API), как 2.1–2.6/4.1/4.2.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from scripts.init.init_project import (
    SCRIPTS_PTH_NAME,
    StorageInitError,
    _create_parser,
    _resolve_storage_root,
    _validate_game_name,
    _write_scripts_pth,
    init_storage,
)
from scripts.init.scaffold import StorageTemplateError
from scripts.init.symlinks import SymlinkPreflightError, preflight_symlink_capability
from scripts.utils.env_reader import COUNTER_ENV, DATA_ROOT_ENV, TOKEN_ENV


# --- Фикстуры/хелперы -------------------------------------------------------------------


@pytest.fixture
def symlink_capable() -> None:
    """Гейт реальных симлинк-тестов: нет способности (Windows без Dev Mode) → skip, не красный."""
    try:
        preflight_symlink_capability()
    except SymlinkPreflightError:
        pytest.skip("нет способности создавать симлинки (Windows без Developer Mode)")


@pytest.fixture
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Изолировать git от global/system config: идентичность через ``GIT_*``-env + нейтрализация
    конфига (``commit.gpgsign``/``core.hooksPath``/``init.templateDir`` чужого окружения уронили
    бы ``git commit``). Детерминизм на любой машине/CI."""
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "gdau-test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    # Полная изоляция от пользовательского/системного git-конфига (gpgsign/hooks/templateDir).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _make_mini_dev_repo(tmp_path: Path) -> Path:
    """Собрать мини-dev-репо на ``tmp_path``: шаблон (5 файлов) + контракт + цели симлинков.

    Корень назван как реальный dev-репо (``gamedev-analytics-unit``) — резолюция ``../{game}``
    кладёт хранилище соседом. ``_create_database`` использует РЕАЛЬНЫЙ каталог (``load_catalog``
    резолвится от модуля ``catalog.py``, не от ``dev_repo_root``) → мини-репо каталог не нужен.
    """
    dev = tmp_path / "gamedev-analytics-unit"
    tpl = dev / "templates" / "external_storage"
    tpl.mkdir(parents=True)
    (tpl / ".env.example").write_text(
        "YANDEX_METRICA_TOKEN=\nYANDEX_METRICA_COUNTER_ID=\n# GDAU_DATA_ROOT=\n",
        encoding="utf-8",
    )
    (tpl / ".gitignore").write_text(
        ".env\n.env.*\n!.env.example\n.venv/\ndata/\n.writer.lock\n*.duckdb\n*.parquet\n"
        "/scripts\n/pyproject.toml\n",
        encoding="utf-8",
    )
    (tpl / "CLAUDE.md").write_text("# рабочее пространство игры (тест)\n", encoding="utf-8")
    (tpl / "gdd.md").write_text("<!-- заполни: название игры -->\n", encoding="utf-8")
    (tpl / "EVENTS.md").write_text("<!-- заполни: события аналитики -->\n", encoding="utf-8")
    # Контракт симлинков + реальные цели в мини-репо (каталог + файл).
    (dev / "templates" / "paths-to-symlink.csv").write_text(
        "path,comment\nscripts,код\npyproject.toml,манифест\n", encoding="utf-8", newline=""
    )
    (dev / "scripts").mkdir()
    (dev / "scripts" / "marker.txt").write_text("я в dev-репо", encoding="utf-8")
    (dev / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return dev


def _real_git_fake_uv(
    calls: list[list[str]],
) -> object:
    """Раннер: ``uv sync`` подменён фейком (создаёт ``.venv``, код 0), ``git`` — реальный."""

    def runner(
        args: list[str], *, cwd: Path, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[:2] == ["uv", "sync"]:
            # Симулируем uv sync как реальный uv: создаём .venv с site-packages — туда init
            # пишет _gdau_scripts.pth (фикс editable из симлинк-раскладки).
            (cwd / ".venv" / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)

    return runner


# --- AC #7: валидация имени (чистая, всегда идёт) ---------------------------------------


@pytest.mark.parametrize("name", ["mygame", "Game-1", "g", "a_b-C9", "x" * 64])
def test_valid_game_names_pass(name: str) -> None:
    """Допустимые имена проходят валидацию и возвращаются очищенными (AC #7)."""
    assert _validate_game_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "my/game",
        "my\\game",
        "..",
        "../secret",
        ".hidden",
        "my game",
        "game!",
        "юникод",
        "CON",
        "nul",
        "com1",
        "LPT9",
        "x" * 65,
    ],
)
def test_invalid_game_names_fail_loud(name: str) -> None:
    """Опасные/некорректные имена → StorageInitError (AC #7)."""
    with pytest.raises(StorageInitError):
        _validate_game_name(name)


# --- AC #11: резолюция пути от корня dev-репо, НЕ от cwd ---------------------------------


def test_resolve_storage_root_is_dev_repo_parent_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_resolve_storage_root`` = ``dev_repo_root.parent / game`` независимо от cwd (AC #11, D2)."""
    dev = tmp_path / "a" / "b" / "gamedev-analytics-unit"
    other = tmp_path / "elsewhere"
    other.mkdir(parents=True)
    monkeypatch.chdir(other)
    assert _resolve_storage_root("mygame", dev, None) == dev.parent / "mygame"
    # Смена cwd не меняет результат — резолюция чистая, не от текущего каталога.
    monkeypatch.chdir(tmp_path)
    assert _resolve_storage_root("mygame", dev, None) == dev.parent / "mygame"


def test_resolve_storage_root_honors_explicit_parent(tmp_path: Path) -> None:
    """Явный ``storage_parent`` перекрывает дефолт ``dev_repo_root.parent``."""
    dev = tmp_path / "dev"
    parent = tmp_path / "games"
    assert _resolve_storage_root("g", dev, parent) == parent / "g"


def test_name_taken_resolves_from_dev_repo_parent_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """init_storage ищет ``dev_repo_root.parent / game``, не ``cwd / game`` (AC #11 через AC #2)."""
    dev = _make_mini_dev_repo(tmp_path)
    (dev.parent / "mygame").mkdir()  # занято соседом dev-репо
    other = tmp_path / "elsewhere"  # в cwd «mygame» НЕТ
    other.mkdir()
    monkeypatch.chdir(other)
    with pytest.raises(StorageInitError, match="Имя занято"):
        init_storage("mygame", dev_repo_root=dev)  # storage_parent=None → dev.parent


# --- AC #2: имя занято → fail-loud без перезаписи ---------------------------------------


def test_name_taken_fails_loud_and_untouched(tmp_path: Path) -> None:
    """Существующая ``../{game}`` → fail-loud ДО мутаций; данные владельца не тронуты (AC #2)."""
    dev = _make_mini_dev_repo(tmp_path)
    parent = tmp_path / "sp"
    parent.mkdir()
    existing = parent / "mygame"
    existing.mkdir()
    (existing / "owner.txt").write_text("данные владельца", encoding="utf-8")

    with pytest.raises(StorageInitError, match="Имя занято"):
        init_storage("mygame", dev_repo_root=dev, storage_parent=parent)

    assert (existing / "owner.txt").read_text(encoding="utf-8") == "данные владельца"


# --- AC #9: preflight git (без симлинк-способности) -------------------------------------


def test_git_missing_preflight_fails_loud_before_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git`` не в PATH → StorageInitError на preflight, ДО создания хранилища (AC #9)."""
    dev = _make_mini_dev_repo(tmp_path)
    parent = tmp_path / "sp"
    parent.mkdir()
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: None if cmd == "git" else real_which(cmd)
    )
    with pytest.raises(StorageInitError, match="git не найден"):
        init_storage("mygame", dev_repo_root=dev, storage_parent=parent)
    assert not (parent / "mygame").exists()  # preflight ДО мутаций — хранилища нет


# --- AC #1, #3, #4, #13, #14: полный проход (capability-gated симлинки) ------------------


def test_full_init_pass(
    tmp_path: Path, symlink_capable: None, git_identity: None
) -> None:
    """Полный разворот: шаблон + симлинки + .env + uv + БД/view'ы + git (AC #1, #3, #4, #13, #14)."""
    dev = _make_mini_dev_repo(tmp_path)
    parent = tmp_path / "sp"
    parent.mkdir()
    calls: list[list[str]] = []

    storage_root = init_storage(
        "mygame", dev_repo_root=dev, storage_parent=parent, runner=_real_git_fake_uv(calls)
    )

    assert storage_root == parent / "mygame"
    assert storage_root.is_dir()

    # Шаблон (5 файлов) на месте.
    for name in (".env.example", ".gitignore", "CLAUDE.md", "gdd.md", "EVENTS.md"):
        assert (storage_root / name).is_file(), f"нет файла шаблона: {name}"

    # Симлинки указывают в dev-репо (относительные); чтение сквозь линк отдаёт dev-репо.
    for rel in ("scripts", "pyproject.toml"):
        link = storage_root / rel
        assert link.is_symlink(), f"{rel} не симлинк"
        assert not os.path.isabs(os.readlink(link))
    assert (storage_root / "scripts" / "marker.txt").read_text(encoding="utf-8") == "я в dev-репо"

    # .env: GDAU_DATA_ROOT = абсолютный путь хранилища; токен/счётчик пусты (AC #3).
    env_text = (storage_root / ".env").read_text(encoding="utf-8")
    assert f"{DATA_ROOT_ENV}={storage_root}" in env_text
    assert _env_value(env_text, TOKEN_ENV) == "", "в .env вписан секрет токена"
    assert _env_value(env_text, COUNTER_ENV) == "", "в .env вписан счётчик"

    # uv sync --frozen был вызван (AC #1).
    assert ["uv", "sync", "--frozen"] in calls

    # .pth-фикс editable: пакет scripts выставлен на путь venv хранилища (без него gdau-logs/
    # gdau-init из папки игры падали ModuleNotFoundError). Содержимое = корень хранилища.
    pth = storage_root / ".venv" / "Lib" / "site-packages" / SCRIPTS_PTH_NAME
    assert pth.is_file(), "нет _gdau_scripts.pth — фикс editable не применён"
    assert pth.read_text(encoding="utf-8").strip() == str(storage_root)

    # gdau.duckdb создан; view'ы visits/hits существуют и пусты-типизированы (AC #1, #14).
    db_path = storage_root / "data" / "duckdb" / "gdau.duckdb"
    assert db_path.is_file()
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM hits").fetchone()[0] == 0
    finally:
        conn.close()

    # git: репо изолировано в хранилище, initial commit непуст, .env исключён (AC #4, #8, #13).
    assert (storage_root / ".git").is_dir()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=storage_root, capture_output=True, text=True
    )
    assert log.returncode == 0 and log.stdout.strip(), "нет initial commit (пуст?)"
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=storage_root, capture_output=True, text=True
    ).stdout.split()
    assert ".env" not in tracked, ".env попал в коммит (нарушение AC #4)"
    assert ".env.example" in tracked and "gdd.md" in tracked and "EVENTS.md" in tracked


def _env_value(env_text: str, var: str) -> str | None:
    """Значение активной строки ``var=...`` из текста ``.env`` (комментарии игнорируются)."""
    for line in env_text.splitlines():
        if line.startswith(f"{var}="):
            return line.split("=", 1)[1].strip()
    return None


# --- Фикс editable: .pth выставляет пакет scripts на путь venv хранилища ----------------


def test_write_scripts_pth_windows_layout(tmp_path: Path) -> None:
    """Windows-раскладка venv (``Lib/site-packages``) → ``.pth`` с корнем хранилища.

    Регрессия: editable-wheel из симлинк-раскладки не выставляет ``scripts`` — без ``.pth``
    ``gdau-logs`` из venv хранилища падал ``ModuleNotFoundError: scripts.tools``.
    """
    sp = tmp_path / ".venv" / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    _write_scripts_pth(tmp_path)
    assert (sp / SCRIPTS_PTH_NAME).read_text(encoding="utf-8").strip() == str(tmp_path)


def test_write_scripts_pth_posix_layout(tmp_path: Path) -> None:
    """POSIX-раскладка venv (``lib/pythonX.Y/site-packages``) → ``.pth`` найден и записан."""
    sp = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages"
    sp.mkdir(parents=True)
    _write_scripts_pth(tmp_path)
    assert (sp / SCRIPTS_PTH_NAME).read_text(encoding="utf-8").strip() == str(tmp_path)


def test_write_scripts_pth_missing_site_packages_fails_loud(tmp_path: Path) -> None:
    """Нет site-packages (``uv sync`` не создал окружение) → ``StorageInitError`` fail-loud."""
    (tmp_path / ".venv").mkdir()  # venv без site-packages
    with pytest.raises(StorageInitError, match="site-packages"):
        _write_scripts_pth(tmp_path)


# --- AC #6, #10, #12: полный откат хранилища при сбое шага -------------------------------


def test_rollback_removes_storage_and_keeps_symlink_targets(
    tmp_path: Path, symlink_capable: None
) -> None:
    """Сбой ``uv sync`` → полный откат хранилища; цели инфра-симлинков ЦЕЛЫ (AC #6, критичный тест).

    После отката имя снова свободно (чистый повтор возможен, AC #10). ``rmtree`` снимает симлинки
    ``os.unlink``'ом, не рекурсируя в цель → код dev-репо за симлинком не удалён (anti-disaster D5).
    """
    dev = _make_mini_dev_repo(tmp_path)
    parent = tmp_path / "sp"
    parent.mkdir()

    def failing_uv(
        args: list[str], *, cwd: Path, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["uv", "sync"]:
            return subprocess.CompletedProcess(args, 1, "", "сбой uv sync (смоделировано)")
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)

    with pytest.raises(StorageInitError, match="uv sync"):
        init_storage("mygame", dev_repo_root=dev, storage_parent=parent, runner=failing_uv)

    storage_root = parent / "mygame"
    assert not storage_root.exists(), "хранилище не откатано целиком (AC #6)"
    # КРИТИЧНО: цель инфра-симлинка (dev-репо) цела — rmtree снял ссылку, не тронул содержимое.
    assert (dev / "scripts" / "marker.txt").read_text(encoding="utf-8") == "я в dev-репо"
    assert (dev / "pyproject.toml").is_file()


def test_broken_template_propagates_and_leaves_no_storage(
    tmp_path: Path, symlink_capable: None
) -> None:
    """Битый шаблон (нет обязательного файла) → fail-loud, хранилище не создано (AC #12-смежн.).

    ``copy_storage_template`` валидирует шаблон ДО ``mkdir`` (4.2 AC #5) → ``storage_root`` не
    появляется; ошибка пробрасывается, откат — no-op (чистить нечего).
    """
    dev = _make_mini_dev_repo(tmp_path)
    (dev / "templates" / "external_storage" / "gdd.md").unlink()  # битый шаблон
    parent = tmp_path / "sp"
    parent.mkdir()

    with pytest.raises(StorageTemplateError):  # 4.2 бросает его на битом шаблоне ДО мутаций
        init_storage("mygame", dev_repo_root=dev, storage_parent=parent)
    assert not (parent / "mygame").exists()


# --- argparse-поверхность ----------------------------------------------------------------


def test_parser_takes_single_positional_game() -> None:
    """``_create_parser`` принимает один позиционный ``game`` (форма directaiq)."""
    parser = _create_parser()
    args = parser.parse_args(["мояигра"])
    assert args.game == "мояигра"


# --- Анти-зависимость: только scripts.init.*/scripts.utils.* + stdlib --------------------


def test_no_forbidden_imports() -> None:
    """``init_project.py`` не тянет ``duckdb``/тяжёлый стек/directaiq-инфру (ast по import-узлам).

    Не по подстроке (docstring упоминает duckdb/paths по границам) — парсим AST. ``duckdb`` —
    только через ``database_manager`` (не напрямую); запись/чтение БД инкапсулированы в 2.1/2.6.
    """
    import scripts.init.init_project as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    forbidden_roots = {
        "pandas",
        "polars",
        "numpy",
        "pyarrow",
        "duckdb",
        "requests",
        "config_manager",
        "base_script",
    }
    offenders = {n for n in imported if n.split(".")[0] in forbidden_roots}
    assert not offenders, f"запрещённые импорты в init_project: {offenders}"
