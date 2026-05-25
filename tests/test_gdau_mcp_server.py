"""Offline-тесты MCP-сервера ``gdau`` (история 3.1).

Покрывают развязку и регистрацию: модуль импортируется и поднимает ``FastMCP("gdau_mcp")``
с инструментом ``duckdb_query`` (AC #1/#5), инструмент сквозь сервер исполняет SQL по read-only
соединению (AC #2/#3), и — главное для развязки — в ``scripts/mcp/**`` нет импорта вырезанной
инфры directaiq (``config_manager``/``auth_manager``/``directaiq``/``scripts.mcp.utils.common``),
проверяется по реальным import-узлам через ``ast`` (не подстрокой — docstring'и упоминают
соседние имена), как ``test_database_manager.py``/``test_views.py``.

Без сети. DuckDB локален (write-фикстура под временным ``GDAU_DATA_ROOT``).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.mcp import gdau_mcp_server as server
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.env_reader import DATA_ROOT_ENV


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Временное хранилище с ``gdau.duckdb`` и таблицей ``visits``; сервер читает read-only."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    with DatabaseManager.connection() as conn:
        conn.execute("CREATE TABLE visits(visit_id BIGINT, page_views INTEGER)")
        conn.execute("INSERT INTO visits VALUES (1, 5), (2, 3)")
    return tmp_path


# --- AC #1/#5: сервер поднимается, брендинг gdau, инструмент зарегистрирован --------------


def test_server_name_is_gdau() -> None:
    """FastMCP назван `gdau_mcp` (брендинг gdau, без directaiq_*, AC #5)."""
    assert server.mcp.name == "gdau_mcp"


def test_tool_duckdb_query_registered() -> None:
    """Инструмент `duckdb_query` зарегистрирован в FastMCP (AC #1)."""
    tool_names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "duckdb_query" in tool_names


def test_tool_annotations_read_only() -> None:
    """Аннотации инструмента помечают канал read-only (в directaiq был False из-за --export, AC #3)."""
    tool = next(t for t in server.mcp._tool_manager.list_tools() if t.name == "duckdb_query")
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False


# --- AC #2/#3: инструмент через сервер исполняет SQL по read-only ------------------------


def test_tool_function_executes_sql(db: Path) -> None:
    """Вызов функции инструмента → handle_query → результат по visits (сквозной путь, AC #2)."""
    out = server.duckdb_query("SELECT visit_id FROM visits ORDER BY visit_id", "json", 10)
    parsed = json.loads(out)
    assert parsed["total_rows"] == 2
    assert parsed["rows"][0] == {"visit_id": 1}


def test_tool_function_rejects_write(db: Path) -> None:
    """Через сервер попытка записи тоже отклоняется guard'ом (read-only-инвариант, AC #3/#7)."""
    out = server.duckdb_query("DROP TABLE visits", "json", 10)
    assert "только для чтения" in out


# --- AC #5: анти-зависимость — в scripts/mcp/** нет вырезанной directaiq-инфры -----------


def _collect_imports(py_file: Path) -> set[str]:
    """Множество импортированных имён модуля по реальным import-узлам AST (не подстрока)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)
    return imported


def test_no_directaiq_infra_imported_in_mcp_package() -> None:
    """В scripts/mcp/** нет импорта config_manager/auth_manager/directaiq/utils.common (AC #5).

    По реальным import-узлам (ast), как test_database_manager.py: docstring'и упоминают эти имена
    словами, поэтому проверка по подстроке дала бы ложный красный. ``config_manager`` в репо НЕТ —
    вендоренный ``utils/common.get_config`` дал бы ImportError на старте (сервер не поднялся бы).
    """
    mcp_dir = Path(server.__file__).parent
    py_files = sorted(mcp_dir.rglob("*.py"))
    assert py_files, "не найдено ни одного модуля в scripts/mcp/"

    forbidden_head = {"pandas", "polars", "numpy", "pyarrow", "config_manager", "auth_manager", "base_script"}
    forbidden_full = {
        "scripts.utils.config_manager",
        "scripts.utils.auth_manager",
        "scripts.mcp.utils.common",
    }

    for py_file in py_files:
        imported = _collect_imports(py_file)
        head_offenders = {n for n in imported if n.split(".")[0] in forbidden_head}
        # 'directaiq' как ведущий сегмент любого импорта тоже запрещён (вендоринг развязан).
        directaiq_offenders = {n for n in imported if n.split(".")[0] == "directaiq"}
        full_offenders = imported & forbidden_full
        assert not head_offenders, f"{py_file.name}: запрещённые импорты {head_offenders}"
        assert not directaiq_offenders, f"{py_file.name}: импорт directaiq {directaiq_offenders}"
        assert not full_offenders, f"{py_file.name}: импорт вырезанной инфры {full_offenders}"


def test_core_imports_database_manager_not_writers() -> None:
    """Ядро читает через DatabaseManager, но не импортирует путь записи (parquet_store/p81/lock)."""
    core_file = Path(server.__file__).parent / "tools" / "core.py"
    imported = _collect_imports(core_file)
    assert "scripts.utils.database_manager" in imported
    for writer in (
        "scripts.utils.parquet_store",
        "scripts.utils.writer_lock",
        "scripts.utils.load_state",
        "scripts.utils.metrica_client",
    ):
        assert writer not in imported, f"core не должен импортировать путь записи {writer}"
