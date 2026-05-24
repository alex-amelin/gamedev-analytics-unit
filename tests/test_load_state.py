"""Offline-тесты чекпойнта ``load_state`` + реконсиляции мета×факт (история 2.4).

Покрывают дисциплину учёта целостности, не только happy-path: создание мета-таблицы и
идемпотентность DDL (AC #1), UPSERT-отметки без дублей (AC #1), реконсиляция как
конъюнкция трёх условий с источником истины = фактом партиции (AC #2/#3), расхождение
мета↔факт и отсутствие файла → день незагружен + мета приведена к факту (AC #4), битая/
нечитаемая партиция НЕ валит весь проход — соседний здоровый день остаётся (AC #5),
статусы ``loading``/``failed`` = незагружен даже при совпавшем count и осиротевший
``.parquet.tmp`` не считается партицией (AC #6, риск №5), плюс анти-зависимость по
реальным import-узлам через ``ast`` (``duckdb`` РАЗРЕШЁН; нет pandas/polars/numpy/pyarrow,
нет импорта ``parquet_store``/``database_manager`` — риск №7).

Без сети, DuckDB локален (in-memory conn). Корень хранилища — ``monkeypatch.setenv`` на
``tmp_path``; партиции-фикстуры пишутся реальным ``parquet_store.write_partition`` (2.2).
Live-набор осознанно отсутствует: ``load_state`` в сеть не ходит ([[realapi-smoke-tests]]
— opt-in live только для внешнего API).
"""

from __future__ import annotations

import ast
from pathlib import Path

import duckdb
import pytest

from scripts.utils.catalog import Catalog, CatalogField
from scripts.utils.env_reader import DATA_ROOT_ENV
from scripts.utils.load_state import (
    STATUS_LOADED,
    count_partition_rows,
    ensure_load_state_table,
    mark_failed,
    mark_loaded,
    mark_loading,
    reconcile,
)
from scripts.utils.parquet_store import write_partition
from scripts.utils.paths import get_raw_partition_path, get_raw_source_dir


def _catalog() -> Catalog:
    """Мини-каталог: visits (visit_id/date_time) + hits (watch_id). Типы не важны (всё VARCHAR)."""
    return Catalog(
        fields=(
            CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", "Идентификатор визита"),
            CatalogField("visits", "date_time", "ym:s:dateTime", "TIMESTAMP", "Дата/время визита"),
            CatalogField("hits", "watch_id", "ym:pv:watchID", "HUGEINT", "Идентификатор события"),
        )
    )


_VISITS_COLUMNS = ["ym:s:visitID", "ym:s:dateTime"]
_VISITS_ROWS = [
    ["17298374650000000001", "2026-05-20 12:34:56"],
    ["17298374650000000002", "2026-05-20 13:01:02"],
]


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    """In-memory соединение с уже созданной таблицей ``load_state`` (инъекция как в 2.1)."""
    connection = duckdb.connect()
    ensure_load_state_table(connection)
    try:
        yield connection
    finally:
        connection.close()


def _write_visits(date: str, rows: list[list[str | None]]) -> int:
    """Записать партицию visits указанного дня реальным ``write_partition`` (2.2)."""
    return write_partition("visits", date, _VISITS_COLUMNS, rows, catalog=_catalog())


def _meta_dates(connection: duckdb.DuckDBPyConnection, source: str) -> set[str]:
    """Множество дат (``YYYY-MM-DD``), оставшихся в журнале для источника."""
    rows = connection.execute(
        "SELECT date FROM load_state WHERE source = ?", [source]
    ).fetchall()
    return {row[0].isoformat() for row in rows}


# --- AC #1: таблица создаётся, DDL идемпотентен, отметки через UPSERT без дублей --------


def test_ensure_table_creates_schema_and_is_idempotent() -> None:
    """``ensure_load_state_table`` создаёт нужные колонки; повторный вызов не падает (AC #1)."""
    connection = duckdb.connect()
    try:
        ensure_load_state_table(connection)
        info = connection.execute("PRAGMA table_info('load_state')").fetchall()
        names = {row[1] for row in info}
        assert names == {"source", "date", "row_count", "loaded_at", "status"}

        types = {row[1]: row[2] for row in info}
        assert types["date"] == "DATE"  # для диапазонных запросов 2.8
        assert types["row_count"] == "BIGINT"  # НЕ HUGEINT (риск №6)

        # Идемпотентность: повторный CREATE TABLE IF NOT EXISTS не бросает.
        ensure_load_state_table(connection)
    finally:
        connection.close()


def test_mark_loaded_inserts_loaded_row(conn: duckdb.DuckDBPyConnection) -> None:
    """``mark_loaded`` → строка status='loaded', row_count проставлен, loaded_at не NULL (AC #1)."""
    mark_loaded(conn, "visits", "2026-05-20", 2)

    row = conn.execute(
        "SELECT row_count, status, loaded_at FROM load_state "
        "WHERE source = 'visits' AND date = '2026-05-20'"
    ).fetchone()
    assert row is not None
    row_count, status, loaded_at = row
    assert row_count == 2
    assert status == STATUS_LOADED
    assert loaded_at is not None  # время проставила БД (current_timestamp)


def test_mark_loaded_upserts_without_duplicate(conn: duckdb.DuckDBPyConnection) -> None:
    """Повторный ``mark_loaded`` того же дня обновляет строку, а не плодит дубль (AC #1, UPSERT)."""
    mark_loaded(conn, "visits", "2026-05-20", 2)
    mark_loaded(conn, "visits", "2026-05-20", 7)

    rows = conn.execute(
        "SELECT row_count FROM load_state WHERE source = 'visits' AND date = '2026-05-20'"
    ).fetchall()
    assert len(rows) == 1  # одна строка — не дубль
    assert rows[0][0] == 7  # обновлена последним значением


def test_mark_loaded_rejects_negative_row_count(conn: duckdb.DuckDBPyConnection) -> None:
    """Отрицательный row_count → ValueError (fail-loud, дефект вызывающего)."""
    with pytest.raises(ValueError, match="row_count"):
        mark_loaded(conn, "visits", "2026-05-20", -1)


def test_mark_loaded_rejects_invalid_source(conn: duckdb.DuckDBPyConnection) -> None:
    """Невалидный источник → ValueError (мусорный source не пишется в журнал)."""
    with pytest.raises(ValueError, match="source"):
        mark_loaded(conn, "sessions", "2026-05-20", 1)


# --- AC #2/#3: реконсиляция подтверждает день при конъюнкции трёх условий ----------------


def test_reconcile_confirms_loaded_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Партиция на N строк + meta loaded(row_count=N) → день в загруженных, мета не тронута (AC #2/#3)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    written = _write_visits("2026-05-20", _VISITS_ROWS)
    assert written == 2
    mark_loaded(conn, "visits", "2026-05-20", written)

    result = reconcile(conn)

    assert ("visits", "2026-05-20") in result
    # Мета не тронута: строка осталась со status='loaded'.
    assert _meta_dates(conn, "visits") == {"2026-05-20"}


# --- AC #4: расхождение/отсутствие → день незагружен, мета приведена к факту -------------


def test_reconcile_meta_ahead_of_fact_corrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Мета говорит 5, в партиции реально 2 → день НЕ загружен, ложная мета удалена (AC #4a)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    written = _write_visits("2026-05-20", _VISITS_ROWS)  # факт = 2
    assert written == 2
    mark_loaded(conn, "visits", "2026-05-20", 5)  # мета врёт: 5

    result = reconcile(conn)

    assert ("visits", "2026-05-20") not in result
    assert _meta_dates(conn, "visits") == set()  # ложный 'loaded' не остался


def test_reconcile_missing_partition_corrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Мета loaded, а файла партиции нет → день НЕ загружен, мета исправлена (AC #4b)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    mark_loaded(conn, "visits", "2026-05-20", 5)  # партицию НЕ писали

    result = reconcile(conn)

    assert ("visits", "2026-05-20") not in result
    assert _meta_dates(conn, "visits") == set()


# --- AC #5: битая партиция не валит весь проход -----------------------------------------


def test_reconcile_corrupt_partition_does_not_break_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Битый файл одного дня → этот день под перезалив, но проход продолжается; здоровый день остаётся (AC #5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    # Здоровый загруженный день.
    healthy = _write_visits("2026-05-21", _VISITS_ROWS)
    mark_loaded(conn, "visits", "2026-05-21", healthy)

    # Битый день: мусорные байты в .parquet, мета честно заявлена 'loaded'.
    corrupt_path = get_raw_partition_path("visits", "2026-05-20")
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"not a valid parquet file")
    mark_loaded(conn, "visits", "2026-05-20", 2)

    # reconcile НЕ должен бросить исключение из-за одного битого файла.
    result = reconcile(conn)

    assert ("visits", "2026-05-21") in result  # здоровый день уцелел — проход не сорван
    assert ("visits", "2026-05-20") not in result  # битый день под перезалив
    assert _meta_dates(conn, "visits") == {"2026-05-21"}  # мета битого дня исправлена


# --- AC #6: статус незавершённого дня + осиротевший .tmp ---------------------------------


@pytest.mark.parametrize("mark", [mark_loading, mark_failed])
def test_reconcile_pending_status_not_loaded_even_if_count_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conn: duckdb.DuckDBPyConnection,
    mark: object,
) -> None:
    """status loading/failed → день НЕ загружен, даже если файл есть и count совпал (строгий гейт, AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    _write_visits("2026-05-20", _VISITS_ROWS)  # здоровая партиция, count = 2
    mark(conn, "visits", "2026-05-20")  # type: ignore[operator]

    result = reconcile(conn)

    assert ("visits", "2026-05-20") not in result  # засчитывается только 'loaded'
    assert _meta_dates(conn, "visits") == set()  # полу-закоммиченная мета исправлена


def test_reconcile_ignores_stale_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Осиротевший ``{date}.parquet.tmp`` не считается партицией/загруженным днём (риск №5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    # Здоровый загруженный день, чтобы каталог источника существовал.
    healthy = _write_visits("2026-05-21", _VISITS_ROWS)
    mark_loaded(conn, "visits", "2026-05-21", healthy)

    # Stale temp от прошлого крэша для другого дня (без реального .parquet).
    source_dir = get_raw_source_dir("visits")
    stale_tmp = source_dir / "2026-05-20.parquet.tmp"
    stale_tmp.write_bytes(b"partial crash leftover")

    result = reconcile(conn)

    assert ("visits", "2026-05-21") in result
    assert ("visits", "2026-05-20") not in result  # .tmp ≠ партиция
    # glob *.parquet по суффиксу не матчит .parquet.tmp — фиксируем границу.
    assert "2026-05-20" not in {p.stem for p in source_dir.glob("*.parquet")}


# --- count_partition_rows: контракт факта (нет файла / здоровый / битый) -----------------


def test_count_partition_rows_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Нет файла → None; здоровый → N; битый → None без исключения (AC #5, риск №3)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    # Файла нет.
    assert count_partition_rows(conn, "visits", "2026-05-20") is None

    # Здоровый файл.
    _write_visits("2026-05-20", _VISITS_ROWS)
    assert count_partition_rows(conn, "visits", "2026-05-20") == 2

    # Битый файл → None, не исключение.
    corrupt_path = get_raw_partition_path("visits", "2026-05-19")
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"garbage bytes")
    assert count_partition_rows(conn, "visits", "2026-05-19") is None


# --- Анти-зависимость: duckdb разрешён; нет тяжёлого стека/инфры/сцепки (риск №7) --------


def test_no_forbidden_imports_and_no_coupling() -> None:
    """Нет pandas/polars/numpy/pyarrow и directaiq-инфры; нет импорта parquet_store/database_manager.

    По реальным import-узлам через ``ast`` (не подстрока — docstring упоминает соседние
    модули). ``duckdb`` РАЗРЕШЁН (в отличие от ``row_check`` 2.3 — ``load_state`` штатно
    работает с БД). Риск №7: модуль НЕ импортирует ``parquet_store``/``database_manager``,
    даже если тест-фикстура использует ``write_partition``.
    """
    import scripts.utils.load_state as mod

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

    forbidden = {"pandas", "polars", "numpy", "pyarrow", "config_manager", "base_script", "auth_manager"}
    import_offenders = {n for n in imported if n.split(".")[0] in forbidden}
    assert not import_offenders, f"запрещённые импорты в load_state: {import_offenders}"

    # duckdb — штатная зависимость модуля (БД).
    assert "duckdb" in imported

    # Риск №7: нулевая сцепка по коду с записью/открытием БД.
    assert "scripts.utils.parquet_store" not in imported
    assert "scripts.utils.database_manager" not in imported

    # Вырезанная directaiq-инфра не упоминается.
    cut_infra = {"register_udfs", "schema_migrations", "migrations"}
    infra_offenders = cut_infra & referenced_names
    assert not infra_offenders, f"ссылки на вырезанную directaiq-инфру: {infra_offenders}"
