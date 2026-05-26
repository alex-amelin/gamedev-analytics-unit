"""Offline-тесты загрузчика каталога схемы (история 1.5).

Покрывают дисциплину каталога-SSOT, а не только happy-path: срезы по источнику
(AC #1), список полей выгрузки FR-2 (AC #2), обязательные поля + невалидный source
(AC #3), маппинг ClickHouse→DuckDB вкл. расширения SMALLINT/DOUBLE и Array(T) (AC #4),
**реальный каталог без потерь + HUGEINT-id + прод-ветка пути** (AC #5), RFC4180-парсинг
на запятой/«;»/кавычке (AC #6), дубли в источнике и кросс-источник как НЕ-дубль (AC #7),
fail-loud на неизвестном типе/чужом префиксе/отсутствии файла/дрейфе заголовка (AC #8)
и запрет тяжёлых зависимостей (по реальным import-узлам через ``ast``, не по подстроке).

Без сети: мини-каталоги пишутся в ``tmp_path`` через ``csv.writer`` (корректное
RFC4180-квотирование), путь инъектируется параметром ``path``. Один интеграционный
тест (AC #5) грузит реальный ``development-docs/schema-catalog.csv`` дефолтным путём —
закрывает прод-ветку резолюции от модуля и инвариант «без потерь».
"""

from __future__ import annotations

import ast
import csv
from pathlib import Path

import pytest

from scripts.utils.catalog import (
    CLICKHOUSE_TO_DUCKDB,
    DEFAULT_CATALOG_PATH,
    duckdb_type_for,
    load_catalog,
)

HEADER = ["source", "storage_name", "metrica_field", "type", "description"]

# Минимальные валидные строки на источник — база для негативных мутаций.
_VISITS_ROWS = [
    ["visits", "visit_id", "ym:s:visitID", "HUGEINT", "Идентификатор визита"],
    ["visits", "date", "ym:s:date", "DATE", "Дата визита"],
    ["visits", "start_url", "ym:s:startURL", "VARCHAR", "Страница входа"],
]
_HITS_ROWS = [
    ["hits", "watch_id", "ym:pv:watchID", "HUGEINT", "Идентификатор события"],
    ["hits", "url", "ym:pv:URL", "VARCHAR", "Адрес страницы"],
]


def _write_catalog(
    path: Path, rows: list[list[str]], *, header: list[str] | None = None
) -> Path:
    """Записать CSV-каталог в ``path`` через ``csv.writer`` (RFC4180-квотирование)."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header if header is not None else HEADER)
        writer.writerows(rows)
    return path


# --- AC #1: срезы по источнику несут storage_name/metrica_field/duckdb_type -----


def test_fields_for_carries_field_attributes(tmp_path: Path) -> None:
    """fields_for(source) отдаёт поля источника с верными атрибутами (AC #1)."""
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", _VISITS_ROWS + _HITS_ROWS))

    visits = catalog.fields_for("visits")
    assert len(visits) == 3
    first = visits[0]
    assert first.storage_name == "visit_id"
    assert first.metrica_field == "ym:s:visitID"
    assert first.duckdb_type == "HUGEINT"
    assert first.description == "Идентификатор визита"


def test_duckdb_types_returns_storage_to_type_map(tmp_path: Path) -> None:
    """duckdb_types(source) = ожидаемый dict storage_name → тип (AC #1; для views.py 2.6)."""
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", _VISITS_ROWS + _HITS_ROWS))

    assert catalog.duckdb_types("hits") == {
        "watch_id": "HUGEINT",
        "url": "VARCHAR",
    }


# --- 3.3: Catalog.descriptions — семантика колонок для MCP-контекста (FR-18) ----


def test_descriptions_returns_storage_to_description_map(tmp_path: Path) -> None:
    """descriptions(source) = dict storage_name → description источника (3.3; для MCP-контекста)."""
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", _VISITS_ROWS + _HITS_ROWS))

    assert catalog.descriptions("hits") == {
        "watch_id": "Идентификатор события",
        "url": "Адрес страницы",
    }


def test_descriptions_invalid_source_raises(tmp_path: Path) -> None:
    """Невалидный source у descriptions → ValueError (наследуется из fields_for, 3.3)."""
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", _VISITS_ROWS))
    with pytest.raises(ValueError, match="sessions"):
        catalog.descriptions("sessions")


def test_descriptions_empty_description_preserved(tmp_path: Path) -> None:
    """Пустой description сохраняется как пустая строка (потребитель трактует как unknown, 3.3)."""
    rows = [["visits", "visit_id", "ym:s:visitID", "HUGEINT", ""]]
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", rows))

    assert catalog.descriptions("visits") == {"visit_id": ""}


# --- AC #2 (FR-2): список metrica_field источника, без хардкода «на всё» --------


def test_metrica_fields_exact_order_and_isolation(tmp_path: Path) -> None:
    """metrica_fields(source) = точный список ym:*-полей в порядке строк, источники разделены (AC #2)."""
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", _VISITS_ROWS + _HITS_ROWS))

    assert catalog.metrica_fields("visits") == [
        "ym:s:visitID",
        "ym:s:date",
        "ym:s:startURL",
    ]
    hits_fields = catalog.metrica_fields("hits")
    assert hits_fields == ["ym:pv:watchID", "ym:pv:URL"]
    # Нет хардкода «на всё»: visits-поля не протекают в hits-список.
    assert not any(f.startswith("ym:s:") for f in hits_fields)


def test_metrica_fields_is_list_for_create_log_request(tmp_path: Path) -> None:
    """metrica_fields отдаёт именно list[str] — формат под create_log_request(fields=...) (AC #2)."""
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", _VISITS_ROWS))
    fields = catalog.metrica_fields("visits")

    assert isinstance(fields, list)
    assert all(isinstance(f, str) for f in fields)


# --- AC #3: обязательные поля + невалидный source -> fail-loud -----------------


def test_empty_storage_name_raises(tmp_path: Path) -> None:
    """Пустой storage_name (поле без записи) → ValueError с номером строки (AC #3)."""
    rows = [["visits", "", "ym:s:visitID", "HUGEINT", "desc"]]
    with pytest.raises(ValueError, match="storage_name"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_empty_type_raises(tmp_path: Path) -> None:
    """Пустой type → ValueError (обязательная колонка, AC #3)."""
    rows = [["visits", "visit_id", "ym:s:visitID", "", "desc"]]
    with pytest.raises(ValueError, match="type"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_unknown_source_raises(tmp_path: Path) -> None:
    """source вне {visits,hits} → ValueError (AC #3)."""
    rows = [["sessions", "visit_id", "ym:s:visitID", "HUGEINT", "desc"]]
    with pytest.raises(ValueError, match="sessions"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_empty_description_is_allowed(tmp_path: Path) -> None:
    """Пустой description допустим — это семантика, не структура (AC #3)."""
    rows = [["visits", "visit_id", "ym:s:visitID", "HUGEINT", ""]]
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", rows))

    assert catalog.fields_for("visits")[0].description == ""


# --- AC #4: маппинг ClickHouse→DuckDB (типы не угадываются) --------------------


@pytest.mark.parametrize(
    ("clickhouse", "duckdb"),
    [
        ("UInt64", "HUGEINT"),
        ("UInt32", "BIGINT"),
        ("Int64", "BIGINT"),
        ("Int32", "INTEGER"),
        ("Int16", "SMALLINT"),
        ("UInt8", "BOOLEAN"),
        ("Float64", "DOUBLE"),
        ("Date", "DATE"),
        ("DateTime", "TIMESTAMP"),
        ("String", "VARCHAR"),
        ("Array(UInt64)", "HUGEINT[]"),
        ("Array(String)", "VARCHAR[]"),
        ("Array(Float64)", "DOUBLE[]"),
    ],
)
def test_duckdb_type_for_mapping(clickhouse: str, duckdb: str) -> None:
    """duckdb_type_for переводит справочный тип в DuckDB по таблице, вкл. Array(T) (AC #4)."""
    assert duckdb_type_for(clickhouse) == duckdb


def test_duckdb_type_for_unknown_raises() -> None:
    """Неизвестный ClickHouse-тип → ValueError с самим типом (AC #4, #8)."""
    with pytest.raises(ValueError, match="Decimal"):
        duckdb_type_for("Decimal")


def test_mapping_codomain_includes_smallint_and_double() -> None:
    """Кодомен маппинга содержит SMALLINT и DOUBLE — иначе реальный каталог упал бы (риск #2)."""
    codomain = set(CLICKHOUSE_TO_DUCKDB.values())
    assert "SMALLINT" in codomain
    assert "DOUBLE" in codomain


# --- AC #5: реальный каталог — интеграционный смоук (прод-ветка пути) ----------


def test_real_catalog_loads_without_loss() -> None:
    """Реальный каталог грузится дефолтным путём, без потерь, HUGEINT-id (AC #5).

    Прогон БЕЗ аргумента покрывает прод-ветку резолюции пути от модуля (риск #4).
    «Без потерь» сверяется с сырым числом строк данных файла (устойчиво к росту),
    плюс зафиксирован текущий снимок 73 visits + 41 hits = 114.
    """
    catalog = load_catalog()

    # Сырое число строк данных файла = всего полей (ничего не потеряно при парсинге).
    with DEFAULT_CATALOG_PATH.open(newline="", encoding="utf-8") as handle:
        raw_data_rows = sum(1 for _ in csv.reader(handle)) - 1  # минус заголовок
    assert len(catalog.fields) == raw_data_rows

    assert len(catalog.fields_for("visits")) == 73
    assert len(catalog.fields_for("hits")) == 41
    assert len(catalog.fields) == 114

    visits_types = catalog.duckdb_types("visits")
    hits_types = catalog.duckdb_types("hits")
    assert visits_types["visit_id"] == "HUGEINT"
    assert visits_types["client_id"] == "HUGEINT"
    assert visits_types["watch_ids"] == "HUGEINT[]"
    assert hits_types["watch_id"] == "HUGEINT"
    assert hits_types["client_id"] == "HUGEINT"


# --- AC #6: RFC4180-парсинг — описания с запятой/«;»/кавычкой не рвут строку ----


def test_rfc4180_description_with_commas_and_quotes(tmp_path: Path) -> None:
    """Описание с запятой/«;»/кавычкой парсится целиком, соседние поля не съезжают (AC #6).

    Доказывает использование csv-парсера вместо наивного split(",") — split порвал бы
    строку на запятой внутри описания и сдвинул бы duckdb_type/description.
    """
    tricky = 'Идентификатор визита, уникален; "альфа"'
    rows = [["visits", "visit_id", "ym:s:visitID", "HUGEINT", tricky]]
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", rows))

    field = catalog.fields_for("visits")[0]
    assert field.description == tricky  # описание целое, запятая не разбила
    assert field.duckdb_type == "HUGEINT"  # тип НЕ съехал в описание
    assert field.metrica_field == "ym:s:visitID"


# --- AC #7: дубли в источнике -> fail-loud; кросс-источник -> НЕ дубль ----------


def test_duplicate_storage_name_in_source_raises(tmp_path: Path) -> None:
    """Два visits-поля с одним storage_name → ValueError (коллизия DDL, AC #7)."""
    rows = [
        ["visits", "visit_id", "ym:s:visitID", "HUGEINT", "a"],
        ["visits", "visit_id", "ym:s:date", "DATE", "b"],
    ]
    with pytest.raises(ValueError, match="дубль storage_name"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_duplicate_metrica_field_in_source_raises(tmp_path: Path) -> None:
    """Два visits-поля с одним metrica_field → ValueError (AC #7)."""
    rows = [
        ["visits", "visit_id", "ym:s:visitID", "HUGEINT", "a"],
        ["visits", "other", "ym:s:visitID", "VARCHAR", "b"],
    ]
    with pytest.raises(ValueError, match="дубль metrica_field"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_same_storage_name_across_sources_is_not_duplicate(tmp_path: Path) -> None:
    """client_id легально есть и в visits, и в hits — это НЕ дубль (AC #7)."""
    rows = [
        ["visits", "client_id", "ym:s:clientID", "HUGEINT", "a"],
        ["hits", "client_id", "ym:pv:clientID", "HUGEINT", "b"],
    ]
    catalog = load_catalog(_write_catalog(tmp_path / "c.csv", rows))

    assert catalog.duckdb_types("visits")["client_id"] == "HUGEINT"
    assert catalog.duckdb_types("hits")["client_id"] == "HUGEINT"


# --- AC #8: неизвестный тип / чужой префикс / нет файла / дрейф заголовка -------


def test_unknown_duckdb_type_raises(tmp_path: Path) -> None:
    """type вне набора DuckDB-типов → ValueError (AC #8)."""
    rows = [["visits", "visit_id", "ym:s:visitID", "FOOBAR", "desc"]]
    with pytest.raises(ValueError, match="FOOBAR"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_raw_clickhouse_type_in_catalog_raises(tmp_path: Path) -> None:
    """Сырой ClickHouse-тип (UInt64) просочился в каталог → ValueError (AC #8).

    Каталог хранит DuckDB-типы; UInt64 — это вход маппинга, а не DuckDB-тип.
    """
    rows = [["visits", "visit_id", "ym:s:visitID", "UInt64", "desc"]]
    with pytest.raises(ValueError, match="UInt64"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_wrong_prefix_for_source_raises(tmp_path: Path) -> None:
    """visits-строка с hits-префиксом ym:pv: → ValueError (AC #8)."""
    rows = [["visits", "foo", "ym:pv:foo", "VARCHAR", "desc"]]
    with pytest.raises(ValueError, match="ym:s:"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_missing_file_raises() -> None:
    """Отсутствующий файл каталога → ValueError с путём (AC #8)."""
    missing = Path("nonexistent-catalog-xyz.csv")
    with pytest.raises(ValueError, match="nonexistent-catalog-xyz"):
        load_catalog(missing)


def test_header_drift_working_type_raises(tmp_path: Path) -> None:
    """Заголовок с working_type вместо type → ValueError про колонки (риск #1, AC #8)."""
    bad_header = ["source", "storage_name", "metrica_field", "working_type", "description"]
    rows = [["visits", "visit_id", "ym:s:visitID", "HUGEINT", "desc"]]
    with pytest.raises(ValueError, match="working_type"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows, header=bad_header))


# --- Review-патчи: лишние колонки / пустой каталог / формат storage_name -------


def test_extra_columns_in_row_raises(tmp_path: Path) -> None:
    """Строка с колонок БОЛЬШЕ заголовка → ValueError (не теряем хвост молча).

    csv.DictReader складывает избыток под restkey; без проверки незакавыченная запятая
    в описании сдвинула бы поля и потеряла хвост незаметно (нарушение fail-loud).
    """
    rows = [["visits", "visit_id", "ym:s:visitID", "HUGEINT", "desc", "ЛИШНЕЕ"]]
    with pytest.raises(ValueError, match="колонок больше"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_empty_catalog_raises(tmp_path: Path) -> None:
    """Валидный заголовок, ноль строк данных → ValueError (пустой SSOT = дефект)."""
    with pytest.raises(ValueError, match="пуст"):
        load_catalog(_write_catalog(tmp_path / "c.csv", []))


def test_storage_name_with_internal_space_raises(tmp_path: Path) -> None:
    """storage_name с внутренним пробелом → ValueError (формат snake_case)."""
    rows = [["visits", "visit id", "ym:s:visitID", "HUGEINT", "desc"]]
    with pytest.raises(ValueError, match="snake_case"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


def test_storage_name_uppercase_raises(tmp_path: Path) -> None:
    """storage_name с заглавной → ValueError (формат snake_case)."""
    rows = [["visits", "VisitId", "ym:s:visitID", "HUGEINT", "desc"]]
    with pytest.raises(ValueError, match="snake_case"):
        load_catalog(_write_catalog(tmp_path / "c.csv", rows))


# --- Анти-зависимость: модуль не тянет pandas/polars/numpy/yaml (NFR-6) --------


def test_no_heavy_dependencies_imported() -> None:
    """Среди реальных import-узлов нет pandas/polars/numpy/yaml (stdlib-only, NFR-6).

    Намеренно НЕ по подстроке: docstring/комментарии могут упоминать эти имена —
    наивный поиск дал бы ложный красный. Парсим AST и смотрим Import/ImportFrom-узлы.
    """
    import scripts.utils.catalog as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            # `from pkg import numpy` — само импортируемое имя может быть тяжёлым модулем,
            # не только node.module. Сканируем и имена тоже, иначе обход теста.
            imported.update(alias.name for alias in node.names)

    # Сравниваем TOP-LEVEL пакет (не подстроку): иначе легальный `geopandas`/`typedyaml`
    # дал бы ложный красный (содержит `pandas`/`yaml` как подстроку).
    forbidden = {"pandas", "polars", "numpy", "yaml"}
    offenders = {name for name in imported if name.split(".")[0] in forbidden}
    assert not offenders, f"запрещённые тяжёлые импорты в catalog: {offenders}"
