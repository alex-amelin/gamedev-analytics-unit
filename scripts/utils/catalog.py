"""Загрузчик каталога схемы — единственная программная точка к SSOT.

Каталог ``development-docs/schema-catalog.csv`` — единый источник истины (FR-16):
какие поля Метрики мы выгружаем (родное имя ``ym:s:*``/``ym:pv:*``), под каким
storage-именем (``snake_case``) они ложатся и какой у них DuckDB-тип. Этот модуль
читает каталог, валидирует его инварианты (fail-loud) и отдаёт удобные срезы по
источнику. Так список полей, типы и семантика берутся из ОДНОГО места — выгрузка,
типизация view'ов и контекст MCP не разъезжаются.

Потребители (реализуются в своих историях, не здесь):
:func:`Catalog.metrica_fields` — CLI ``create`` (1.6) и оркестратор p81 (2.7) для
списка полей выгрузки (FR-2); :func:`Catalog.duckdb_types` — ``views.py`` (2.6) для
DDL view'ов с ``TRY_CAST``; :func:`Catalog.fields_for` — MCP-контекст (3.3) для
семантики колонок. Это НЕ вендоринг: в directaiq каталога-CSV не было — наша
собственная конструкция на чистой stdlib ``csv``. Сетей/DuckDB/путей хранилища
модуль не знает; в каталог не пишет.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Точный ожидаемый заголовок каталога. Валидация на этот кортеж ловит дрейф колонок
# fail-loud — в частности устаревшее имя `working_type` из ранних версий
# architecture.md (источник истины — реальный файл + epics AC #1: колонка зовётся `type`).
CATALOG_COLUMNS = ("source", "storage_name", "metrica_field", "type", "description")

# Источники Logs API, которые юнит принимает (visits + hits — осознанное решение PRD).
VALID_SOURCES = ("visits", "hits")

# Родной префикс имени поля Logs API на источник. Проверяется на каждой строке:
# поле visits обязано начинаться с `ym:s:`, поле hits — с `ym:pv:` (AC #8).
SOURCE_PREFIXES = {"visits": "ym:s:", "hits": "ym:pv:"}

# Дефолтный путь к каталогу резолвится ОТ МОДУЛЯ, а не от cwd: каталог — артефакт
# dev-репо (`development-docs/`), он путешествует с кодом (в per-game хранилище
# приходит симлинком), а НЕ данные оператора (для них — GDAU_DATA_ROOT/.env, другая
# зона). `.resolve()` проходит сквозь симлинк хранилища в dev-репо, где `scripts/` и
# `development-docs/` — реальные соседи. parents[2]: catalog.py → utils → scripts → корень.
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "development-docs" / "schema-catalog.csv"
)

# Таблица происхождения типов: ClickHouse (справочник Logs API) → DuckDB. Типы НЕ
# угадываются — сидятся отсюда (AC #4). Помимо таблицы AC #4 добавлены Int16→SMALLINT
# и Float64→DOUBLE: реальный каталог использует SMALLINT и DOUBLE[], без них AC #5
# («все строки без потерь») конфликтовал бы с AC #8 («неизвестный тип → fail»).
CLICKHOUSE_TO_DUCKDB: dict[str, str] = {
    "UInt64": "HUGEINT",  # > 2^63, в BIGINT не влезает (NFR-4)
    "UInt32": "BIGINT",
    "Int64": "BIGINT",
    "Int32": "INTEGER",
    "Int16": "SMALLINT",  # сверх таблицы AC #4 — реальный каталог содержит SMALLINT
    "UInt8": "BOOLEAN",  # флаг 0/1
    "Float64": "DOUBLE",  # сверх таблицы AC #4 — реальный каталог содержит DOUBLE[]
    "Date": "DATE",
    "DateTime": "TIMESTAMP",
    "String": "VARCHAR",
}

# Набор валидных скалярных DuckDB-типов = КОДОМЕН маппинга (не дублировать вторым
# литералом — иначе разъедется с CLICKHOUSE_TO_DUCKDB). Валидный тип каталога — это
# скаляр отсюда либо его `T[]`-форма (один уровень; каталог глубже массива не идёт).
_DUCKDB_SCALARS = frozenset(CLICKHOUSE_TO_DUCKDB.values())

# Array(T) справочника Logs API → DuckDB `T[]`. Один уровень вложенности (каталог не
# использует вложенные массивы). Группа 1 — внутренний тип T.
_ARRAY_RE = re.compile(r"Array\((.+)\)")

# storage-имена строго snake_case (инвариант project-context). Валидируем формат:
# буква в начале, далее строчные/цифры/подчёркивание. Ловит внутренние пробелы
# (`"visit id"`), заглавные и спецсимволы, которые `strip()` НЕ убирает и которые
# сломали бы имя колонки view ниже по течению (2.6).
_SNAKE_CASE_RE = re.compile(r"[a-z][a-z0-9_]*")

# Ключ для значений сверх заголовка: `csv.DictReader` складывает избыток в список под
# restkey. Делаем его явной строкой (а не дефолтным None) — так строка с лишними
# колонками детектируется fail-loud, а не теряет хвост молча (напр. незакавыченная
# запятая в описании сдвинула бы поля). Имя нарочно «невозможное» для колонки каталога.
_RESTKEY = "__extra_columns__"

__all__ = [
    "Catalog",
    "CatalogField",
    "CLICKHOUSE_TO_DUCKDB",
    "duckdb_type_for",
    "load_catalog",
    "DEFAULT_CATALOG_PATH",
    "VALID_SOURCES",
    "SOURCE_PREFIXES",
]


def duckdb_type_for(clickhouse_type: str) -> str:
    """Перевести тип справочника ClickHouse в тип DuckDB (AC #4, «не угадываем»).

    ``Array(T)`` → ``f"{duckdb_type_for(T)}[]"`` (рекурсивно по внутреннему типу;
    нотация каталога — ``T[]``, в DuckDB-прозе тот же тип зовётся ``LIST<T>``).
    Скаляр → запись из :data:`CLICKHOUSE_TO_DUCKDB`. Неизвестный ClickHouse-тип →
    :class:`ValueError` с самим типом в сообщении (fail-loud, AC #8).
    """
    array_match = _ARRAY_RE.fullmatch(clickhouse_type)
    if array_match is not None:
        return f"{duckdb_type_for(array_match.group(1))}[]"
    try:
        return CLICKHOUSE_TO_DUCKDB[clickhouse_type]
    except KeyError:
        raise ValueError(
            f"Неизвестный ClickHouse-тип: {clickhouse_type!r} "
            f"(нет в таблице маппинга ClickHouse→DuckDB)"
        ) from None


def _is_valid_duckdb_type(duckdb_type: str) -> bool:
    """Проверить, что ``duckdb_type`` входит в набор легальных типов каталога (AC #8).

    Валиден скаляр из кодомена маппинга либо его `T[]`-форма, где T — такой скаляр.
    Покрывает все наблюдаемые типы каталога; глубже одного уровня массива не идёт.
    """
    if duckdb_type in _DUCKDB_SCALARS:
        return True
    if duckdb_type.endswith("[]") and duckdb_type[:-2] in _DUCKDB_SCALARS:
        return True
    return False


@dataclass(frozen=True, slots=True)
class CatalogField:
    """Одно поле каталога: связка storage-имя ↔ родное имя Метрики ↔ DuckDB-тип.

    Атрибут зовётся ``duckdb_type``, а НЕ ``type`` — чтобы не затенять builtin
    ``type``. CSV-колонка ``type`` маппится сюда как ``duckdb_type``.
    """

    source: str
    storage_name: str
    metrica_field: str
    duckdb_type: str
    description: str


@dataclass(frozen=True, slots=True)
class Catalog:
    """Загруженный и провалидированный каталог схемы.

    ``fields`` хранятся в порядке строк CSV — важно для воспроизводимого списка
    выгрузки (:func:`metrica_fields`).
    """

    fields: tuple[CatalogField, ...]

    def fields_for(self, source: str) -> tuple[CatalogField, ...]:
        """Все поля источника в порядке каталога (AC #1).

        Невалидный ``source`` (вне :data:`VALID_SOURCES`) → :class:`ValueError`.
        """
        _require_valid_source(source)
        return tuple(f for f in self.fields if f.source == source)

    def metrica_fields(self, source: str) -> list[str]:
        """Список родных имён ``metrica_field`` источника в порядке каталога (AC #2, FR-2).

        Формат — ровно ``list[str]`` под ``MetricaClient.create_log_request(fields=...)``
        (клиент сам джойнит через ``","``). Источник полей — каталог, без хардкода.
        """
        return [f.metrica_field for f in self.fields_for(source)]

    def duckdb_types(self, source: str) -> dict[str, str]:
        """Отображение ``storage_name → duckdb_type`` источника (AC #1; для views.py 2.6)."""
        return {f.storage_name: f.duckdb_type for f in self.fields_for(source)}


def _require_valid_source(source: str) -> None:
    """Провалидировать имя источника или fail-loud (AC #3)."""
    if source not in VALID_SOURCES:
        raise ValueError(
            f"Неизвестный source: {source!r} (ожидается один из {VALID_SOURCES})"
        )


def load_catalog(path: Path | None = None) -> Catalog:
    """Прочитать и провалидировать каталог схемы из CSV (fail-loud).

    ``path`` — инъектируемый шов: тесты передают мини-фикстуру, в проде берётся
    :data:`DEFAULT_CATALOG_PATH`. Парсинг — через :class:`csv.DictReader` (RFC4180,
    НЕ ``str.split(",")``): описания каталога содержат запятые/точки-с-запятой/кавычки
    (AC #6). Валидирует: заголовок == :data:`CATALOG_COLUMNS`, непустые обязательные
    поля, ``source`` ∈ {visits,hits}, префикс ``metrica_field`` по источнику, тип ∈
    известных DuckDB-типов, отсутствие дублей ``storage_name``/``metrica_field`` в
    источнике. Первая невалидная строка → :class:`ValueError` с её номером и
    содержимым (агрегацию ошибок осознанно не делаем — простота, NFR-6).

    Отсутствие файла / битый симлинк → :class:`ValueError` с путём (AC #8).
    """
    resolved = path if path is not None else DEFAULT_CATALOG_PATH
    if not resolved.is_file():
        raise ValueError(
            f"Каталог схемы не найден (файл отсутствует или битый симлинк): {resolved}"
        )

    # newline="" — требование csv: иначе встроенные переводы строк в кавычках
    # разобьются. encoding="utf-8": в описаниях кириллица и «—».
    with resolved.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, restkey=_RESTKEY)
        if reader.fieldnames != list(CATALOG_COLUMNS):
            raise ValueError(
                f"Заголовок каталога не соответствует ожидаемому. "
                f"Ожидалось {list(CATALOG_COLUMNS)}, получено {reader.fieldnames} "
                f"(проверь дрейф колонок, напр. type↔working_type)"
            )

        parsed: list[CatalogField] = []
        seen_storage: dict[str, set[str]] = {s: set() for s in VALID_SOURCES}
        seen_metrica: dict[str, set[str]] = {s: set() for s in VALID_SOURCES}

        # Нумеруем с 2: заголовок — строка 1; номер в сообщениях помогает диагностике.
        for line_no, row in enumerate(reader, start=2):
            field = _parse_row(row, line_no=line_no)
            # Дубли проверяем в рамках источника: одинаковый storage_name/metrica_field
            # → коллизия DDL view (AC #7). Кросс-источник НЕ дубль (client_id легально
            # есть и в visits, и в hits).
            if field.storage_name in seen_storage[field.source]:
                raise ValueError(
                    f"Строка {line_no}: дубль storage_name "
                    f"{field.storage_name!r} в источнике {field.source!r} "
                    f"(коллизия имени колонки view)"
                )
            if field.metrica_field in seen_metrica[field.source]:
                raise ValueError(
                    f"Строка {line_no}: дубль metrica_field "
                    f"{field.metrica_field!r} в источнике {field.source!r}"
                )
            seen_storage[field.source].add(field.storage_name)
            seen_metrica[field.source].add(field.metrica_field)
            parsed.append(field)

    # Пустой каталог (валидный заголовок, ноль строк данных) — вырожденный SSOT: дефект,
    # а не пустые данные. Молча отдать пустой Catalog → downstream закажет пустой набор
    # полей выгрузки. Fail-loud (project-context: «в спорной — строже»).
    if not parsed:
        raise ValueError(
            f"Каталог схемы пуст — нет ни одной строки данных (только заголовок): "
            f"{resolved}. Пустой SSOT = дефект."
        )

    return Catalog(fields=tuple(parsed))


def _parse_row(row: dict[str, str | None], *, line_no: int) -> CatalogField:
    """Провалидировать одну строку каталога и собрать :class:`CatalogField` (fail-loud).

    Обязательные ``source/storage_name/metrica_field/type`` непусты (AC #3);
    ``description`` — колонка обязана быть, значение может быть пустым (семантика, не
    структура). ``source`` ∈ {visits,hits} (AC #3); префикс ``metrica_field`` по
    источнику (AC #8); ``type`` ∈ известных DuckDB-типов (AC #8).
    """
    # Строка с колонок БОЛЬШЕ заголовка → избыток ушёл в список под restkey (_RESTKEY).
    # Без этой проверки хвост молча терялся бы (напр. незакавыченная запятая в описании
    # сдвинула бы поля) — против инварианта fail-loud (поле без записи = дефект).
    extra = row.get(_RESTKEY)
    if extra:
        raise ValueError(
            f"Строка {line_no}: колонок больше, чем в заголовке "
            f"({len(CATALOG_COLUMNS)}); лишнее: {extra!r}. "
            f"Проверь незакавыченные запятые/лишние поля. Содержимое строки: {row}"
        )

    # csv.DictReader отдаёт значения как str|None (None при недостатке колонок в строке).
    values = {col: (row.get(col) or "").strip() for col in CATALOG_COLUMNS}
    source = values["source"]
    storage_name = values["storage_name"]
    metrica_field = values["metrica_field"]
    duckdb_type = values["type"]
    description = values["description"]

    for col in ("source", "storage_name", "metrica_field", "type"):
        if not values[col]:
            raise ValueError(
                f"Строка {line_no}: обязательная колонка {col!r} пуста "
                f"(поле без записи = дефект). Содержимое строки: {row}"
            )

    if source not in VALID_SOURCES:
        raise ValueError(
            f"Строка {line_no}: source {source!r} вне {VALID_SOURCES}"
        )

    # storage-имя строго snake_case (инвариант): strip() убирает только края, а
    # внутренний пробел/заглавная сломали бы имя колонки view (2.6). Fail-loud.
    if not _SNAKE_CASE_RE.fullmatch(storage_name):
        raise ValueError(
            f"Строка {line_no}: storage_name {storage_name!r} не snake_case "
            f"(ожидается [a-z][a-z0-9_]*; пробелы/заглавные/спецсимволы недопустимы)"
        )

    expected_prefix = SOURCE_PREFIXES[source]
    if not metrica_field.startswith(expected_prefix):
        raise ValueError(
            f"Строка {line_no}: metrica_field {metrica_field!r} не начинается с "
            f"{expected_prefix!r} (ожидается для источника {source!r})"
        )

    if not _is_valid_duckdb_type(duckdb_type):
        raise ValueError(
            f"Строка {line_no}: неизвестный DuckDB-тип {duckdb_type!r} "
            f"(поле {storage_name!r}). Тип сидится маппингом ClickHouse→DuckDB, "
            f"сырой ClickHouse-тип в каталоге недопустим"
        )

    return CatalogField(
        source=source,
        storage_name=storage_name,
        metrica_field=metrica_field,
        duckdb_type=duckdb_type,
        description=description,
    )
