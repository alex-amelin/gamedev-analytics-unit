"""Offline-тесты рабочего слоя — типизированные view'ы ``visits``/``hits`` (история 2.6).

Покрывают весь контракт FR-7/FR-3, не только happy-path: чистый билдер DDL без БД
(``CREATE OR REPLACE``, ``TRY_CAST``, ``union_by_name``, квотированные snake_case-имена,
форма пустого источника, экранирование пути) и интеграцию на tmp-партициях через живой
in-memory DuckDB: типизация скаляров/ID HUGEINT/массивов в ``LIST`` (AC #1/#3/#8), битая
ячейка → ``NULL`` без падения view (AC #2), ленивость view — перезалив виден без
пере-создания (AC #4), дрейф схемы между партициями через ``union_by_name`` (AC #7),
пустой источник → пустой типизированный view не валит другой + осиротевший ``.tmp`` не
партиция (AC #6), наследуемый fail-loud битого корня и анти-зависимость по реальным
import-узлам через ``ast`` (``duckdb`` РАЗРЕШЁН; нет pandas/polars/numpy/pyarrow и нет
импорта database_manager/parquet_store/load_state/writer_lock — риск №6).

Без сети, без внешнего API. DuckDB локален (in-memory conn); партиции-фикстуры пишутся
реальным ``parquet_store.write_partition`` (2.2) как хелпером (в модуль views.py он НЕ
импортируется — проверяется ast-тестом). Кросс-платформенно (``tmp_path``/``pathlib``;
glob/``union_by_name``/``as_posix`` обязаны работать на ubuntu и windows). Live-набор
осознанно отсутствует: ``views.py`` в сеть не ходит — генерирует DDL и читает локальный
Parquet ([[realapi-smoke-tests]] — opt-in live только для внешнего Logs API; реальный
формат TSV-массива подтверждает live-smoke оркестратора 2.7, фикстуры освежить из ответа).
"""

from __future__ import annotations

import ast
from pathlib import Path

import duckdb
import pytest

from scripts.utils.catalog import Catalog, CatalogField
from scripts.utils.env_reader import DATA_ROOT_ENV
from scripts.utils.parquet_store import write_partition
from scripts.utils.paths import get_raw_source_dir
from scripts.utils.views import build_view_ddl, create_views


def _catalog() -> Catalog:
    """Мини-каталог: visits (скаляры + ID HUGEINT + массив HUGEINT[]) и hits (массив BIGINT[]).

    Оба источника присутствуют — ``create_views`` по умолчанию строит ``visits`` и ``hits``,
    и для каждого нужны поля каталога. Типы реалистичны (взяты из schema-catalog.csv).
    """
    return Catalog(
        fields=(
            CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", "Идентификатор визита"),
            CatalogField("visits", "client_id", "ym:s:clientID", "HUGEINT", "Аноним. идентификатор"),
            CatalogField("visits", "watch_ids", "ym:s:watchIDs", "HUGEINT[]", "Просмотры визита"),
            CatalogField("visits", "date", "ym:s:date", "DATE", "Дата визита"),
            CatalogField("visits", "page_views", "ym:s:pageViews", "INTEGER", "Глубина просмотра"),
            CatalogField("hits", "watch_id", "ym:pv:watchID", "HUGEINT", "Идентификатор события"),
            CatalogField("hits", "goals_id", "ym:pv:goalsID", "BIGINT[]", "Номера целей"),
            CatalogField("hits", "date", "ym:pv:date", "DATE", "Дата события"),
        )
    )


_VISITS_COLUMNS = [
    "ym:s:visitID",
    "ym:s:clientID",
    "ym:s:watchIDs",
    "ym:s:date",
    "ym:s:pageViews",
]


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    """In-memory соединение (инъекция как в 2.1/2.4; views.py БД сам не открывает)."""
    connection = duckdb.connect()
    try:
        yield connection
    finally:
        connection.close()


def _write_visits(rows: list[list[str | None]], date: str = "2026-05-20") -> int:
    """Записать партицию visits дня реальным ``write_partition`` (2.2) — хелпер фикстуры."""
    return write_partition("visits", date, _VISITS_COLUMNS, rows, catalog=_catalog())


# --- Чистый билдер DDL (без БД) — главный тестируемый шов --------------------------------


def test_build_view_ddl_non_empty_source_shape() -> None:
    """Непустой источник: CREATE OR REPLACE + TRY_CAST + union_by_name + типы + snake_case (AC #1/#3/#5/#7)."""
    ddl = build_view_ddl(
        "visits",
        _catalog(),
        partition_glob="/storage/data/raw/visits/*.parquet",
        has_partitions=True,
    )

    assert 'CREATE OR REPLACE VIEW "visits" AS' in ddl  # имя view квотировано (риск №8)
    assert "TRY_CAST" in ddl
    assert "union_by_name => true" in ddl
    assert "read_parquet('/storage/data/raw/visits/*.parquet'" in ddl
    # Типы из каталога — в т.ч. HUGEINT (ID, AC #3), DATE, массив HUGEINT[] (AC #8).
    assert "HUGEINT" in ddl
    assert "HUGEINT[]" in ddl
    assert "DATE" in ddl
    # Квотированные snake_case storage-имена (AC #5).
    assert '"visit_id"' in ddl
    assert '"watch_ids"' in ddl
    # Родные имена Метрики во view НЕ появляются (AC #5, инвариант).
    assert "ym:s:" not in ddl


def test_build_view_ddl_empty_source_shape() -> None:
    """Пустой источник: типизированная проекция CAST(NULL AS ...) + WHERE false, без read_parquet (AC #6)."""
    ddl = build_view_ddl(
        "hits",
        _catalog(),
        partition_glob="/storage/data/raw/hits/*.parquet",
        has_partitions=False,
    )

    assert 'CREATE OR REPLACE VIEW "hits" AS' in ddl  # имя view квотировано (риск №8)
    assert "WHERE false" in ddl
    assert "CAST(NULL AS HUGEINT)" in ddl
    assert "CAST(NULL AS BIGINT[])" in ddl
    # Пустой источник не зовёт read_parquet (иначе «No files found» при запросе).
    assert "read_parquet" not in ddl
    assert '"watch_id"' in ddl
    assert "ym:pv:" not in ddl


def test_build_view_ddl_escapes_single_quote_in_path() -> None:
    """Одинарная кавычка в пути партиций экранируется удвоением (риск №2, дисциплина SQL)."""
    ddl = build_view_ddl(
        "visits",
        _catalog(),
        partition_glob="/data/o'brien/raw/visits/*.parquet",
        has_partitions=True,
    )

    # Кавычка удвоена внутри строкового литерала — путь не «вырывается» из строки SQL.
    assert "o''brien" in ddl
    assert "o'brien'" not in ddl


def test_build_view_ddl_rejects_invalid_source() -> None:
    """Невалидный источник → ValueError (наследуется из catalog.duckdb_types, fail-loud)."""
    with pytest.raises(ValueError, match="source"):
        build_view_ddl(
            "sessions",
            _catalog(),
            partition_glob="/x/*.parquet",
            has_partitions=True,
        )


def test_build_view_ddl_rejects_source_without_fields() -> None:
    """Источник без полей в каталоге → ValueError (вырожденный SSOT = дефект, строгий вариант)."""
    visits_only = Catalog(
        fields=(CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", ""),)
    )
    with pytest.raises(ValueError, match="нет полей"):
        build_view_ddl(
            "hits",
            visits_only,
            partition_glob="/x/*.parquet",
            has_partitions=False,
        )


# --- AC #1 + #3 + #5: интеграция, типы (HUGEINT/LIST/DATE), snake_case ------------------


def test_views_typing_and_no_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """create_views → visits отдаёт HUGEINT/LIST/DATE; ID > 2^63 не переполняется; snake_case (AC #1/#3/#5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    # visit_id > 2^63 (≈9.2e18) — влезает только в HUGEINT (NFR-4).
    _write_visits(
        [["17298374650000000001", "100", "[8273645,8273646]", "2026-05-20", "5"]]
    )
    create_views(conn, catalog=_catalog())

    type_row = conn.execute(
        "SELECT typeof(visit_id), typeof(watch_ids), typeof(date) FROM visits LIMIT 1"
    ).fetchone()
    assert type_row == ("HUGEINT", "HUGEINT[]", "DATE")

    # Значение за пределами BIGINT не теряется/не переполняется (HUGEINT, AC #3).
    value_row = conn.execute(
        "SELECT visit_id FROM visits WHERE visit_id = 17298374650000000001"
    ).fetchone()
    assert value_row is not None
    assert value_row[0] == 17298374650000000001


def test_views_array_typed_as_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Массив `[v1,v2]`/`[]`/мусор → LIST/пустой LIST/NULL (AC #8, вариант A — нативный TRY_CAST)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    _write_visits(
        [
            ["1", "100", "[8273645,8273646]", "2026-05-20", "1"],  # многоэлементный
            ["2", "200", "[]", "2026-05-20", "1"],  # пустой массив
            ["3", "300", "garbage", "2026-05-20", "1"],  # битый → NULL
        ]
    )
    create_views(conn, catalog=_catalog())

    # Многоэлементный массив → LIST из 2 элементов.
    assert conn.execute("SELECT len(watch_ids) FROM visits WHERE visit_id = 1").fetchone()[0] == 2
    # Пустой `[]` → ПУСТОЙ список (вариант A), а НЕ [NULL] (дефект варианта B).
    assert conn.execute("SELECT len(watch_ids) FROM visits WHERE visit_id = 2").fetchone()[0] == 0
    # Битая строка-массив → NULL (битая ячейка, контракт FR-7).
    assert conn.execute("SELECT watch_ids FROM visits WHERE visit_id = 3").fetchone()[0] is None


# --- AC #2: битая ячейка → NULL, view не падает -----------------------------------------


def test_corrupt_cell_becomes_null_view_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Битое значение в типизируемой колонке → NULL; соседние валидные строки целы (AC #2)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    _write_visits(
        [
            ["1", "100", "[1]", "2026-05-20", "5"],  # валидная строка
            ["2", "200", "[2]", "not-a-date", "abc"],  # date и page_views битые
        ]
    )
    create_views(conn, catalog=_catalog())

    rows = conn.execute(
        "SELECT visit_id, date, page_views FROM visits ORDER BY visit_id"
    ).fetchall()
    # View не упал; обе строки на месте.
    assert len(rows) == 2
    # Валидная строка целая.
    assert rows[0][2] == 5
    # Битые ячейки → NULL, но строка читается (день не падает).
    assert rows[1][1] is None  # date='not-a-date' → NULL
    assert rows[1][2] is None  # page_views='abc' → NULL


# --- AC #4: перезалив отражается без материализации (ленивость view) --------------------


def test_view_reflects_repartition_without_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Перезапись партиции (2.2 os.replace) виден ТЕМ ЖЕ view без пере-создания (AC #4, OQ#3)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    _write_visits([["1", "1", "[1]", "2026-05-20", "1"]])  # 1 строка
    create_views(conn, catalog=_catalog())
    assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 1

    # Перезалив того же дня (другое число строк) — view НЕ пере-создаём.
    _write_visits(
        [
            ["1", "1", "[1]", "2026-05-20", "1"],
            ["2", "2", "[2]", "2026-05-20", "2"],
            ["3", "3", "[3]", "2026-05-20", "3"],
        ]
    )
    assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 3


# --- AC #7: дрейф схемы между партициями → union_by_name --------------------------------


def test_schema_drift_union_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Новое поле есть только в свежей партиции → старые строки NULL, обе партиции читаются (AC #7)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    # Каталог из 2 полей: каждое поле присутствует хотя бы в одной партиции (граница риска №4).
    drift_catalog = Catalog(
        fields=(
            CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", ""),
            CatalogField("visits", "page_views", "ym:s:pageViews", "INTEGER", ""),
        )
    )
    # Старая партиция БЕЗ page_views.
    write_partition(
        "visits", "2026-05-20", ["ym:s:visitID"], [["1"]], catalog=drift_catalog
    )
    # Свежая партиция С page_views.
    write_partition(
        "visits",
        "2026-05-21",
        ["ym:s:visitID", "ym:s:pageViews"],
        [["2", "42"]],
        catalog=drift_catalog,
    )
    create_views(conn, catalog=drift_catalog, sources=("visits",))

    rows = dict(
        conn.execute("SELECT visit_id, page_views FROM visits").fetchall()
    )
    # Обе строки читаются (нет ошибки позиционного матча).
    assert set(rows) == {1, 2}
    # Старая партиция без поля → NULL; свежая → реальное значение.
    assert rows[1] is None
    assert rows[2] == 42


# --- AC #6: пустой источник → пустой типизированный view, не валит другой ---------------


def test_empty_source_makes_typed_empty_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Нет ни одной партиции → пустой ТИПИЗИРОВАННЫЙ view (0 строк, типы колонок корректны) (AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    create_views(conn, catalog=_catalog())

    # Оба источника пусты → оба view существуют и пусты.
    assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM hits").fetchone()[0] == 0

    # Типы колонок корректны даже на 0 строк (через DESCRIBE — потребитель 3.3 видит штатно).
    described = dict(
        (row[0], row[1]) for row in conn.execute("DESCRIBE hits").fetchall()
    )
    assert described["watch_id"] == "HUGEINT"
    assert described["goals_id"] == "BIGINT[]"
    assert described["date"] == "DATE"


def test_one_empty_source_does_not_break_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """visits есть, hits пуст → visits отдаёт строки, hits пустой типизированный (граница FR-3, AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    _write_visits([["1", "100", "[1]", "2026-05-20", "5"]])  # только visits
    create_views(conn, catalog=_catalog())

    assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM hits").fetchone()[0] == 0  # пустой не валит другой


def test_orphan_tmp_is_not_a_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Осиротевший ``{date}.parquet.tmp`` (без .parquet) → источник считается пустым (риск №3/№5, AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    source_dir = get_raw_source_dir("visits")
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "2026-05-20.parquet.tmp").write_bytes(b"partial crash leftover")

    create_views(conn, catalog=_catalog())

    # .tmp не матчится glob *.parquet → visits — пустой типизированный view (не падает на чтении).
    assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 0


def test_directory_named_parquet_is_not_a_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Каталог с именем ``{date}.parquet`` → НЕ партиция (фильтр is_file, как load_state; AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    source_dir = get_raw_source_dir("visits")
    source_dir.mkdir(parents=True, exist_ok=True)
    # КАТАЛОГ (не файл) с именем партиции — glob('*.parquet') матчит его по имени.
    (source_dir / "2026-05-20.parquet").mkdir()

    create_views(conn, catalog=_catalog())

    # is_file отсекает каталог → источник пуст → типизированный пустой view (read_parquet
    # не зовётся, иначе была бы IOException при запросе — расхождение с веткой пустого источника).
    assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 0


# --- Битый корень: fail-loud наследуется из paths (2.1) ---------------------------------


def test_broken_root_fails_loud(
    monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Нет GDAU_DATA_ROOT → ValueError из get_raw_source_dir/get_storage_root (наследуется, без побочек)."""
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)

    with pytest.raises(ValueError):
        create_views(conn, catalog=_catalog())


# --- Патчи code-review: атомарность набора + glob-метасимволы корня ----------------------


def test_create_views_atomic_on_source_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Падение на втором источнике → НИ ОДНОГО view (все DDL собираются до первого execute)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    # Каталог с полями только для visits: build_view_ddl("hits") бросит ValueError.
    visits_only = Catalog(
        fields=(CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", ""),)
    )

    with pytest.raises(ValueError, match="нет полей"):
        create_views(conn, catalog=visits_only, sources=("visits", "hits"))

    # visits НЕ создан, хотя в порядке шёл первым: набор атомарен (нет частичного слоя).
    with pytest.raises(duckdb.Error):
        conn.execute("SELECT * FROM visits")


def test_glob_metachars_in_root_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conn: duckdb.DuckDBPyConnection
) -> None:
    """Glob-метасимволы в корне хранилища ('…/game[1]/…') экранируются → партиция находится."""
    bracket_root = tmp_path / "game[1]"  # '[1]' — иначе класс символов для read_parquet
    bracket_root.mkdir()
    monkeypatch.setenv(DATA_ROOT_ENV, str(bracket_root))

    _write_visits([["1", "100", "[1]", "2026-05-20", "5"]])
    create_views(conn, catalog=_catalog())

    # Без glob.escape read_parquet истолковал бы '[1]' как класс → файл не найден (0/ошибка).
    assert conn.execute("SELECT count(*) FROM visits").fetchone()[0] == 1


# --- Анти-зависимость: duckdb разрешён; нет тяжёлого стека и сцепки (риск №6) -----------


def test_no_forbidden_imports_and_no_coupling() -> None:
    """Нет pandas/polars/numpy/pyarrow и directaiq-инфры; нет импорта БД-модулей записи (риск №6).

    По реальным import-узлам через ``ast`` (не подстрока — docstring упоминает соседние
    модули). ``duckdb`` РАЗРЕШЁН (как 2.4 — нужен для аннотации ``conn``). Риск №6: модуль
    НЕ открывает gdau.duckdb и НЕ импортирует database_manager/parquet_store/load_state/
    writer_lock, даже если тест-фикстура использует write_partition.
    """
    import scripts.utils.views as mod

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

    forbidden = {"pandas", "polars", "numpy", "pyarrow", "config_manager", "base_script"}
    import_offenders = {n for n in imported if n.split(".")[0] in forbidden}
    assert not import_offenders, f"запрещённые импорты в views: {import_offenders}"

    # duckdb — штатная зависимость (аннотация conn), как в load_state 2.4.
    assert "duckdb" in imported

    # Риск №6: нулевая сцепка по коду с записью/открытием БД и соседними слоями.
    for coupled in (
        "scripts.utils.database_manager",
        "scripts.utils.parquet_store",
        "scripts.utils.load_state",
        "scripts.utils.writer_lock",
    ):
        assert coupled not in imported, f"views не должен импортировать {coupled}"
