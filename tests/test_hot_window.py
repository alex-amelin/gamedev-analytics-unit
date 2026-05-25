"""Offline-тесты инкремента / hot-window / диапазонного приёма (история 2.8).

Покрывают **слой решения «какие дни грузить»** поверх готового цикла одного дня (2.7):

- чистое ядро :func:`_select_days_to_load` (главный тестируемый шов — без ``conn``/сети/часов,
  детерминизм инъекцией ``anchor``/``loaded``): инкремент по подтверждённо-загруженным (AC #1),
  hot-window последних N дней грузится всегда (AC #3), hot-window побеждает skip (AC #4),
  границы N — ``N=0`` off / ``N<0`` fail / ``N`` больше диапазона → клиппинг (AC #5),
  якорь = «вчера по МСК» и клиппинг к диапазону итерацией (риск №4);
- helper :func:`_iter_dates` (дни ``[date1, date2]`` включительно по возрастанию);
- run-level :func:`ingest_range` (вариант A — лок+conn+клиент **один раз**): порядок вызова
  ``load_day`` совпадает с набором дней, лок взят один раз (НЕ ``ingest_day`` в цикле —
  анти-реентрантность, риск №1), clamp ``date2``→вчера + валидация инверсии **до** лока
  (риск №8), сбой дня → проброс с сохранением закоммиченного хвоста (риск №6),
  :class:`IngestRangeResult` на смешанном диапазоне;
- анти-зависимость (по реальным import-узлам через ``ast``): новый код не вводит тяжёлого
  стека/инфры directaiq (NFR-6).

2.8 **не вводит нового сетевого контракта** (реальный цикл дня подтверждён live-smoke 2.7) —
здесь всё детерминированно offline: поддельный ``load_day`` + мок окружения, без сети и без
реальных пауз. Импорт p81 — через :func:`importlib.import_module` (digit-префикс каталога
``scripts/8x_metrica_logs_api`` → ``import scripts.8x_…`` как statement = ``SyntaxError``;
образец из ``test_p81_orchestrator.py`` 2.7 и для CLI ``update`` 2.9).
"""

from __future__ import annotations

import ast
import importlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.utils.env_reader import DATA_ROOT_ENV, MetricaCredentials
from scripts.utils.dates import format_date, moscow_today, moscow_yesterday
from scripts.utils.writer_lock import writer_lock

# Digit-префикс пакета: импорт строкой через importlib (как 2.7). Образец для 2.9.
p81 = importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")


# --- Чистое ядро _select_days_to_load (главный шов — без conn/сети/часов, риск №2) -------


def test_select_incremental_skips_loaded_outside_window() -> None:
    """AC #1: загруженные дни вне hot-window пропускаются, отсутствующие — в наборе (по возрастанию)."""
    # anchor далеко в будущем от диапазона → hot-window не пересекает [05-01, 05-04].
    loaded = frozenset({("visits", "2026-05-02"), ("visits", "2026-05-03")})
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 1),
        date(2026, 5, 4),
        hot_window_days=3,
        anchor=date(2026, 5, 20),
    )
    assert days == ["2026-05-01", "2026-05-04"]  # 02/03 загружены и вне окна → skip


def test_select_hot_window_always_reloaded_even_if_loaded() -> None:
    """AC #3: последние N дней в наборе ДАЖЕ если в loaded; дни до окна — по инкременту."""
    # range [04-28, 05-04], anchor 05-04, N=3 → окно [05-02, 05-04].
    loaded = frozenset(
        {
            ("visits", "2026-04-29"),  # вне окна, загружен → skip
            ("visits", "2026-05-02"),  # в окне, загружен → перезалив
            ("visits", "2026-05-03"),  # в окне, загружен → перезалив
            ("visits", "2026-05-04"),  # в окне, загружен → перезалив
        }
    )
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 4, 28),
        date(2026, 5, 4),
        hot_window_days=3,
        anchor=date(2026, 5, 4),
    )
    # 04-28 (нет/не окно) + 04-30, 05-01 (нет/не окно) + всё окно 05-02..05-04; 04-29 skip.
    assert days == ["2026-04-28", "2026-04-30", "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"]


def test_select_hot_window_beats_skip() -> None:
    """AC #4: день и в loaded, и в окне → в наборе (дизъюнкция — hot-window игнорирует loaded)."""
    loaded = frozenset({("visits", "2026-05-04")})
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 4),
        date(2026, 5, 4),
        hot_window_days=1,  # окно = [anchor, anchor] = [05-04]
        anchor=date(2026, 5, 4),
    )
    assert days == ["2026-05-04"]  # загружен, но в окне → перезалив


def test_select_n_zero_disables_window() -> None:
    """AC #5: N=0 → окно выключено, чистый инкремент (загруженный день вне набора)."""
    loaded = frozenset({("visits", "2026-05-04")})
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 3),
        date(2026, 5, 4),
        hot_window_days=0,
        anchor=date(2026, 5, 4),
    )
    assert days == ["2026-05-03"]  # 05-04 загружен, окно off → skip


def test_select_negative_n_raises() -> None:
    """AC #5: N<0 → понятная ValueError."""
    with pytest.raises(ValueError, match="hot_window_days"):
        p81._select_days_to_load(
            "visits",
            frozenset(),
            date(2026, 5, 1),
            date(2026, 5, 2),
            hot_window_days=-1,
            anchor=date(2026, 5, 2),
        )


def test_select_n_larger_than_range_clipped() -> None:
    """AC #5: N больше длины диапазона → клиппинг к [date1, date2] (без выхода за date1)."""
    loaded = frozenset({("visits", "2026-05-03"), ("visits", "2026-05-04")})
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 3),
        date(2026, 5, 4),
        hot_window_days=10,  # окно [04-25, 05-04], но итерируем только диапазон
        anchor=date(2026, 5, 4),
    )
    assert days == ["2026-05-03", "2026-05-04"]  # оба в окне; нет дней до 05-03


def test_select_historical_range_no_window_overlap() -> None:
    """Риск №4: исторический диапазон (date2 < anchor) → окно не пересекается → чистый инкремент."""
    loaded = frozenset({("visits", "2026-05-02")})
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 1),
        date(2026, 5, 3),
        hot_window_days=3,  # окно [05-18, 05-20] — не пересекает [05-01, 05-03]
        anchor=date(2026, 5, 20),
    )
    assert days == ["2026-05-01", "2026-05-03"]  # 05-02 загружен и вне окна → skip


def test_select_anchor_always_in_set_when_in_range() -> None:
    """Риск №4: при N>=1 и anchor ∈ [date1, date2] якорь всегда в наборе (даже загруженный)."""
    loaded = frozenset({("visits", "2026-05-04")})
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 1),
        date(2026, 5, 4),
        hot_window_days=1,
        anchor=date(2026, 5, 4),
    )
    assert "2026-05-04" in days


def test_select_source_isolation() -> None:
    """Ключ loaded — (source, date): загрузка hits НЕ скрывает тот же день для visits."""
    loaded = frozenset({("hits", "2026-05-02")})  # загружен hits, не visits
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 1),
        date(2026, 5, 3),
        hot_window_days=0,
        anchor=date(2026, 5, 20),
    )
    assert days == ["2026-05-01", "2026-05-02", "2026-05-03"]  # visits ничего не пропускает


def test_select_all_loaded_window_off_empty() -> None:
    """AC #1/#5: весь диапазон загружен + N=0 → пустой набор (грузить нечего, дёшевый повтор)."""
    loaded = frozenset({("visits", "2026-05-01"), ("visits", "2026-05-02")})
    days = p81._select_days_to_load(
        "visits",
        loaded,
        date(2026, 5, 1),
        date(2026, 5, 2),
        hot_window_days=0,  # окно off → ничего поверх инкремента не форсим
        anchor=date(2026, 5, 20),  # вне диапазона — окно и так не пересекло бы
    )
    assert days == []  # оба дня загружены и вне окна → пустой набор


# --- helper _iter_dates ------------------------------------------------------------------


def test_iter_dates_inclusive_ascending() -> None:
    """_iter_dates даёт дни [date1, date2] включительно по возрастанию."""
    got = list(p81._iter_dates(date(2026, 5, 1), date(2026, 5, 3)))
    assert got == [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]


def test_iter_dates_empty_when_inverted() -> None:
    """_iter_dates пуст при date1 > date2 (clamp в ingest_range инверсию уже не пускает)."""
    assert list(p81._iter_dates(date(2026, 5, 3), date(2026, 5, 1))) == []


# --- Интеграция ingest_range (вариант A — лок один раз; поддельный load_day) -------------


class _CountingLock:
    """Обёртка-счётчик над реальным writer_lock: фиксирует число входов в лок (риск №1)."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.entries = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.entries += 1
        return self._real(**kwargs)


def _isolate_range_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    loaded: frozenset[tuple[str, str]],
    load_record: list[str],
    fail_on: str | None = None,
    rows_per_day: int = 2,
) -> _CountingLock:
    """Изолировать ingest_range: реальные лок+БД, моки кредов/клиента/view/reconcile/load_day.

    Возвращает счётчик входов в лок (для проверки «лок один раз», риск №1). ``ingest_day``
    подменён на бросающий — доказывает, что диапазон НЕ зовёт его в цикле (анти-реентрантность).
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    monkeypatch.setattr(
        p81, "read_metrica_credentials", lambda: MetricaCredentials(token="t", counter_id=1)
    )
    monkeypatch.setattr(p81, "MetricaClient", lambda **kwargs: object())
    monkeypatch.setattr(p81, "create_views", lambda conn, **kwargs: None)

    def _fake_reconcile(conn: object, *, sources: Any) -> frozenset[tuple[str, str]]:
        return loaded

    monkeypatch.setattr(p81, "reconcile", _fake_reconcile)

    def _fake_load_day(
        conn: object, client: object, source: str, day: str, **kwargs: Any
    ) -> int:
        load_record.append(day)
        if fail_on is not None and day == fail_on:
            raise RuntimeError(f"поддельный сбой дня {day}")
        return rows_per_day

    monkeypatch.setattr(p81, "load_day", _fake_load_day)

    def _boom_ingest_day(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("ingest_range НЕ должен звать ingest_day (реентрантность лока)")

    monkeypatch.setattr(p81, "ingest_day", _boom_ingest_day)

    counting = _CountingLock(p81.writer_lock)
    monkeypatch.setattr(p81, "writer_lock", counting)
    return counting


def test_ingest_range_loads_selected_days_lock_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #1/#3: load_day зван по набору дней по возрастанию; лок взят РОВНО один раз (риск №1)."""
    record: list[str] = []
    y = moscow_yesterday()
    d0 = format_date(y - timedelta(days=2))
    d1 = format_date(y - timedelta(days=1))
    d2 = format_date(y)
    # d1 загружен и вне hot-window (N=0) → пропуск; d0/d2 грузятся.
    loaded = frozenset({("visits", d1)})
    counting = _isolate_range_env(monkeypatch, tmp_path, loaded=loaded, load_record=record)

    result = p81.ingest_range(
        "visits", d0, d2, hot_window_days=0, poll_interval_s=0.0, sleep=lambda _s: None
    )

    assert record == [d0, d2]  # d1 пропущен (загружен, окно off), порядок по возрастанию
    assert counting.entries == 1  # лок взят один раз на весь прогон (НЕ на каждый день)
    assert result.source == "visits"
    assert result.loaded_dates == [d0, d2]
    assert result.skipped_dates == [d1]
    assert result.total_rows == 4  # 2 дня × 2 строки


def test_ingest_range_hot_window_reloads_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #3: при N=3 последние 3 дня (до вчера) перезаливаются, даже будучи загруженными."""
    record: list[str] = []
    y = moscow_yesterday()
    days_all = [format_date(y - timedelta(days=k)) for k in (2, 1, 0)]
    # Все три дня «загружены», но все попадают в hot-window N=3 → все перезаливаются.
    loaded = frozenset((("visits", d) for d in days_all))
    _isolate_range_env(monkeypatch, tmp_path, loaded=loaded, load_record=record)

    result = p81.ingest_range(
        "visits", days_all[0], days_all[2], hot_window_days=3,
        poll_interval_s=0.0, sleep=lambda _s: None,
    )

    assert record == days_all  # все три перезалиты несмотря на loaded (hot-window > skip)
    assert result.loaded_dates == days_all
    assert result.skipped_dates == []


def test_ingest_range_clamps_date2_to_yesterday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Риск №8: date2=завтра → подрезан к вчера; сегодня/завтра НЕ грузятся (clamp до лока)."""
    record: list[str] = []
    y = moscow_yesterday()
    d_start = format_date(y - timedelta(days=2))
    tomorrow = format_date(moscow_today() + timedelta(days=1))
    _isolate_range_env(monkeypatch, tmp_path, loaded=frozenset(), load_record=record)

    result = p81.ingest_range(
        "visits", d_start, tomorrow, hot_window_days=0, poll_interval_s=0.0, sleep=lambda _s: None
    )

    expected = [format_date(y - timedelta(days=k)) for k in (2, 1, 0)]  # до вчера включительно
    assert record == expected
    assert format_date(moscow_today()) not in record  # сегодня не грузится
    assert tomorrow not in record  # завтра не грузится
    assert result.loaded_dates == expected


def test_ingest_range_inverted_range_raises_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Риск №8: инвертированный диапазон → ValueError ДО взятия лока (счётчик лока = 0)."""
    record: list[str] = []
    counting = _isolate_range_env(monkeypatch, tmp_path, loaded=frozenset(), load_record=record)

    with pytest.raises(ValueError):
        # date1 = сегодня (> потолка вчера) → clamp бросает инверсию до лока.
        p81.ingest_range(
            "visits",
            format_date(moscow_today()),
            format_date(moscow_today()),
            poll_interval_s=0.0,
            sleep=lambda _s: None,
        )

    assert counting.entries == 0  # лок не брался
    assert record == []  # ни один день не грузился


def test_ingest_range_negative_n_raises_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #5: N<0 → ValueError ДО лока (fail-fast, лок не брался)."""
    record: list[str] = []
    counting = _isolate_range_env(monkeypatch, tmp_path, loaded=frozenset(), load_record=record)
    y = moscow_yesterday()

    with pytest.raises(ValueError, match="hot_window_days"):
        p81.ingest_range(
            "visits", format_date(y - timedelta(days=1)), format_date(y),
            hot_window_days=-1, poll_interval_s=0.0, sleep=lambda _s: None,
        )

    assert counting.entries == 0
    assert record == []


def test_ingest_range_invalid_source_raises_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Невалидный source → ValueError ДО лока (fail-loud, лок не брался)."""
    record: list[str] = []
    counting = _isolate_range_env(monkeypatch, tmp_path, loaded=frozenset(), load_record=record)
    y = moscow_yesterday()

    with pytest.raises(ValueError, match="source"):
        p81.ingest_range(
            "sessions", format_date(y - timedelta(days=1)), format_date(y),
            poll_interval_s=0.0, sleep=lambda _s: None,
        )

    assert counting.entries == 0
    assert record == []


def test_ingest_range_day_failure_propagates_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Риск №6: сбой 2-го дня → проброс; 1-й закоммичен «остался», 3-й не начат, лок освобождён."""
    record: list[str] = []
    y = moscow_yesterday()
    d0 = format_date(y - timedelta(days=2))
    d1 = format_date(y - timedelta(days=1))
    d2 = format_date(y)
    # N=3 → все три в окне → все в наборе; load_day бросает на d1 (2-й день).
    counting = _isolate_range_env(
        monkeypatch, tmp_path, loaded=frozenset(), load_record=record, fail_on=d1
    )

    with pytest.raises(RuntimeError, match="поддельный сбой"):
        p81.ingest_range(
            "visits", d0, d2, hot_window_days=3, poll_interval_s=0.0, sleep=lambda _s: None
        )

    assert record == [d0, d1]  # d0 закоммичен, на d1 упали, d2 НЕ начат
    assert counting.entries == 1  # лок брался один раз
    # Лок освобождён (finally контекст-менеджера): повторный захват того же пути успешен.
    with writer_lock(lock_path=tmp_path / ".writer.lock"):
        pass


def test_ingest_range_result_mixed_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IngestRangeResult корректен на смешанном диапазоне: часть skip, часть hot-window."""
    record: list[str] = []
    y = moscow_yesterday()
    # Диапазон 5 дней; последние 2 — hot-window (N=2); среди старых 3 один загружен (skip).
    d = [format_date(y - timedelta(days=k)) for k in (4, 3, 2, 1, 0)]
    loaded = frozenset({("visits", d[1])})  # старый загруженный, вне окна → skip
    _isolate_range_env(monkeypatch, tmp_path, loaded=loaded, load_record=record)

    result = p81.ingest_range(
        "visits", d[0], d[4], hot_window_days=2, poll_interval_s=0.0, sleep=lambda _s: None
    )

    # Окно (N=2) = [d[3], d[4]]; d[1] загружен и вне окна → skip; d[0]/d[2] грузятся.
    assert result.loaded_dates == [d[0], d[2], d[3], d[4]]
    assert result.skipped_dates == [d[1]]
    assert result.total_rows == 8  # 4 дня × 2 строки
    assert record == [d[0], d[2], d[3], d[4]]


def test_ingest_range_all_loaded_window_off_no_load_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-9: весь диапазон загружен + N=0 → load_day НЕ зван, итог пустой (дёшевый идемпотентный повтор).

    Достижимая ветка: набор дней пуст, цикл `for day in days` — no-op. Лок всё равно берётся
    один раз (reconcile обязан под локом, чтобы узнать «всё загружено» — by-design).
    """
    record: list[str] = []
    y = moscow_yesterday()
    d0 = format_date(y - timedelta(days=1))
    d1 = format_date(y)
    loaded = frozenset({("visits", d0), ("visits", d1)})  # оба дня подтверждённо загружены
    counting = _isolate_range_env(monkeypatch, tmp_path, loaded=loaded, load_record=record)

    result = p81.ingest_range(
        "visits", d0, d1, hot_window_days=0, poll_interval_s=0.0, sleep=lambda _s: None
    )

    assert record == []  # load_day не зван ни разу (грузить нечего)
    assert result.loaded_dates == []
    assert result.total_rows == 0
    assert result.skipped_dates == [d0, d1]  # весь диапазон пропущен
    assert counting.entries == 1  # лок берётся один раз даже при пустой работе (reconcile под локом)


# --- Анти-зависимость: новый код не тянет тяжёлый стек / инфру directaiq (NFR-6) ---------


def test_no_heavy_or_directaiq_infra_imported() -> None:
    """Импорты 2.8 — только stdlib (datetime/collections.abc/typing) и scripts.utils.* (NFR-6).

    Парсим AST p81 и смотрим реальные import-узлы по корню имени (не по подстроке — docstring
    упоминает directaiq/BaseScript). duckdb и scripts.utils.* разрешены; pandas/polars/numpy/
    pyarrow/config_manager/base_script/view_builders — нет. Зеркало ast-теста 2.7/2.2.
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


def test_public_api_exposes_range_names() -> None:
    """Публичный контракт 2.8 экспортирован в __all__ (для импорта 2.9)."""
    for name in ("DEFAULT_HOT_WINDOW_DAYS", "ingest_range", "IngestRangeResult"):
        assert name in p81.__all__
        assert hasattr(p81, name)
    assert p81.DEFAULT_HOT_WINDOW_DAYS == 3  # FR-11 default
