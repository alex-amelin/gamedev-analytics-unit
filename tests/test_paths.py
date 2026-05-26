"""Offline-тесты резолверов путей per-game хранилища (история 2.1).

Покрывают дисциплину фундамента, а не только happy-path: резолюцию всех путей
относительно ``GDAU_DATA_ROOT`` (AC #1), fail-loud без создания мусорных каталогов
при отсутствующем/несуществующем корне (AC #5), валидацию источника и запрет тяжёлых
зависимостей / directaiq-инфры (по реальным import-узлам через ``ast``, не по подстроке).

Без сети (модуль сетей не знает). Корень хранилища задаётся ``monkeypatch.setenv`` на
``tmp_path``; снимается ``monkeypatch.delenv``. Сравнение путей — через ``Path``-равенство
(не строки), чтобы тест проходил на обеих ОС (CI: ubuntu + windows).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.utils.env_reader import DATA_ROOT_ENV
from scripts.utils.paths import (
    get_db_path,
    get_mcp_output_dir,
    get_raw_partition_path,
    get_raw_source_dir,
    get_results_dir,
    get_storage_root,
    get_writer_lock_path,
)

# --- AC #1: резолюция путей относительно корня хранилища ------------------------


def test_get_storage_root_resolves_env_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_storage_root возвращает существующий корень из GDAU_DATA_ROOT (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    assert get_storage_root() == tmp_path.resolve()


def test_get_db_path_under_data_duckdb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_db_path → {root}/data/duckdb/gdau.duckdb (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    assert get_db_path() == tmp_path.resolve() / "data" / "duckdb" / "gdau.duckdb"


def test_get_raw_partition_path_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_raw_partition_path → {root}/data/raw/{source}/{date}.parquet (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    expected = tmp_path.resolve() / "data" / "raw" / "visits" / "2026-05-20.parquet"
    assert get_raw_partition_path("visits", "2026-05-20") == expected


def test_get_raw_source_dir_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_raw_source_dir → {root}/data/raw/{source} (AC #1; для views.py 2.6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    assert get_raw_source_dir("hits") == tmp_path.resolve() / "data" / "raw" / "hits"


def test_get_writer_lock_path_at_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_writer_lock_path → {root}/.writer.lock (AC #1; захват — 2.5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    assert get_writer_lock_path() == tmp_path.resolve() / ".writer.lock"


# --- 3.2: каталоги результатов/аудита MCP — чистые резолверы без mkdir (AC #7) ----


def test_get_results_dir_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_results_dir → {root}/data/results (3.2, AC #7)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    assert get_results_dir() == tmp_path.resolve() / "data" / "results"


def test_get_mcp_output_dir_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_mcp_output_dir → {root}/data/mcp_output (3.2, AC #7)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    assert get_mcp_output_dir() == tmp_path.resolve() / "data" / "mcp_output"


def test_results_and_mcp_output_dirs_make_no_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Резолверы 3.2 НЕ делают mkdir — каталог создаёт писатель на месте записи (AC #7, риск №3).

    Отличие get_mcp_output_dir от directaiq (там был mkdir): инвариант чистых резолверов.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    results = get_results_dir()
    mcp_output = get_mcp_output_dir()

    # Пути построены, но каталоги на диске не появились (резолвер чистый).
    assert not results.exists()
    assert not mcp_output.exists()
    # Под корнем хранилища нет ни одного побочного каталога после вызова резолверов.
    assert list(tmp_path.iterdir()) == []


def test_results_and_mcp_output_dirs_fail_loud_without_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Нет GDAU_DATA_ROOT → ValueError (fail-loud наследуется из get_storage_root, AC #7)."""
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)

    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        get_results_dir()
    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        get_mcp_output_dir()


# --- AC #5: не задан / несуществующий корень → fail-loud БЕЗ side-effect ---------


def test_unset_root_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GDAU_DATA_ROOT не задан → ValueError на резолюции (AC #5)."""
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)

    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        get_storage_root()
    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        get_db_path()


def test_unset_root_creates_no_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """При не заданном корне ни один каталог не создаётся (AC #5: нет мусорной резолюции).

    cwd на время теста переносим в пустой tmp_path: если бы резолвер делал mkdir
    относительно cwd/репо, здесь появился бы артефакт. Доказываем отсутствие side-effect.
    """
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        get_db_path()

    # Ни data/, ни duckdb/ не должны появиться нигде в рабочем каталоге.
    assert list(tmp_path.iterdir()) == []


def test_nonexistent_root_raises_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GDAU_DATA_ROOT указывает на несуществующий путь → ValueError; путь не создаётся (AC #5)."""
    missing = tmp_path / "nope"
    monkeypatch.setenv(DATA_ROOT_ENV, str(missing))

    with pytest.raises(ValueError, match="nope"):
        get_storage_root()
    with pytest.raises(ValueError):
        get_db_path()

    # Резолвер ничего не mkdir-ит — несуществующий корень так и не появился.
    assert not missing.exists()


def test_root_pointing_at_file_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GDAU_DATA_ROOT указывает на файл (не каталог) → ValueError (is_dir-гейт, AC #5)."""
    bogus = tmp_path / "not_a_dir.txt"
    bogus.write_text("x", encoding="utf-8")
    monkeypatch.setenv(DATA_ROOT_ENV, str(bogus))

    with pytest.raises(ValueError):
        get_storage_root()


def test_empty_root_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """GDAU_DATA_ROOT задан, но пустой/из пробелов → ValueError (AC #5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, "   ")

    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        get_storage_root()


def test_relative_root_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Относительный GDAU_DATA_ROOT → ValueError ДО резолюции (review-патч, AC #5).

    Без is_absolute-гейта относительное значение разрешилось бы против cwd и при запуске
    из dev-репо могло бы увести запись ВНУТРЬ dev-репо (инвариант «в dev-репо данные не
    пишутся»). cwd переносим в пустой tmp_path и доказываем отсутствие side-effect.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(DATA_ROOT_ENV, "data")

    with pytest.raises(ValueError, match="абсолютн"):
        get_storage_root()

    # Гейт срабатывает ДО построения путей — ничего не создано.
    assert list(tmp_path.iterdir()) == []


# --- Валидация источника: мусорный source не должен молча резолвиться ------------


def test_get_raw_partition_path_invalid_source_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Невалидный source в get_raw_partition_path → ValueError (не молчаливая резолюция)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="sessions"):
        get_raw_partition_path("sessions", "2026-05-20")


def test_get_raw_source_dir_invalid_source_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Невалидный source в get_raw_source_dir → ValueError."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="sessions"):
        get_raw_source_dir("sessions")


# --- Анти-зависимость: модуль не тянет pandas/polars/numpy и инфру directaiq -----


def test_no_heavy_or_directaiq_dependencies_imported() -> None:
    """Среди реальных import-узлов нет тяжёлых пакетов и directaiq-инфры (NFR-6).

    Намеренно НЕ по подстроке: docstring/комментарии упоминают directaiq/config_manager —
    наивный поиск дал бы ложный красный. Парсим AST, смотрим Import/ImportFrom top-level.
    В частности — нет dotenv (paths .env НЕ грузит, риск #2: это зона env_reader).
    """
    import scripts.utils.paths as mod

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

    forbidden = {"pandas", "polars", "numpy", "config_manager", "base_script", "auth_manager", "dotenv"}
    offenders = {name for name in imported if name.split(".")[0] in forbidden}
    assert not offenders, f"запрещённые импорты в paths: {offenders}"
