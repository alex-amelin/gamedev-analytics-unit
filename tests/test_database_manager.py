"""Offline-тесты контекст-менеджера соединения DuckDB (история 2.1).

Покрывают дисциплину фундамента: write создаёт файл БД и работает как встроенный
движок без сервера (AC #2, #3), read-only читает записанное (AC #2), соединение
гарантированно закрывается без утечки/висящего лока — особенно на Windows (AC #4),
read-only до первой выгрузки → понятная ошибка «не инициализирована», а не сырой
``duckdb.IOException`` (AC #6), и запрет тяжёлых зависимостей / вырезанной directaiq-
инфры (по реальным import-узлам и AST-ссылкам, не по подстроке).

Без сети, DuckDB локален (файл в ``tmp_path``). Корень хранилища — ``monkeypatch.setenv``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import duckdb
import pytest

from scripts.utils.database_manager import DatabaseManager
from scripts.utils.env_reader import DATA_ROOT_ENV
from scripts.utils.paths import get_db_path

# --- AC #2/#3: write создаёт файл БД и работает (встроенный движок, ноль серверов) --


def test_write_connection_creates_and_persists_db_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write-соединение создаёт gdau.duckdb на диске и исполняет DDL/DML (AC #2, #3)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with DatabaseManager.connection() as conn:
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1), (2)")
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (2,)

    # Реальный файл на диске = embedded, ноль серверных процессов (FR-8, AC #3).
    assert get_db_path().is_file()


def test_write_connection_creates_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write-режим создаёт родителя data/duckdb/ внутри провалидированного корня (AC #2)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    assert not (tmp_path / "data" / "duckdb").exists()

    with DatabaseManager.connection() as conn:
        conn.execute("SELECT 1").fetchone()

    assert (tmp_path / "data" / "duckdb").is_dir()


def test_write_mkdir_failure_wrapped_as_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой mkdir родителя БД → RuntimeError, а не сырой OSError (review-патч).

    Контракт модуля: на ошибку открытия — понятный RuntimeError с контекстом, не сырой
    трейсбек ОС. Кладём ФАЙЛ туда, где write-режим ожидает каталог data/, — mkdir
    родителя data/duckdb упрётся в OSError (NotADirectoryError/FileExistsError). Если бы
    обёртки не было, сырой OSError пролетел бы мимо pytest.raises(RuntimeError).
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    (tmp_path / "data").write_text("x", encoding="utf-8")  # файл на месте каталога data/

    with pytest.raises(RuntimeError, match="каталог БД"):
        with DatabaseManager.connection():
            pass


# --- AC #2: read-only читает записанное write-соединением -----------------------


def test_read_only_connection_reads_written_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read-only отдаёт данные, записанные предыдущим write-соединением (AC #2)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with DatabaseManager.connection() as conn:
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")

    with DatabaseManager.connection(read_only=True) as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [(42,)]


# --- AC #4: соединение закрывается; нет утечки/висящего хэндла ------------------


def test_connection_closed_after_context_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После выхода из with соединение закрыто — обращение к нему падает (AC #4)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with DatabaseManager.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    leaked = conn  # ссылка на закрытый объект

    with pytest.raises(Exception):
        leaked.execute("SELECT 1")


def test_reopen_after_close_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write → close → снова open (write и read-only) успешно: нет висящего лока (AC #4).

    Windows особенно чувствителен: незакрытый write-conn держал бы эксклюзивный лок
    файла БД и заблокировал бы повторное открытие. Успешный реоткрытие доказывает
    чистое закрытие в finally.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with DatabaseManager.connection() as conn:
        conn.execute("CREATE TABLE t(x INTEGER)")
    with DatabaseManager.connection() as conn:
        conn.execute("INSERT INTO t VALUES (1)")
    with DatabaseManager.connection(read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (1,)


def test_connection_closed_even_on_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исключение в теле with не оставляет висящий лок — следующий open проходит (AC #4)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(RuntimeError, match="bang"):
        with DatabaseManager.connection() as conn:
            conn.execute("CREATE TABLE t(x INTEGER)")
            raise RuntimeError("bang")

    # finally-close сработал даже при исключении — повторное открытие успешно.
    with DatabaseManager.connection(read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (0,)


# --- AC #6: read-only до init → понятная ошибка, не сырой IOException -----------


def test_read_only_before_init_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read-only при отсутствующем gdau.duckdb → RuntimeError «не инициализирована» (AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    assert not get_db_path().exists()

    with pytest.raises(RuntimeError, match="не инициализирована"):
        with DatabaseManager.connection(read_only=True):
            pass


def test_read_only_before_init_creates_no_db_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read-only до init НЕ создаёт файл БД (превентивный гейт до connect, AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(RuntimeError):
        with DatabaseManager.connection(read_only=True):
            pass

    assert not get_db_path().exists()


def test_read_only_error_is_not_raw_duckdb_io_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибка read-only до init — НЕ сырой duckdb.IOException, а RuntimeError (риск #4, AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(Exception) as exc_info:
        with DatabaseManager.connection(read_only=True):
            pass

    assert not isinstance(exc_info.value, duckdb.Error)
    assert isinstance(exc_info.value, RuntimeError)


# --- AC #5 наследуется: битый корень → fail-loud до connect ---------------------


def test_missing_root_propagates_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GDAU_DATA_ROOT не задан → ValueError из get_db_path до открытия (AC #5 наследуется)."""
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)

    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        with DatabaseManager.connection():
            pass


# --- Анти-зависимость: нет тяжёлых пакетов и вырезанной directaiq-инфры ----------


def test_no_heavy_or_directaiq_infra_imported() -> None:
    """Нет import pandas/polars/numpy и directaiq-инфры; нет ссылок на вырезанное (NFR-6, риск #3).

    Не по подстроке (docstring упоминает register_udfs/миграции) — парсим AST. Помимо
    import-узлов проверяем отсутствие любых узлов-имён register_udfs/schema_migrations/
    migrations: эта инфра directaiq вырезана сознательно.
    """
    import scripts.utils.database_manager as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    referenced_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)

    forbidden_imports = {"pandas", "polars", "numpy", "config_manager", "base_script", "auth_manager"}
    import_offenders = {n for n in imported if n.split(".")[0] in forbidden_imports}
    assert not import_offenders, f"запрещённые импорты в database_manager: {import_offenders}"

    cut_infra = {"register_udfs", "schema_migrations", "migrations", "REQUIRED_TABLES", "TABLE_METADATA_DDL"}
    infra_offenders = cut_infra & referenced_names
    assert not infra_offenders, f"ссылки на вырезанную directaiq-инфру: {infra_offenders}"
