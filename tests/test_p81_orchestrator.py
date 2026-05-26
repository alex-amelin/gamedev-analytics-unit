"""Offline-тесты оркестратора приёма за день p81 (история 2.7).

Покрывают весь цикл приёма за один день одного источника на **поддельном клиенте** (без
сети) и **in-memory** соединении DuckDB, с **инъектируемым** ``sleep`` (без реальных пауз):
happy-path полного цикла (AC #1), терминальные статусы выгрузки → fail (AC #2), запись под
локом / fail-fast при занятом локе (AC #3), оба источника независимо (AC #4), ограниченный
poll — таймаут / лимит подряд-ошибок / сброс счётчика на успехе (AC #5), частичные части →
день не персистится (AC #6), ``clean`` после коммита падает → WARNING, день остаётся (AC #7),
жёсткая сверка строк в реальном цикле, контракт единого сплиттера с ``count_part_rows``,
честно пустой день, граница дат (будущий день → fail-loud) и анти-зависимость (по реальным
import-узлам через ``ast``: нет ``pandas``/``polars``/``numpy``/``pyarrow`` и directaiq-инфры).

Импорт p81 — через :func:`importlib.import_module` (каталог ``scripts/8x_metrica_logs_api``
начинается с цифры → ``import scripts.8x_…`` как statement = ``SyntaxError``; это образец и
для CLI ``update`` 2.9). Корень хранилища — ``monkeypatch.setenv`` на ``tmp_path`` (кросс-
платформенно). Live-набор — отдельно в ``test_p81_orchestrator_live.py`` (реальный API).
"""

from __future__ import annotations

import ast
import importlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from scripts.utils.catalog import Catalog, CatalogField
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.dates import format_date, moscow_yesterday
from scripts.utils.env_reader import DATA_ROOT_ENV, MetricaCredentials
from scripts.utils.load_state import ensure_load_state_table
from scripts.utils.row_check import RowCountMismatchError, count_part_rows
from scripts.utils.writer_lock import WriterLockHeldError, writer_lock

# Digit-префикс пакета: импорт строкой через importlib (риск №3). Образец для 2.9.
p81 = importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")


# --- Фикстуры данных: мини-каталог и канонические TSV-части ------------------------------


def _catalog() -> Catalog:
    """Мини-каталог: visits (visit_id/date_time/watch_ids) + hits (watch_id)."""
    return Catalog(
        fields=(
            CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", "Идентификатор визита"),
            CatalogField("visits", "date_time", "ym:s:dateTime", "TIMESTAMP", "Дата/время визита"),
            CatalogField("visits", "watch_ids", "ym:s:watchIDs", "HUGEINT[]", "Просмотры визита"),
            CatalogField("hits", "watch_id", "ym:pv:watchID", "HUGEINT", "Идентификатор события"),
        )
    )


_VISITS_HEADER = "ym:s:visitID\tym:s:dateTime\tym:s:watchIDs"
_VISITS_ROW1 = "17298374650000000001\t2026-05-20 12:34:56\t[8273645,8273646]"
_VISITS_ROW2 = "17298374650000000002\t2026-05-20 13:01:02\t[8273647]"
# Одна часть с заголовком + 2 строки-данные (как отдаёт download_log_request_part).
_VISITS_PART = (_VISITS_HEADER + "\n" + _VISITS_ROW1 + "\n" + _VISITS_ROW2 + "\n").encode("utf-8")

_HITS_HEADER = "ym:pv:watchID"
_HITS_PART = (_HITS_HEADER + "\n8273645\n8273646\n").encode("utf-8")  # 2 строки-данные


def _no_sleep(_seconds: float) -> None:
    """Шов сна как no-op — poll без реальных пауз в offline-тестах."""


def _yesterday() -> str:
    """Канон 'вчера по МСК' (валидная дата ≤ потолка для load_day)."""
    return format_date(moscow_yesterday())


class _FakeClient:
    """Поддельный :class:`MetricaClient`: канонические ответы цикла, без сети.

    ``get_script`` — сценарий ответов опроса: каждый элемент либо статус (``processing``/
    ``processed``/``canceled``/…), либо ``"error"`` (тогда ``get_log_request`` бросает
    ``RuntimeError``, как поднятый наружу терминальный сбой после retry клиента). Последний
    элемент «залипает» (clamp) — удобно для «всегда processing»/«всегда error».
    """

    def __init__(
        self,
        *,
        get_script: list[str] | None = None,
        parts: list[dict[str, Any]] | None = None,
        payloads: dict[int, bytes] | None = None,
        download_error_parts: set[int] | None = None,
        clean_error: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.get_script = get_script if get_script is not None else ["processed"]
        self.parts = parts if parts is not None else [{"part_number": 1}]
        self.payloads = payloads if payloads is not None else {1: _VISITS_PART}
        self.download_error_parts = download_error_parts or set()
        self.clean_error = clean_error
        self.events = events if events is not None else []
        self._get_idx = 0
        self.created = False
        self.cleaned = False
        self.downloaded: list[int] = []

    def create_log_request(
        self, date1: str, date2: str, fields: list[str], source: str = "visits",
        attribution: str = "CROSS_DEVICE_LAST_SIGNIFICANT",
    ) -> dict[str, Any]:
        self.created = True
        self.events.append("create")
        return {"log_request": {"request_id": 1, "status": "created"}}

    def get_log_request(self, request_id: int) -> dict[str, Any]:
        self.events.append("get")
        action = self.get_script[min(self._get_idx, len(self.get_script) - 1)]
        self._get_idx += 1
        if action == "error":
            raise RuntimeError("поддельный терминальный сбой опроса")
        if action == "processed":
            return {"status": "processed", "parts": self.parts}
        return {"status": action}

    def download_log_request_part(self, request_id: int, part_number: int) -> bytes:
        self.events.append(f"download:{part_number}")
        if part_number in self.download_error_parts:
            raise RuntimeError(f"поддельный сбой скачивания части {part_number}")
        self.downloaded.append(part_number)
        return self.payloads[part_number]

    def clean_log_request(self, request_id: int) -> dict[str, Any]:
        self.events.append("clean")
        if self.clean_error:
            raise RuntimeError("поддельный сбой очистки выгрузки")
        self.cleaned = True
        return {"status": "cleaned"}


def _fresh_conn() -> duckdb.DuckDBPyConnection:
    """In-memory соединение с готовой таблицей load_state (её заводит ingest_day/init в проде)."""
    conn = duckdb.connect()
    ensure_load_state_table(conn)
    return conn


def _load_state_row(
    conn: duckdb.DuckDBPyConnection, source: str, day: str
) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT status, row_count FROM load_state WHERE source = ? AND date = ?",
        [source, day],
    ).fetchone()


# --- AC #1: happy-path полного цикла create→poll→download→write→verify→load_state→clean ---


def test_load_day_happy_path_full_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Полный цикл: партиция создана, load_state loaded, clean ПОСЛЕ mark_loaded, возврат N (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    order: list[str] = []
    fake = _FakeClient(get_script=["processing", "processed"], events=order)

    # Шпион mark_loaded в ТУ ЖЕ ленту событий — доказать, что clean идёт ПОСЛЕ коммита (AC #7).
    real_mark_loaded = p81.mark_loaded

    def _spy_mark_loaded(conn: object, source: str, date: str, row_count: int) -> None:
        order.append("mark_loaded")
        real_mark_loaded(conn, source, date, row_count)

    monkeypatch.setattr(p81, "mark_loaded", _spy_mark_loaded)

    conn = _fresh_conn()
    day = _yesterday()
    written = p81.load_day(
        conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
    )

    assert written == 2
    partition = tmp_path / "data" / "raw" / "visits" / f"{day}.parquet"
    assert partition.is_file()
    assert _load_state_row(conn, "visits", day) == ("loaded", 2)
    assert fake.downloaded == [1]  # download вызван по каждой части
    assert fake.cleaned is True
    # Точка коммита (mark_loaded) строго ДО clean (AC #7: clean — не часть коммита).
    assert order.index("mark_loaded") < order.index("clean")


def test_load_day_downloads_every_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Многочастная выгрузка: download вызван по КАЖДОЙ части; строки склеены (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    parts = [{"part_number": 1}, {"part_number": 2}]
    payloads = {
        1: (_VISITS_HEADER + "\n" + _VISITS_ROW1 + "\n").encode("utf-8"),  # 1 строка
        2: (_VISITS_HEADER + "\n" + _VISITS_ROW2 + "\n").encode("utf-8"),  # 1 строка
    }
    fake = _FakeClient(parts=parts, payloads=payloads)

    conn = _fresh_conn()
    day = _yesterday()
    written = p81.load_day(
        conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
    )

    assert written == 2  # 1 + 1, заголовок каждой части вычтен
    assert fake.downloaded == [1, 2]
    assert _load_state_row(conn, "visits", day) == ("loaded", 2)


# --- AC #2: processing_failed/canceled → fail с диагностикой, без молчаливого пропуска ---


@pytest.mark.parametrize("terminal_status", ["processing_failed", "canceled"])
def test_load_day_terminal_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_status: str
) -> None:
    """Терминальный статус выгрузки → RuntimeError; партиция не создана, день не loaded (AC #2)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient(get_script=[terminal_status])

    conn = _fresh_conn()
    day = _yesterday()
    with pytest.raises(RuntimeError) as exc_info:
        p81.load_day(
            conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
        )
    assert terminal_status in str(exc_info.value)

    partition = tmp_path / "data" / "raw" / "visits" / f"{day}.parquet"
    assert not partition.exists()
    row = _load_state_row(conn, "visits", day)
    assert row is not None and row[0] != "loaded"  # mark_failed (защитная отметка, риск №12)


# --- AC #3: запись под .writer.lock (run-level ingest_day) -------------------------------


def test_ingest_day_fails_fast_when_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Лок занят живым писателем → ingest_day fail-fast, без записи (AC #3)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    day = _yesterday()

    # Держим лок по дефолтному пути ({root}/.writer.lock) — ingest_day берёт тот же путь.
    with writer_lock(lock_path=tmp_path / ".writer.lock"):
        with pytest.raises(WriterLockHeldError):
            p81.ingest_day("visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep)

    # Запись не дошла: ни партиции, ни БД (лок брался ДО conn/клиента).
    assert not (tmp_path / "data" / "raw" / "visits").exists()


def test_ingest_day_happy_path_takes_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ingest_day проводит день под локом и освобождает лок на выходе (AC #1, #3).

    Run-level изолирован: креды/клиент/create_views подменены (monkeypatch), реальны лок и БД.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient()
    monkeypatch.setattr(
        p81, "read_metrica_credentials", lambda: MetricaCredentials(token="t", counter_id=1)
    )
    monkeypatch.setattr(p81, "MetricaClient", lambda **kwargs: fake)
    monkeypatch.setattr(p81, "create_views", lambda conn, **kwargs: None)

    day = _yesterday()
    written = p81.ingest_day(
        "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
    )

    assert written == 2
    partition = tmp_path / "data" / "raw" / "visits" / f"{day}.parquet"
    assert partition.is_file()
    # Лок освобождён — повторный захват того же пути успешен (иначе WriterLockHeldError).
    with writer_lock(lock_path=tmp_path / ".writer.lock"):
        pass


# --- AC #4: оба источника грузятся независимо -------------------------------------------


def test_both_sources_load_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """visits и hits — две независимые партиции + две строки load_state (AC #4)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    conn = _fresh_conn()
    day = _yesterday()

    visits_client = _FakeClient(payloads={1: _VISITS_PART})
    hits_client = _FakeClient(payloads={1: _HITS_PART})

    n_visits = p81.load_day(
        conn, visits_client, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
    )
    n_hits = p81.load_day(
        conn, hits_client, "hits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
    )

    assert n_visits == 2 and n_hits == 2
    assert (tmp_path / "data" / "raw" / "visits" / f"{day}.parquet").is_file()
    assert (tmp_path / "data" / "raw" / "hits" / f"{day}.parquet").is_file()
    assert _load_state_row(conn, "visits", day) == ("loaded", 2)
    assert _load_state_row(conn, "hits", day) == ("loaded", 2)


# --- AC #5: poll ограничен — таймаут / лимит подряд-ошибок / сброс счётчика --------------


def test_poll_timeout_not_infinite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Статус навсегда 'processing' + крошечный таймаут → RuntimeError «таймаут» (AC #5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient(get_script=["processing"])  # всегда processing (clamp на последний)

    conn = _fresh_conn()
    day = _yesterday()
    with pytest.raises(RuntimeError, match="Таймаут"):
        p81.load_day(
            conn, fake, "visits", day, catalog=_catalog(),
            poll_interval_s=0.0, poll_timeout_s=0.01, sleep=_no_sleep,
        )
    assert not (tmp_path / "data" / "raw" / "visits" / f"{day}.parquet").exists()


def test_poll_consecutive_errors_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Опрос всегда падает → после лимита подряд-ошибок RuntimeError, не вечный цикл (AC #5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient(get_script=["error"])  # всегда error

    conn = _fresh_conn()
    day = _yesterday()
    with pytest.raises(RuntimeError, match="подряд"):
        p81.load_day(
            conn, fake, "visits", day, catalog=_catalog(),
            poll_interval_s=0.0, max_consecutive_errors=3, sleep=_no_sleep,
        )


def test_poll_error_counter_resets_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Успешный опрос сбрасывает счётчик подряд-ошибок: 2 ошибки вразбивку не валят (AC #5).

    max=2; сценарий error→processing→error→processed. Без сброса 2 ошибки → fail; со сбросом
    подряд-счётчик не превышает 1 → день догружается.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient(get_script=["error", "processing", "error", "processed"])

    conn = _fresh_conn()
    day = _yesterday()
    written = p81.load_day(
        conn, fake, "visits", day, catalog=_catalog(),
        poll_interval_s=0.0, max_consecutive_errors=2, sleep=_no_sleep,
    )
    assert written == 2
    assert _load_state_row(conn, "visits", day) == ("loaded", 2)


# --- AC #6: частичные части → день НЕ персистится ---------------------------------------


def test_partial_parts_not_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Часть 2 не скачалась → исключение ДО записи; ни партиции, ни .tmp, день не loaded (AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    parts = [{"part_number": 1}, {"part_number": 2}]
    payloads = {1: _VISITS_PART}  # часть 2 в download_error_parts — payload не нужен
    fake = _FakeClient(parts=parts, payloads=payloads, download_error_parts={2})

    conn = _fresh_conn()
    day = _yesterday()
    with pytest.raises(RuntimeError, match="части 2"):
        p81.load_day(
            conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
        )

    source_dir = tmp_path / "data" / "raw" / "visits"
    # Ни .parquet, ни .tmp мусора (части держались в памяти, запись не начиналась).
    assert not source_dir.exists() or list(source_dir.iterdir()) == []
    row = _load_state_row(conn, "visits", day)
    assert row is not None and row[0] != "loaded"


def test_download_all_parts_missing_part_number_fails_loud() -> None:
    """Часть без 'part_number' → понятный RuntimeError, а не сырой KeyError (патч ревью).

    Реальный Logs API ключ всегда отдаёт; но при некорректном ответе диагностика должна быть
    в стиле модуля (с req_id и содержимым части), а не голый KeyError.
    """
    fake = _FakeClient()
    with pytest.raises(RuntimeError, match="part_number"):
        p81._download_all_parts(fake, 1, {"parts": [{"no_number": 1}]})


# --- AC #7: clean после коммита падает → WARNING, день остаётся загруженным --------------


def test_clean_failure_after_commit_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """clean бросает ПОСЛЕ mark_loaded → load_day НЕ падает, день остаётся loaded, WARNING (AC #7)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient(clean_error=True)

    conn = _fresh_conn()
    day = _yesterday()
    with caplog.at_level(logging.WARNING):
        written = p81.load_day(
            conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
        )

    assert written == 2  # день загружен несмотря на сбой clean
    assert (tmp_path / "data" / "raw" / "visits" / f"{day}.parquet").is_file()
    assert _load_state_row(conn, "visits", day) == ("loaded", 2)
    assert any("очистить выгрузку" in r.message for r in caplog.records if r.levelno >= logging.WARNING)


def test_clean_failure_non_runtime_exception_still_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """clean бросает НЕ RuntimeError/OSError (напр. ValueError) → день остаётся loaded (патч AC #7).

    Реальный клиент оборачивает всё в RuntimeError, но контракт AC #7 — «после коммита clean
    НИКОГДА не валит прогон» — обязан держаться при ЛЮБОМ типе исключения уборки (широкий
    except умышленный: уже-загруженный день не должен стать провалом из-за уборки квоты).
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient()

    def _boom_clean(request_id: int) -> dict[str, Any]:
        raise ValueError("неожиданный тип ошибки уборки")

    monkeypatch.setattr(fake, "clean_log_request", _boom_clean)

    conn = _fresh_conn()
    day = _yesterday()
    with caplog.at_level(logging.WARNING):
        written = p81.load_day(
            conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
        )

    assert written == 2  # день загружен несмотря на нестандартный сбой clean
    assert _load_state_row(conn, "visits", day) == ("loaded", 2)
    assert any("очистить выгрузку" in r.message for r in caplog.records if r.levelno >= logging.WARNING)


# --- Сверка-fail (интеграция 2.3 в реальном цикле) --------------------------------------


def test_row_count_mismatch_blocks_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """actual != expected → RowCountMismatchError; mark_loaded не вызван (гейт 2.3 в цикле)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient()

    # Подделываем запись: вернёт на 1 строку больше, чем реально → сверка не сойдётся.
    real_write = p81.write_partition

    def _write_plus_one(*args: Any, **kwargs: Any) -> int:
        return real_write(*args, **kwargs) + 1

    monkeypatch.setattr(p81, "write_partition", _write_plus_one)

    conn = _fresh_conn()
    day = _yesterday()
    with pytest.raises(RowCountMismatchError):
        p81.load_day(
            conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
        )
    row = _load_state_row(conn, "visits", day)
    assert row is not None and row[0] != "loaded"  # mark_loaded не достигнут


# --- Контракт единого сплиттера: число распарсенных p81 строк == count_part_rows (риск №2) -


def test_parse_parts_matches_count_part_rows() -> None:
    """Парсинг p81 и счёт сверки режут строки одним сплиттером → согласованы по границам (defer 2.3).

    Часть со встроенным CRLF/хвостовым переводом/пустой строкой: число распарсенных p81
    строк-данных РАВНО count_part_rows(part). Доказывает отсутствие off-by-N misfire.
    """
    part = (
        _VISITS_HEADER + "\r\n"
        + _VISITS_ROW1 + "\r\n"
        + "\r\n"  # пустая строка между записями
        + _VISITS_ROW2 + "\r\n"
    ).encode("utf-8")
    header, rows = p81._parse_parts([part])
    assert header == _VISITS_HEADER.split("\t")
    assert len(rows) == count_part_rows(part) == 2


def test_parse_parts_rejects_divergent_headers() -> None:
    """Заголовки частей расходятся (та же ширина, иной порядок) → RuntimeError ДО записи (патч ревью).

    _parse_parts отбрасывает заголовок КАЖДОЙ части, полагая их одинаковыми. Часть с другим
    порядком колонок при той же ширине иначе тихо рассогласовала бы ячейки — а счётчики строк
    сошлись бы (verify прошла бы) → порча закоммитилась бы незаметно. Жёсткий fail её ловит.
    """
    part1 = (_VISITS_HEADER + "\n" + _VISITS_ROW1 + "\n").encode("utf-8")
    divergent = "ym:s:dateTime\tym:s:visitID\tym:s:watchIDs"  # тот же набор, иной порядок (ширина 3)
    part2 = (divergent + "\n" + _VISITS_ROW2 + "\n").encode("utf-8")
    with pytest.raises(RuntimeError, match="аголовки частей"):
        p81._parse_parts([part1, part2])


# --- Честно пустой день (0 частей) → колонки из каталога, день loaded --------------------


def test_empty_day_zero_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """processed с 0 частей → колонки из каталога, rows=[], verify(0,0), день loaded (риск №8)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient(parts=[], payloads={})

    conn = _fresh_conn()
    day = _yesterday()
    written = p81.load_day(
        conn, fake, "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
    )

    assert written == 0
    partition = tmp_path / "data" / "raw" / "visits" / f"{day}.parquet"
    assert partition.is_file()  # пустая ТИПИЗИРОВАННАЯ партиция со схемой
    assert _load_state_row(conn, "visits", day) == ("loaded", 0)
    # Колонки — из каталога (схема пустого дня), не пусто.
    con = duckdb.connect()
    try:
        names = [c[0] for c in con.execute(f"SELECT * FROM read_parquet('{partition.as_posix()}')").description]
    finally:
        con.close()
    assert names == ["visit_id", "date_time", "watch_ids"]
    assert fake.cleaned is True


# --- Граница дат: будущий день → fail-loud, без создания запроса (риск №10) --------------


def test_future_date_fails_loud_without_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дата позже вчера по МСК → ValueError ДО create_log_request (не молчаливый clamp, риск №10)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient()

    conn = _fresh_conn()
    future = format_date(moscow_yesterday() + timedelta(days=2))
    with pytest.raises(ValueError, match="позже вчера"):
        p81.load_day(
            conn, fake, "visits", future, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
        )
    assert fake.created is False  # запрос не создавался


def test_invalid_source_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Неизвестный источник → ValueError ДО любых сетевых вызовов."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient()
    conn = _fresh_conn()
    with pytest.raises(ValueError, match="source"):
        p81.load_day(
            conn, fake, "sessions", _yesterday(), catalog=_catalog(),
            poll_interval_s=0.0, sleep=_no_sleep,
        )
    assert fake.created is False


# --- Анти-зависимость: своя оркестрация, без тяжёлого стека и инфры directaiq ------------


def test_no_heavy_or_directaiq_infra_imported() -> None:
    """Нет import pandas/polars/numpy/pyarrow и directaiq-инфры (своя оркестрация, NFR-6).

    Не по подстроке (docstring модуля упоминает directaiq/BaseScript) — парсим AST и смотрим
    реальные import-узлы по корню имени. duckdb и scripts.utils.* разрешены (p81 их склеивает).
    """
    source = Path(p81.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    forbidden = {
        "pandas",
        "polars",
        "numpy",
        "pyarrow",
        "config_manager",
        "base_script",
        "view_builders",
    }
    offenders = {n for n in imported if n.split(".")[0] in forbidden}
    assert not offenders, f"запрещённые импорты в p81_load_logs: {offenders}"


# --- Регрессия #3: view свежезагруженного источника отражает партиции (пере-сборка ПОСЛЕ) --


def test_ingest_range_view_reflects_loaded_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После ingest_range view источника отражает свежие партиции, а не пуст (регрессия #3).

    Баг: ``create_views`` зовётся ДО ``load_day``; для источника без партиций
    ``build_view_ddl`` даёт статичную пустышку (``WHERE false``). Без пересборки ПОСЛЕ записи
    первый прогон оставлял данные на диске, но view возвращал 0 строк (тихо неверно). Здесь
    ``create_views`` НЕ подменяется, а итог читается новым read-only соединением (как канал
    MCP-чтения): до фикса assert упал бы на 0.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient()  # 1 часть, 2 строки visits
    monkeypatch.setattr(
        p81, "read_metrica_credentials", lambda: MetricaCredentials(token="t", counter_id=1)
    )
    monkeypatch.setattr(p81, "MetricaClient", lambda **kwargs: fake)

    day = _yesterday()
    result = p81.ingest_range(
        "visits", day, day, catalog=_catalog(),
        hot_window_days=0, poll_interval_s=0.0, sleep=_no_sleep,
    )
    assert result.total_rows == 2

    # Свежее read-only соединение (как MCP-чтение) — view не пустышка, отражает партицию.
    with DatabaseManager.connection(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM visits").fetchone()[0] == 2


def test_ingest_day_view_reflects_loaded_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После ingest_day view источника отражает записанную партицию, а не пуст (регрессия #3)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    fake = _FakeClient()
    monkeypatch.setattr(
        p81, "read_metrica_credentials", lambda: MetricaCredentials(token="t", counter_id=1)
    )
    monkeypatch.setattr(p81, "MetricaClient", lambda **kwargs: fake)

    day = _yesterday()
    written = p81.ingest_day(
        "visits", day, catalog=_catalog(), poll_interval_s=0.0, sleep=_no_sleep
    )
    assert written == 2

    with DatabaseManager.connection(read_only=True) as con:
        assert con.execute("SELECT count(*) FROM visits").fetchone()[0] == 2
