"""Оркестратор приёма Logs API за **один день один источник** — протокол идемпотентного дня.

Проводит один день одного источника (``visits``/``hits``) через весь цикл Logs API в строгом
порядке: ``mark_loading`` → ``create`` → poll до ``processed`` → download ВСЕХ частей →
парсинг TSV → ``write_partition`` (атомарно) → жёсткая сверка строк → ``mark_loaded``
(**точка коммита дня**) → ``clean``. День считается загруженным ТОЛЬКО после атомарного
rename партиции, сошедшейся сверки и записи чекпойнта (architecture.md: «протокол
идемпотентного дня»).

**Это СКЛЕЙКА примитивов, а не их повтор.** Вся «тяжёлая» механика уже реализована в
независимых примитивах 1.x/2.1–2.6 (retry/rate-limit — в ``MetricaClient`` 1.3, NFR-3;
атомарная запись — ``parquet_store`` 2.2; сверка — ``row_check`` 2.3; чекпойнт/реконсиляция
— ``load_state`` 2.4; лок одного писателя — ``writer_lock`` 2.5; типизированные view —
``views`` 2.6). p81 — единственное место, где материализуется **порядок** этих шагов; он их
**вызывает в правильной последовательности**, а не реализует заново.

**Это СВОЯ оркестрация, не построчный вендоринг directaiq.** В отличие от directaiq
``p81_load_logs.py`` здесь нет ``BaseScript``/``config_manager``, нет ``DROP TABLE`` ради
перезалива (перезалив дня = перезапись одного Parquet-файла через ``write_partition``), нет
параллельной очереди и нет «сверки как warning» (расхождение строк = жёсткий fail наружу).

**Гарантии цикла дня:**

- **Всё-или-ничего на частях (AC #6):** сначала скачиваются ВСЕ части в память; любая
  не скачалась → исключение ДО ``write_partition`` → день не персистится (ни «полу-дня», ни
  мусорных ``.tsv``).
- **Точка коммита = ``mark_loaded`` (AC #7):** ``clean`` идёт ПОСЛЕ коммита; его сбой —
  WARNING (остаток квоты), день остаётся загруженным, без отката и без fail.
- **Bounded poll (AC #5):** интервал/верхняя граница ожидания/лимит подряд-ошибок — модульные
  константы + переопределяемые kwargs (``config_manager`` не тащим, NFR-6); сон — через
  инъектируемый шов ``sleep`` (тесты без реальных пауз). ``canceled``/``processing_failed``
  → fail с диагностикой (AC #2), без молчаливого пропуска.
- **Fail до коммита → ``mark_failed`` (best-effort) + re-raise (AC #2):** реконсиляция (2.4)
  трактует ``loading``/``failed`` как незагруженный день → перельёт.

**Декомпозиция (вариант A, утверждён Шефом 2026-05-25):** ядро :func:`load_day`
(``conn``/``client`` инъектируются — НЕ берёт лок, НЕ открывает БД, НЕ строит клиент:
главный тестируемый шов) и run-level :func:`ingest_day` (берёт ``.writer.lock`` **один раз**,
открывает write-соединение, строит клиент из кредов, заводит ``load_state``/view'ы и зовёт
``load_day`` — ad-hoc единичный прогон, AC #1/#3). ``writer_lock`` **не реентерабелен**,
поэтому диапазон дней (2.8) берёт лок **один раз** вокруг всего прогона и зовёт ``load_day``
напрямую — **НЕ** ``ingest_day`` в цикле.

**Связь visits↔hits (AC #4) — факт каталога, не код p81.** Источники грузятся независимо
(тот же :func:`load_day` с ``source∈VALID_SOURCES``, своя партиция, своя строка чекпойнта);
связь ``visits.watch_ids`` ↔ ``hits.watch_id`` — свойство модели/каталога, джойнится в SQL
агента через view (2.6). p81 спецлогики связи не несёт.

**Диапазонный слой (история 2.8, в ЭТОМ модуле):** :func:`ingest_range` решает, какие дни
грузить — пропуск подтверждённо-загруженных (по ``reconcile``, FR-9) + идемпотентный перезалив
hot-window последних N дней (FR-10/FR-11), берёт лок один раз вокруг прогона и зовёт
:func:`load_day` напрямую (вариант A). Чистое ядро решения — :func:`_select_days_to_load`.

**Границы (НЕ здесь):** поверхность команды ``gdau-logs update`` (argparse, exit-коды, прогон
обоих источников под одним локом, агрегация частичного сбоя, resumable-сообщение про дневную
квоту) — 2.9; типизация view — 2.6; чтение/анализ через MCP — 3.1.

Каталог ``scripts/8x_metrica_logs_api/`` начинается с цифры → ``import scripts.8x_…`` как
statement = ``SyntaxError`` (digit-префикс). Импортировать модуль строкой:
``importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")`` (каталог — неявный
namespace-пакет без ``__init__.py``). Прямого entry-point у p81 НЕТ — его дёргает CLI 2.9.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from typing import Any, NamedTuple

import duckdb

from scripts.utils.catalog import VALID_SOURCES, Catalog, load_catalog
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.dates import (
    clamp_date_range,
    format_date,
    moscow_today,
    moscow_yesterday,
    parse_date,
)
from scripts.utils.env_reader import read_metrica_credentials
from scripts.utils.load_state import (
    ensure_load_state_table,
    mark_failed,
    mark_loaded,
    mark_loading,
    reconcile,
)
from scripts.utils.metrica_client import MetricaClient
from scripts.utils.parquet_store import write_partition
from scripts.utils.row_check import count_source_rows, split_tsv_rows, verify_row_count
from scripts.utils.views import create_views
from scripts.utils.writer_lock import writer_lock

logger = logging.getLogger(__name__)

# Дисциплина poll (AC #5). config_manager не тащим (NFR-6) → модульные константы +
# переопределяемые kwargs (тесты дают крошечные значения). 60 мин таймаута — как
# directaiq _wait_for_request; 30s интервал — рекомендация architecture.md.
POLL_INTERVAL_S = 30.0
POLL_TIMEOUT_S = 3600.0
MAX_CONSECUTIVE_POLL_ERRORS = 5

# Терминальные статусы цикла (AC #2): выгрузка завершилась НЕ успехом → fail с диагностикой.
_TERMINAL_FAILURE_STATUSES = frozenset({"canceled", "processing_failed"})

# Размер hot-window по умолчанию (FR-11, история 2.8). Почему 3: Метрика дособирает
# статистику задним числом несколько суток (доезжающие данные) — последние N дней
# переливаем ВСЕГДА поверх инкремента, чтобы догнать дозаписи без ручного перезалива
# (architecture.md:39–41/197). Конфигурируется через kwarg ingest_range(hot_window_days=…).
DEFAULT_HOT_WINDOW_DAYS = 3

__all__ = [
    "POLL_INTERVAL_S",
    "POLL_TIMEOUT_S",
    "MAX_CONSECUTIVE_POLL_ERRORS",
    "DEFAULT_HOT_WINDOW_DAYS",
    "load_day",
    "ingest_day",
    "ingest_range",
    "IngestRangeResult",
]


def load_day(
    conn: duckdb.DuckDBPyConnection,
    client: MetricaClient,
    source: str,
    date: str,
    *,
    catalog: Catalog | None = None,
    poll_interval_s: float = POLL_INTERVAL_S,
    poll_timeout_s: float = POLL_TIMEOUT_S,
    max_consecutive_errors: int = MAX_CONSECUTIVE_POLL_ERRORS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Провести один день одного источника через весь цикл приёма (AC #1, #2, #5, #6, #7).

    **Ядро на инъектируемых ``conn`` + ``client``** — главный тестируемый шов: НЕ берёт
    ``.writer.lock`` (его scope — зацикливающий вход: :func:`ingest_day` для одного дня, 2.8
    для диапазона; ``writer_lock`` не реентерабелен), НЕ открывает ``gdau.duckdb`` (готовый
    write-``conn`` инъектируется), НЕ строит клиент и НЕ реализует retry/rate-limit (это
    ``MetricaClient`` 1.3, NFR-3). Порядок шагов СТРОГИЙ (протокол идемпотентного дня):
    ``mark_loading`` → ``create`` → poll → download всех частей → парсинг TSV →
    ``write_partition`` → ``verify_row_count`` → ``mark_loaded`` (**точка коммита**) →
    ``clean``.

    :param conn: открытое write-соединение ``gdau.duckdb`` (открывает/закрывает вызывающий).
    :param client: построенный :class:`MetricaClient` (креды инжектит вызывающий).
    :param source: ``visits``/``hits`` (валидируется fail-loud).
    :param date: дата дня ``YYYY-MM-DD``; **валидируется** ``≤ вчера по МСК`` (НЕ молчаливый
        clamp: будущий день грузить нельзя — Logs API требует ``date2 < today``; диапазонный
        clamp — забота 2.8).
    :param catalog: инъектируемый шов; ``None`` → :func:`load_catalog` (прод-путь).
    :param poll_interval_s: пауза между опросами статуса (по умолчанию 30s).
    :param poll_timeout_s: верхняя граница ожидания ``processed`` (по умолчанию 60 мин).
    :param max_consecutive_errors: лимит подряд-ошибок опроса до fail (по умолчанию 5).
    :param sleep: шов сна (по умолчанию :func:`time.sleep`); тесты дают no-op.
    :returns: число записанных строк дня (== число строк источника после сверки).
    :raises ValueError: невалидный ``source``/формат даты или дата позже вчера по МСК.
    :raises RuntimeError: терминальный статус выгрузки (AC #2), таймаут/лимит ошибок poll
        (AC #5), ОС-сбой записи; :class:`~scripts.utils.row_check.RowCountMismatchError` —
        расхождение строк (жёсткий fail целостности, НЕ глушится).
    """
    _require_valid_source(source)
    # Граница дат: валидируем ≤ вчера по МСК fail-loud, НЕ клампим тихо (риск №10).
    # parse_date — строгий YYYY-MM-DD; format_date нормализует обратно в канон.
    parsed_date = parse_date(date)
    yesterday = moscow_yesterday()
    if parsed_date > yesterday:
        raise ValueError(
            f"Дата {date!r} позже вчера по МСК ({yesterday.isoformat()}): Logs API не "
            f"отдаёт сегодняшний/будущий день (требуется date2 < today). Загрузка отменена."
        )
    day = format_date(parsed_date)

    effective_catalog = catalog if catalog is not None else load_catalog()
    fields = effective_catalog.metrica_fields(source)

    # Двухфазная отметка: loading → (коммит) loaded. Реконсиляция (2.4) трактует loading как
    # незагруженный, поэтому крэш между mark_loading и mark_loaded → день под перезалив.
    mark_loading(conn, source, day)
    try:
        resp = client.create_log_request(
            date1=day, date2=day, fields=fields, source=source
        )
        # create_log_request отдаёт ПОЛНЫЙ ответ; нужное — под log_request (приём CLI 1.6).
        # `or resp` (не default): защищает и от null-значения ключа, не только отсутствия.
        log_request = resp.get("log_request") or resp
        req_id = int(log_request["request_id"])
        logger.info(
            "Заказана выгрузка: источник %s, дата %s, request_id %d", source, day, req_id
        )

        info = _poll_until_processed(
            client,
            req_id,
            poll_interval_s=poll_interval_s,
            poll_timeout_s=poll_timeout_s,
            max_consecutive_errors=max_consecutive_errors,
            sleep=sleep,
        )

        # Всё-или-ничего: ВСЕ части в память; любая ошибка → исключение ДО write (AC #6).
        parts_bytes = _download_all_parts(client, req_id, info)

        # Колонки берём из заголовка TSV (родные имена, авторитетны для выравнивания). Нет
        # заголовка (0 частей / части без строк = честно пустой день, риск №8) → колонки из
        # каталога (тот же список, что заказан) + строк нет: пустая типизированная партиция.
        header, rows = _parse_parts(parts_bytes)
        columns = header if header is not None else list(fields)

        # expected — от сырого TSV (заголовок на часть), независимо от парсинга (2.3).
        expected = count_source_rows(parts_bytes)
        actual = write_partition(source, day, columns, rows, catalog=effective_catalog)
        # Расхождение источник↔партиция → RowCountMismatchError наружу (НЕ глушим — риск №12).
        verify_row_count(expected, actual, source=source, date=day)

        # === ТОЧКА КОММИТА ДНЯ === день «загружен» ТОЛЬКО здесь (после rename + сверки).
        mark_loaded(conn, source, day, actual)
        logger.info("День загружен: источник %s, дата %s, строк %d", source, day, actual)
    except BaseException:
        # Любой сбой ДО коммита → защитная отметка failed (реконсиляция перельёт) + re-raise.
        # mark_failed сам может упасть (сбой в conn/БД) → best-effort: вторичная ошибка не
        # должна маскировать исходную; даже без неё остаётся loading от mark_loading (риск №12).
        with contextlib.suppress(Exception):
            mark_failed(conn, source, day)
        raise

    # clean ПОСЛЕ коммита (AC #7): день уже загружен. Сбой clean → WARNING (остаток квоты),
    # НЕ откат и НЕ fail — clean не часть коммита. req_id определён (except выше re-raise-ит,
    # сюда доходим только при успехе try).
    try:
        client.clean_log_request(req_id)
        logger.info(
            "Выгрузка очищена на стороне Метрики: request_id %d (квота освобождена)", req_id
        )
    except Exception as exc:
        # AC #7: день уже закоммичен (mark_loaded). clean — НЕ часть коммита, после него
        # единственная операция — сетевая очистка квоты. ЛЮБОЙ её сбой → WARNING, день
        # остаётся загруженным; никогда не fail и не откат (широкий except умышленный —
        # уже-загруженный день не должен превратиться в провал вызова из-за уборки квоты).
        logger.warning(
            "Не удалось очистить выгрузку request_id %d (%s) — день уже загружен; "
            "освободите квоту вручную при необходимости",
            req_id,
            exc,
        )
    return actual


def ingest_day(
    source: str,
    date: str,
    *,
    catalog: Catalog | None = None,
    poll_interval_s: float = POLL_INTERVAL_S,
    poll_timeout_s: float = POLL_TIMEOUT_S,
    max_consecutive_errors: int = MAX_CONSECUTIVE_POLL_ERRORS,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Запустить приём одного дня под локом — run-level единичного прогона (AC #1, #3).

    Читает креды окружения **до** лока (1.2; fail-loud ДО сети при отсутствии кредов; и грузит
    ``.env`` в окружение — от него зависит резолюция ``GDAU_DATA_ROOT`` в ``writer_lock``/
    ``connection``, см. :func:`ingest_range`), берёт ``.writer.lock`` **один раз** (AC #3,
    fail-fast если занят живым писателем), открывает write-соединение ``gdau.duckdb`` (2.1),
    строит :class:`MetricaClient`, заводит чекпойнт-таблицу и типизированные view'ы
    (идемпотентно; пере-определяет их и ПОСЛЕ load_day — иначе свежий источник остался бы пуст,
    см. ниже) и зовёт :func:`load_day`. Ad-hoc единичный прогон одного дня одного источника.

    **2.8 НЕ зовёт ``ingest_day`` в цикле** (``writer_lock`` не реентерабелен — повторный
    захват того же пути конфликтует сам с собой): диапазон дней берёт лок **один раз** вокруг
    всего прогона, открывает ``conn``/клиент один раз и зовёт :func:`load_day` напрямую по
    вычисленному набору дней (reconcile→skip+hot-window).

    Параметры/исключения — как у :func:`load_day` (плюс
    :class:`~scripts.utils.writer_lock.WriterLockHeldError`, если хранилище занято другим
    писателем, и :class:`ValueError` из ридера кредов, если их нет).
    """
    # Креды читаем ДО лока: грузит .env (GDAU_DATA_ROOT для writer_lock/connection — paths сам
    # .env не грузит) и fail-loud при отсутствии кредов до взятия лока/сети (см. ingest_range).
    creds = read_metrica_credentials()
    with writer_lock():  # один захват на весь прогон (AC #3, scope — зацикливающий вход)
        with DatabaseManager.connection() as conn:  # write-соединение (создаёт БД при отсутствии)
            client = MetricaClient(token=creds.token, counter_id=creds.counter_id)
            # Чекпойнт-таблица (2.4) + типизированные view'ы (2.6), идемпотентно.
            ensure_load_state_table(conn)
            create_views(conn, catalog=catalog)
            rows = load_day(
                conn,
                client,
                source,
                date,
                catalog=catalog,
                poll_interval_s=poll_interval_s,
                poll_timeout_s=poll_timeout_s,
                max_consecutive_errors=max_consecutive_errors,
                sleep=sleep,
            )
            # Партиция записана → пере-определяем view'ы поверх неё: до записи source без
            # партиций даёт пустышку WHERE false (см. ingest_range / views.build_view_ddl),
            # и без этой пересборки свежезагруженный день остался бы невидим в view.
            create_views(conn, catalog=catalog)
            return rows


class IngestRangeResult(NamedTuple):
    """Итог прогона диапазона за один источник — для агрегации/отчёта 2.9.

    :param source: источник прогона (``visits``/``hits``).
    :param loaded_dates: дни, которые реально прогнали через ``load_day`` (перезалиты), по
        возрастанию даты — отсутствующие по инкременту + все дни hot-window.
    :param skipped_dates: дни запрошенного (после clamp) диапазона, которые пропущены как
        подтверждённо-загруженные вне hot-window.
    :param total_rows: суммарное число записанных строк по всем загруженным дням.
    """

    source: str
    loaded_dates: list[str]
    skipped_dates: list[str]
    total_rows: int


def _iter_dates(date1: date, date2: date) -> Iterator[date]:
    """Дни ``[date1, date2]`` включительно по возрастанию (пусто при ``date1 > date2``).

    ``ingest_range`` инвертированный диапазон до сюда не пускает (``clamp_date_range`` валит
    его fail-fast ДО лока — риск №8); пустой генератор — лишь безопасный нижний предел helper'а.
    """
    cur = date1
    while cur <= date2:
        yield cur
        cur += timedelta(days=1)


def _select_days_to_load(
    source: str,
    loaded: frozenset[tuple[str, str]],
    date1: date,
    date2: date,
    *,
    hot_window_days: int,
    anchor: date,
) -> list[str]:
    """Решить, какие дни диапазона реально грузить — **ЧИСТАЯ** функция (AC #1, #3, #4, #5).

    Главный тестируемый шов 2.8 (приём 2.6 ``build_view_ddl``/2.7 ``load_day``): без ``conn``,
    сети и обращения к часам — детерминируется инъекцией ``anchor``/``loaded``. ``ingest_range``
    — лишь тонкая обвязка над ней (лок/соединение/клиент/``reconcile``).

    Логика по каждому дню ``d`` диапазона ``[date1, date2]`` (по возрастанию): грузить, если он
    **в hot-window ИЛИ ещё не загружен**. Дизъюнкция означает, что **hot-window побеждает skip**
    (AC #4): день, попавший и в ``loaded``, и в окно, всё равно перезаливается.

    Hot-window — последние ``hot_window_days`` дней, заканчивающихся на ``anchor`` («вчера по
    МСК», передаётся вызывающим): окно ``[anchor - (N-1), anchor]``. К запрошенному диапазону
    окно **клипуется естественно** — итерируем только ``[date1, date2]`` (риск №4); якорь окна —
    НЕ ``date2`` (для исторического диапазона ``date2 < anchor`` окно с ним не пересекается →
    чистый инкремент, доезжать нечему). ``hot_window_days == 0`` → окно выключено (чистый
    инкремент); ``hot_window_days < 0`` → :class:`ValueError` (AC #5).

    «Загружен» = подтверждено ``reconcile`` (2.4: факт партиции + ``status='loaded'`` +
    ``row_count == факт``), приходит как ``loaded: frozenset[(source, 'YYYY-MM-DD')]`` — НЕ
    ``SELECT DISTINCT date``/наличие файла (риск №3, FR-9). Ключ сравнения — тот же формат
    ``(source, format_date(d))``.

    :returns: список дней ``YYYY-MM-DD`` к загрузке, по возрастанию даты.
    :raises ValueError: ``hot_window_days < 0``.
    """
    if hot_window_days < 0:
        raise ValueError(
            f"hot_window_days не может быть отрицательным: {hot_window_days} "
            f"(0 — окно выключено, >0 — размер свежего окна перезалива)"
        )
    # Старт окна считаем один раз; при N==0 окно выключено (hot_start остаётся None →
    # in_hot всегда False). `hot_start is not None` ниже сужает тип для mypy strict.
    hot_start: date | None = None
    if hot_window_days > 0:
        hot_start = anchor - timedelta(days=hot_window_days - 1)

    days: list[str] = []
    for d in _iter_dates(date1, date2):
        in_hot = hot_start is not None and hot_start <= d <= anchor
        key = (source, format_date(d))
        if in_hot or key not in loaded:  # hot-window игнорирует loaded (AC #4)
            days.append(format_date(d))
    return days


def ingest_range(
    source: str,
    date1: str,
    date2: str,
    *,
    hot_window_days: int = DEFAULT_HOT_WINDOW_DAYS,
    catalog: Catalog | None = None,
    poll_interval_s: float = POLL_INTERVAL_S,
    poll_timeout_s: float = POLL_TIMEOUT_S,
    max_consecutive_errors: int = MAX_CONSECUTIVE_POLL_ERRORS,
    sleep: Callable[[float], None] = time.sleep,
) -> IngestRangeResult:
    """Обновить один источник за диапазон — run-level диапазона (вариант A, AC #1–#5).

    Берёт ``.writer.lock`` **ОДИН раз** вокруг всего прогона, открывает write-соединение,
    строит :class:`MetricaClient`, заводит ``load_state``/view'ы и ``reconcile`` (всё один раз),
    вычисляет набор дней (:func:`_select_days_to_load`) и зовёт :func:`load_day` **напрямую** по
    каждому дню. Реализует инкремент (FR-9: грузим только отсутствующие по ``reconcile`` дни +
    hot-window), идемпотентный перезалив (FR-10: перезалив дня = повторный ``load_day`` →
    ``write_partition`` одного файла, без ``DROP``) и hot-window последних N дней (FR-11).

    **Лок ОДИН раз — НЕ ``ingest_day`` в цикле.** ``writer_lock`` не реентерабелен
    (``writer_lock.py:82``): повторный захват того же пути тем же процессом конфликтует сам с
    собой. Поэтому диапазон берёт лок один раз и зовёт ядро ``load_day`` (оно лок НЕ берёт — его
    контракт по варианту A 2.7), а **НЕ** run-level ``ingest_day`` (тот берёт лок). Лок-scope —
    зацикливающий вход (как ``ingest_day``/``load_day``): чтобы прогнать **оба** источника под
    одним локом, 2.9 вынесет лок/conn/клиент наружу и позовёт диапазонную логику по каждому
    ``source`` (без реентрантности — риск №1/№7).

    **Порядок СТРОГИЙ.** Чтение кредов, clamp диапазона и валидация ``N`` — **до** лока
    (fail-fast, риск №8): не брать лок для заведомо пустого/инвертированного диапазона или при
    отсутствии кредов. Чтение кредов **до** лока ещё и грузит ``.env`` в окружение, от которого
    зависит резолюция ``GDAU_DATA_ROOT`` в ``writer_lock``/``connection`` (``paths`` сам ``.env``
    не грузит) — иначе лок резолвил бы корень хранилища ДО загрузки ``.env``. ``moscow_today()``
    замеряется **один раз** — потолок clamp (``today - 1``) и якорь hot-window (``today - 1``)
    гарантированно консистентны (нет TOCTOU на полночи, риск №4).

    **Сбой дня → проброс (риск №6).** Если ``load_day`` бросает (``RowCountMismatchError`` 2.3,
    терминальный статус API, исчерпание poll) — исключение пробрасывается наружу (лок снимается
    в ``finally`` контекст-менеджера). Уже закоммиченные дни (каждый ``load_day`` коммитит
    независимо через ``mark_loaded``) **остаются** загруженными; повторный прогон до-грузит хвост
    через инкремент (skip уже-загруженных) — resumability через per-day-коммит + incremental-skip.
    Сбой НЕ глушится ради продолжения (маскировка потери — анти-паттерн).

    **Границы (НЕ здесь):** поверхность ``gdau-logs update`` (argparse, exit-коды, прогон обоих
    источников под одним локом, агрегация частичного сбоя, resumable-сообщение про дневную квоту)
    — история 2.9.

    :param source: ``visits``/``hits`` (валидируется fail-loud ДО лока).
    :param date1: начало диапазона ``YYYY-MM-DD`` (строго; мусор → ``ValueError`` до лока).
    :param date2: конец диапазона ``YYYY-MM-DD``; клампится к «вчера по МСК» с INFO-логом.
    :param hot_window_days: размер hot-window (по умолчанию :data:`DEFAULT_HOT_WINDOW_DAYS`);
        ``0`` — окно выключено (чистый инкремент); ``<0`` → ``ValueError`` до лока (AC #5).
    :param catalog: инъектируемый шов; пробрасывается в ``load_day`` (``None`` → прод-каталог).
    :param poll_interval_s: пауза между опросами статуса (проброс в ``load_day``).
    :param poll_timeout_s: верхняя граница ожидания ``processed`` (проброс в ``load_day``).
    :param max_consecutive_errors: лимит подряд-ошибок опроса (проброс в ``load_day``).
    :param sleep: шов сна (по умолчанию :func:`time.sleep`; тесты дают no-op).
    :returns: :class:`IngestRangeResult` (что перезалито/пропущено/сумма строк) для 2.9.
    :raises ValueError: невалидный ``source``/формат даты, инвертированный диапазон (clamp),
        ``hot_window_days < 0`` и отсутствие кредов — всё fail-fast ДО лока и ДО сети.
        ``read_metrica_credentials`` зовётся ПЕРЕД ``writer_lock`` намеренно: его побочный
        эффект грузит ``.env`` в окружение, от которого зависит резолюция ``GDAU_DATA_ROOT`` в
        ``writer_lock``/``connection`` (``paths`` сам ``.env`` не грузит).
    :raises WriterLockHeldError: хранилище занято другим живым писателем.
    :raises RuntimeError: проброс сбоя дня из ``load_day`` (включая ``RowCountMismatchError``).
    """
    # --- до лока (fail-fast — риск №8): валидация + чтение кредов + clamp без взятия лока ---
    _require_valid_source(source)
    if hot_window_days < 0:  # дублируем гард _select_days_to_load ДО лока (AC #5, риск №8)
        raise ValueError(
            f"hot_window_days не может быть отрицательным: {hot_window_days} "
            f"(0 — окно выключено, >0 — размер свежего окна перезалива)"
        )
    # Креды читаем ДО лока. Помимо fail-loud-а при отсутствии кредов (не брать лок ради
    # заведомо нерабочего прогона), это грузит .env в окружение (env_reader._load_env), а от
    # него зависит резолюция GDAU_DATA_ROOT в writer_lock/connection: paths сам .env НЕ грузит
    # (см. paths.py). Иначе writer_lock резолвил бы корень хранилища ДО загрузки .env →
    # ValueError «GDAU_DATA_ROOT не задана» при штатном запуске оператора из папки игры.
    creds = read_metrica_credentials()
    today = moscow_today()  # ОДИН замер часов: потолок clamp и якорь окна консистентны (риск №4)
    d1, d2 = clamp_date_range(parse_date(date1), parse_date(date2), today_msk=today)
    anchor = today - timedelta(days=1)  # == потолок clamp == вчера по МСК

    # --- под локом ОДИН раз (AC #3): запись всего диапазона под единым .writer.lock ---
    with writer_lock():  # один захват на весь прогон (НЕ на каждый день — риск №1)
        with DatabaseManager.connection() as conn:  # write-соединение (создаёт БД при отсутствии)
            client = MetricaClient(token=creds.token, counter_id=creds.counter_id)
            # Чекпойнт-таблица (2.4) + типизированные view'ы (2.6), идемпотентно. View'ы строим
            # ДО загрузки (слой существует, даже когда грузить нечего) И пере-строим ПОСЛЕ (ниже):
            # для источника без партиций build_view_ddl даёт статичную пустышку WHERE false — без
            # пост-пересборки свежезагруженный источник остался бы пуст до следующего прогона.
            ensure_load_state_table(conn)
            create_views(conn, catalog=catalog)
            # Инкремент по reconcile (FR-9): подтверждённо-загруженные дни (факт+loaded+
            # row_count). Мутирует load_state (DELETE ложной меты) → строго под локом (риск №3).
            loaded = reconcile(conn, sources=[source])
            days = _select_days_to_load(
                source, loaded, d1, d2, hot_window_days=hot_window_days, anchor=anchor
            )
            day_set = set(days)
            skipped = [
                format_date(d) for d in _iter_dates(d1, d2) if format_date(d) not in day_set
            ]
            logger.info(
                "Диапазон %s: источник %s, дней к загрузке %d, пропущено %d, hot-window N=%d",
                f"[{format_date(d1)}..{format_date(d2)}]",
                source,
                len(days),
                len(skipped),
                hot_window_days,
            )
            total_rows = 0
            for day in days:
                # load_day НАПРЯМУЮ (не ingest_day — реентрантность лока, риск №1). Сбой дня
                # пробрасывается наружу (НЕ глушим — риск №6); закоммиченные дни остаются.
                total_rows += load_day(
                    conn,
                    client,
                    source,
                    day,
                    catalog=catalog,
                    poll_interval_s=poll_interval_s,
                    poll_timeout_s=poll_timeout_s,
                    max_consecutive_errors=max_consecutive_errors,
                    sleep=sleep,
                )
            if days:
                # Партиции записаны → пере-определяем view'ы поверх нового сырья: source-view
                # перестаёт быть пустышкой (WHERE false) и становится ленивым read_parquet,
                # отражающим загруженные партиции (и будущие). См. views.build_view_ddl.
                create_views(conn, catalog=catalog)
            return IngestRangeResult(
                source=source,
                loaded_dates=days,
                skipped_dates=skipped,
                total_rows=total_rows,
            )


def _poll_until_processed(
    client: MetricaClient,
    req_id: int,
    *,
    poll_interval_s: float,
    poll_timeout_s: float,
    max_consecutive_errors: int,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Опрашивать статус выгрузки до ``processed`` — ограниченный цикл (AC #2, #5).

    Цикл по :func:`time.monotonic` с верхней границей ``poll_timeout_s`` (не вечное ожидание).
    ``processed`` → вернуть info (со списком ``parts``); ``canceled``/``processing_failed`` →
    :class:`RuntimeError` с диагностикой (AC #2, без молчаливого пропуска); прочие статусы
    (``created``/``processing``/…) → ждать ``sleep(poll_interval_s)`` и опрашивать дальше.
    Ошибка опроса (клиент уже отретраил транзиент — поднятое наружу терминально) наращивает
    счётчик подряд-ошибок: на лимите → fail; успешный опрос счётчик **сбрасывает** (AC #5).
    rate-limit/retry заново НЕ реализуем (NFR-3); сон — через инъектируемый ``sleep``.
    """
    deadline = time.monotonic() + poll_timeout_s
    consecutive_errors = 0
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Таймаут ожидания выгрузки request_id {req_id}: статус не стал 'processed' "
                f"за {poll_timeout_s:.0f}s. Загрузка отменена (poll не вечен — AC #5)."
            )
        try:
            info = client.get_log_request(req_id)
        except RuntimeError as exc:
            # Клиент уже отретраил транзиент (NFR-3); поднятое наружу — терминальная ошибка.
            consecutive_errors += 1
            logger.warning(
                "Опрос выгрузки request_id %d дал ошибку (%d/%d): %s",
                req_id,
                consecutive_errors,
                max_consecutive_errors,
                exc,
            )
            if consecutive_errors >= max_consecutive_errors:
                raise RuntimeError(
                    f"Опрос выгрузки request_id {req_id} провалился {consecutive_errors} "
                    f"раз подряд (лимит {max_consecutive_errors}). Загрузка отменена."
                ) from exc
            sleep(poll_interval_s)
            continue

        consecutive_errors = 0  # успешный опрос сбрасывает счётчик подряд-ошибок (AC #5)
        status = info.get("status")
        if status == "processed":
            return info
        if status in _TERMINAL_FAILURE_STATUSES:
            raise RuntimeError(
                f"Выгрузка request_id {req_id} завершилась статусом {status!r} (не "
                f"'processed'). Загрузка отменена без молчаливого пропуска (AC #2)."
            )
        logger.info(
            "Выгрузка request_id %d в статусе %r — ждём %.0fs", req_id, status, poll_interval_s
        )
        sleep(poll_interval_s)


def _download_all_parts(
    client: MetricaClient, req_id: int, info: dict[str, Any]
) -> list[bytes]:
    """Скачать ВСЕ части выгрузки в память — всё-или-ничего (AC #6, риск №6).

    Части держим **байтами в памяти** (не россыпью ``.tsv``-файлов): нет мусора частичных
    файлов при сбое. Любая часть не скачалась (``RuntimeError``/``OSError`` клиента) →
    исключение пробрасывается наверх ДО ``write_partition`` → день не персистится целиком
    (не «собрали что есть»). 0 частей (честно пустой день) → ``[]``.
    """
    parts = info.get("parts", [])
    parts_bytes: list[bytes] = []
    for part in parts:
        try:
            part_number = part["part_number"]
        except (KeyError, TypeError) as exc:
            # Понятный fail-loud вместо сырого KeyError/TypeError (стиль модуля + CLI-брат).
            raise RuntimeError(
                f"Часть выгрузки request_id {req_id} без 'part_number' ({part!r}) — "
                f"некорректный ответ Logs API. Сборка дня отменена."
            ) from exc
        # Ошибка скачивания НЕ глушится — пробрасывается до write_partition (AC #6).
        content = client.download_log_request_part(req_id, part_number)
        parts_bytes.append(content)
    logger.info("Скачано частей выгрузки request_id %d: %d", req_id, len(parts_bytes))
    return parts_bytes


def _parse_parts(parts_bytes: list[bytes]) -> tuple[list[str] | None, list[list[str]]]:
    """Распарсить TSV-части в (заголовок, строки-данные) единым сплиттером (риск №2).

    Границы строк режет общий :func:`~scripts.utils.row_check.split_tsv_rows` (тот же шов,
    что у сверки ``count_part_rows`` 2.3) — парсинг и ``expected`` согласованы по границам
    строк, off-by-N misfire исключён. Заголовок берём из **первой** непустой части (родные
    имена, авторитетны для выравнивания колонка↔ячейка); строку-заголовок каждой части
    отбрасываем (``lines[1:]``) — он повторяется в каждой части (off-by-P-гард). Ячейки —
    ``split("\\t")``.

    :returns: ``(header, rows)``. ``header`` = ``None``, если ни в одной части нет ни строки
        (0 частей или части без содержимого) — вызывающий тогда берёт колонки из каталога
        (честно пустой день, риск №8); ``rows`` — все строки-данные по частям без заголовков.
    """
    header: list[str] | None = None
    rows: list[list[str]] = []
    for part in parts_bytes:
        lines = split_tsv_rows(part)
        if not lines:
            continue  # пустая часть без содержимого — нечего брать
        part_header = lines[0].split("\t")
        if header is None:
            header = part_header
        elif part_header != header:
            # Заголовок повторяется в КАЖДОЙ части и обязан совпадать. Расхождение (иной
            # порядок/набор колонок при той же ширине) иначе тихо рассогласовало бы ячейки:
            # счётчики строк всё равно сошлись бы и verify_row_count прошла бы → порча
            # закоммитилась бы незаметно. Жёсткий fail ДО write_partition (целостность сырья).
            raise RuntimeError(
                f"Заголовки частей выгрузки расходятся: {part_header} != {header}. "
                f"Сборка дня отменена (рассогласование колонок недопустимо)."
            )
        # lines[1:] — строки-данные ЭТОЙ части (её заголовок отброшен; он есть в каждой части).
        rows.extend(line.split("\t") for line in lines[1:])
    return header, rows


def _require_valid_source(source: str) -> None:
    """Провалидировать имя источника или fail-loud (переиспользует ``VALID_SOURCES`` каталога)."""
    if source not in VALID_SOURCES:
        raise ValueError(
            f"Неизвестный source: {source!r} (ожидается один из {VALID_SOURCES})"
        )
