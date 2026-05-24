"""Чекпойнт загруженных дней ``load_state`` + реконсиляция мета×факт на старте.

Ведёт в рабочей базе ``gdau.duckdb`` мета-таблицу ``load_state`` — журнал «какой день
какого источника уже загружен, сколько в нём строк и в каком он состоянии». И на старте
каждого обновления **сверяет журнал с реальными Parquet-партициями на диске**: где
**источник истины — факт партиции**, а мета приводится к нему (мета может врать после
крэша посреди записи, ручного вмешательства или рассинхрона — журнал не назначает правду,
он её отражает, FR-12 / NFR-1).

День засчитывается загруженным **только** при конъюнкции трёх условий: (1) файл партиции
существует, (2) ``status == 'loaded'`` и (3) ``row_count`` журнала равен реальному
``count()`` партиции. Любое из трёх ложно → день незагружен (под перезалив). Битая/
нечитаемая партиция при счёте → этот день под перезалив, но **проход не падает** (один
сбойный файл не должен заблокировать обновление всей базы — это и есть «не сломать базу»).
Статусы ``loading``/``failed`` (полу-закоммиченный или явно проваленный день) трактуются
как незагруженный даже при совпавшем ``count``.

**Границы (что делает ДРУГОЙ компонент, не этот):** открытие/закрытие ``gdau.duckdb`` —
:class:`scripts.utils.database_manager.DatabaseManager` (2.1): сюда инъектируется готовый
``conn``, модуль БД сам **не** открывает; запись Parquet-партиции — ``parquet_store`` (2.2);
жёсткую сверку источник↔партиция по сырому TSV — ``row_check`` (2.3); захват ``.writer.lock``
вокруг записи — 2.5; типизированные view с ``TRY_CAST`` — 2.6; сборку дня и оркестрацию
цикла приёма — p81 (2.7); решение «какие дни лить» / перезалив / hot-window — инкремент
(2.8). Модуль в сеть не ходит и **не** импортирует ``parquet_store``/``database_manager``
(нулевая сцепка по коду; считает чтением, а не записью).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import NamedTuple

import duckdb

from scripts.utils.catalog import VALID_SOURCES
from scripts.utils.paths import get_raw_partition_path, get_raw_source_dir

logger = logging.getLogger(__name__)

# Словарь статусов — единый источник, не магические строки. Только STATUS_LOADED
# засчитывается реконсиляцией (AC #6); loading — старт двухфазной отметки p81,
# failed — явный провал; оба трактуются как незагруженный день.
STATUS_LOADING = "loading"
STATUS_LOADED = "loaded"
STATUS_FAILED = "failed"

# Схема мета-таблицы (AC #1). date — DATE (DuckDB сам кастит строку 'YYYY-MM-DD'; нужно
# для диапазонных запросов инкремента/hot-window 2.8). row_count — BIGINT, НЕ HUGEINT:
# HUGEINT обоснован только для ID-полей > 2^63 (visit_id/client_id/watch_id), счётчик
# строк дня влезает в BIGINT с огромным запасом (риск №6). PK (source, date) нужен для
# UPSERT ON CONFLICT и для skip-инкремента 2.8.
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS load_state (
    source     VARCHAR   NOT NULL,
    date       DATE      NOT NULL,
    row_count  BIGINT,
    loaded_at  TIMESTAMP,
    status     VARCHAR   NOT NULL,
    PRIMARY KEY (source, date)
)
"""

# UPSERT отметок: опирается на PK (source, date). Время ставит сама БД через SQL-выражение
# current_timestamp в VALUES (НЕ Python datetime и НЕ параметр — иначе строка не скастится
# в TIMESTAMP); время БД консистентно. Параметры — биндингом (?), не конкатенацией: значения
# хранилища не попадают в текст SQL. Две формы: «loaded» проставляет row_count + время,
# «pending» (loading/failed) сбрасывает row_count/loaded_at в NULL (день не закоммичен).
_UPSERT_LOADED_SQL = """
INSERT INTO load_state (source, date, row_count, loaded_at, status)
VALUES (?, ?, ?, current_timestamp, ?)
ON CONFLICT (source, date) DO UPDATE SET
    row_count = excluded.row_count,
    loaded_at = excluded.loaded_at,
    status    = excluded.status
"""

_UPSERT_PENDING_SQL = """
INSERT INTO load_state (source, date, row_count, loaded_at, status)
VALUES (?, ?, NULL, NULL, ?)
ON CONFLICT (source, date) DO UPDATE SET
    row_count = excluded.row_count,
    loaded_at = excluded.loaded_at,
    status    = excluded.status
"""

__all__ = [
    "STATUS_LOADING",
    "STATUS_LOADED",
    "STATUS_FAILED",
    "ensure_load_state_table",
    "mark_loading",
    "mark_loaded",
    "mark_failed",
    "count_partition_rows",
    "reconcile",
]


class _MetaRow(NamedTuple):
    """Строка журнала ``load_state`` для одного дня (внутреннее представление реконсиляции)."""

    row_count: int | None
    status: str


def ensure_load_state_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Создать мета-таблицу ``load_state``, если её ещё нет — идемпотентно (AC #1).

    ``CREATE TABLE IF NOT EXISTS`` — повторный вызов безопасен (без побочной логики).
    Зовут init (4.3) при разворачивании БД и защитно p81 (2.7) перед записью чекпойнта.
    """
    conn.execute(_CREATE_TABLE_SQL)


def mark_loaded(
    conn: duckdb.DuckDBPyConnection, source: str, date: str, row_count: int
) -> None:
    """Отметить день загруженным — **точка коммита дня** (UPSERT ``status='loaded'``; AC #6).

    Зовётся p81 (2.7) **после** атомарного rename партиции (2.2) и жёсткой сверки строк
    (2.3) — день «загружен» ТОЛЬКО здесь. ``loaded_at`` ставит БД (``current_timestamp``).
    Повторная отметка того же дня обновляет строку (не плодит дубли — PK ``(source, date)``).

    ``source`` ∈ {visits, hits} и ``row_count >= 0`` валидируются fail-loud
    (:class:`ValueError`): мусорный источник/отрицательный счётчик — дефект вызывающего.
    """
    _require_valid_source(source)
    if row_count < 0:
        raise ValueError(
            f"row_count не может быть отрицательным: {row_count!r} "
            f"(источник {source!r}, дата {date!r})."
        )
    conn.execute(_UPSERT_LOADED_SQL, [source, date, row_count, STATUS_LOADED])


def mark_loading(conn: duckdb.DuckDBPyConnection, source: str, date: str) -> None:
    """Отметить начало загрузки дня — старт двухфазной отметки p81 (UPSERT ``status='loading'``).

    ``row_count``/``loaded_at`` сбрасываются в ``NULL``: день ещё не закоммичен. Ради этой
    отметки реконсиляция и обязана трактовать ``loading`` как незагруженный день (AC #6):
    крэш между ``mark_loading`` и ``mark_loaded`` оставит ``loading`` — день под перезалив.
    ``source`` валидируется fail-loud.
    """
    _require_valid_source(source)
    conn.execute(_UPSERT_PENDING_SQL, [source, date, STATUS_LOADING])


def mark_failed(conn: duckdb.DuckDBPyConnection, source: str, date: str) -> None:
    """Отметить явный провал загрузки дня (UPSERT ``status='failed'``; полнота словаря AC #6).

    Защитная отметка: реконсиляция трактует ``failed`` как незагруженный день (под
    перезалив), даже если файл партиции существует. ``source`` валидируется fail-loud.
    """
    _require_valid_source(source)
    conn.execute(_UPSERT_PENDING_SQL, [source, date, STATUS_FAILED])


def count_partition_rows(
    conn: duckdb.DuckDBPyConnection, source: str, date: str
) -> int | None:
    """Реальное число строк в файле партиции дня — факт (AC #5, риск №3).

    Путь резолвится через :func:`scripts.utils.paths.get_raw_partition_path` (валидирует
    ``source``; ``.parquet.tmp`` сюда не попадает — резолвер даёт ``.parquet``). Нет
    **обычного файла** (отсутствует ИЛИ это директория) → ``None`` (дня фактически нет).
    Файл есть → ``count(*)`` через ``read_parquet(?)`` с путём **параметром** (не sql-литерал):
    корень хранилища не уходит в текст SQL — ни инъекций, ни проблем с кавычками. Битый/
    нечитаемый Parquet (:class:`duckdb.Error`) или сбой ФС при доступе (:class:`OSError`) →
    лог WARNING + ``None`` (AC #5: НЕ валить обновление исключением; битый факт = «дня нет»
    под перезалив).
    """
    path = get_raw_partition_path(source, date)
    try:
        # is_file (а не exists): директория с именем {date}.parquet прошла бы .exists()==True,
        # а read_parquet сглобил бы вложенные parquet в агрегированный count — ложный факт.
        if not path.is_file():
            return None
        row = conn.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(path)]
        ).fetchone()
    except (duckdb.Error, OSError) as exc:
        # duckdb.Error — битый/нечитаемый Parquet; OSError — сбой ФС (битый mount/права) при
        # is_file()/чтении. Любой → «дня нет» под перезалив, проход НЕ валим (AC #5).
        logger.warning("Партиция %s нечитаема (%s) — день под перезалив", path, exc)
        return None
    if row is None:  # count(*) всегда возвращает строку; гард ради контракта/типов
        return None
    return int(row[0])


def reconcile(
    conn: duckdb.DuckDBPyConnection, *, sources: Iterable[str] = VALID_SOURCES
) -> frozenset[tuple[str, str]]:
    """Сверить мета × факт партиций на старте обновления; вернуть загруженные дни (AC #2–#6).

    По каждому источнику берёт **объединение** ключей дней: строки журнала ∪ файлы
    ``{date}.parquet`` (``.parquet.tmp`` исключён — риск №5). Для каждого дня считает факт
    партиции и сверяет с журналом. **День загружен ⟺** факт не ``None`` **И**
    ``status == 'loaded'`` **И** ``row_count == факт`` (три условия — AC #3). Иначе (любое
    ложно, включая ``fact is None`` от отсутствия/битости — AC #5 — и ``loading``/``failed``
    — AC #6) день **незагружен**: ложная/устаревшая строка журнала удаляется (мета →
    факт, AC #4; отсутствие строки = «не загружен», инкремент 2.8 перельёт).

    Источник истины — факт партиции: мета корректируется к нему, никогда наоборот (риск №1).
    Возвращает :class:`frozenset` подтверждённо-загруженных ``(source, date)`` для инкремента
    2.8. Решение «что лить»/перезалив/hot-window здесь НЕ принимается (границы — 2.7/2.8).

    Самодостаточна: гарантирует таблицу ``load_state`` в начале (``ensure_load_state_table``
    идемпотентен) — вызов до её создания init (4.3)/p81 (2.7) не должен ронять весь проход
    ``CatalogException``-ом мид-проходом (дух AC #5: один сбой не валит всё обновление).
    """
    # Self-guard: без таблицы SELECT в _load_meta бросил бы CatalogException (подкласс
    # duckdb.Error) мимо per-day-перехвата count_partition_rows и сорвал бы весь проход.
    ensure_load_state_table(conn)
    loaded: set[tuple[str, str]] = set()
    corrected = 0
    for source in sources:
        _require_valid_source(source)
        meta = _load_meta(conn, source)
        parts = _partition_dates(source)
        for date in meta.keys() | parts:
            fact = count_partition_rows(conn, source, date)
            row = meta.get(date)
            if (
                fact is not None
                and row is not None
                and row.status == STATUS_LOADED
                and row.row_count == fact
            ):
                loaded.add((source, date))
            elif row is not None:
                # Ложная/устаревшая/полу-закоммиченная мета — привести к факту (AC #4/#5/#6).
                conn.execute(
                    "DELETE FROM load_state WHERE source = ? AND date = ?",
                    [source, date],
                )
                corrected += 1
    logger.info(
        "Реконсиляция load_state: подтверждено загруженных дней %d, исправлено меты %d",
        len(loaded),
        corrected,
    )
    return frozenset(loaded)


def _load_meta(conn: duckdb.DuckDBPyConnection, source: str) -> dict[str, _MetaRow]:
    """Прочитать строки журнала источника в отображение ``'YYYY-MM-DD' → _MetaRow``.

    ``date`` приходит как :class:`datetime.date`; приводим ``.isoformat()`` для сравнения
    со ``stem``-ами файлов партиций (тоже ``'YYYY-MM-DD'``).
    """
    rows = conn.execute(
        "SELECT date, row_count, status FROM load_state WHERE source = ?", [source]
    ).fetchall()
    meta: dict[str, _MetaRow] = {}
    for date_value, row_count, status in rows:
        meta[date_value.isoformat()] = _MetaRow(row_count, status)
    return meta


def _partition_dates(source: str) -> set[str]:
    """Множество дат дней-партиций источника по файлам ``{date}.parquet`` (``.tmp`` исключён).

    Каталога источника нет (нет ни одной партиции) → пустое множество. Glob ``*.parquet``
    по суффиксу не матчит осиротевший ``{date}.parquet.tmp`` (риск №5 — это stale temp от
    прошлого крэша, забота ``parquet_store`` 2.2, не факт загруженного дня).
    """
    source_dir = get_raw_source_dir(source)
    if not source_dir.is_dir():
        return set()
    return {path.stem for path in source_dir.glob("*.parquet")}


def _require_valid_source(source: str) -> None:
    """Провалидировать имя источника или fail-loud (переиспользует VALID_SOURCES каталога)."""
    if source not in VALID_SOURCES:
        raise ValueError(
            f"Неизвестный source: {source!r} (ожидается один из {VALID_SOURCES})"
        )
