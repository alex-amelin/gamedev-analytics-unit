"""Live-smoke диапазонного приёма ``ingest_range`` против РЕАЛЬНОГО Logs API (история 2.8).

Закрывает реальным контрактом риск, который в offline-наборе держится на моках/рассуждении:
**инкремент (FR-9) + hot-window (FR-11) при ПОВТОРНОМ прогоне того же диапазона**. Гоняет
``ingest_range`` за узкий диапазон 3 дня **дважды** и проверяет: второй прогон пропускает
подтверждённо-загруженные дни ВНЕ окна и **всё равно перезаливает** hot-window — то есть
``reconcile`` + дизъюнкция «в окне ИЛИ не загружен» работают на живых данных, а не только в
моках. Перезалив одного дня идемпотентен (FR-10) — повторный ``load_day`` → ``write_partition``.

Диапазон: ``[вчера-2 .. вчера]`` (3 дня, уважает rate-limit ≤5000/day и асинхронный poll ~30s);
``hot_window_days=1`` → окно ``[вчера, вчера]``, поэтому ``вчера-2``/``вчера-1`` лежат ВНЕ окна и
на втором прогоне пропускаются, а ``вчера`` (== якорь) перезаливается всегда. Один источник
(``visits`` — легче ``hits`` по объёму; оба источника зелёные по live-smoke 2.7).

По умолчанию ВЫКЛЮЧЕН (``addopts = "-m 'not live'"`` в pyproject) — в CI не гоняется. Ручной
прогон (нужны креды Метрики + ``GDAU_DATA_ROOT`` per-game хранилища)::

    uv run pytest -m live

Нет кредов / нет корня хранилища → ``pytest.skip`` с понятной причиной (не ложный красный).

**Устойчивость к предсуществующему состоянию.** Тест НЕ предполагает чистое хранилище: первый
прогон лишь ПРИВОДИТ диапазон в состояние «все три дня загружены» (что-то долит, что-то уже
было; ``вчера`` грузится всегда как окно) и подтверждает это по ``load_state``. Проверка риска —
на ВТОРОМ прогоне, чьё поведение детерминировано: skip вне окна + перезалив окна.

**Внимание: запись в реальное хранилище.** Тест пишет партиции трёх дней в ``GDAU_DATA_ROOT``
(идемпотентно — перезапись одного файла на день) под ``.writer.lock``. Осознанный side-effect
opt-in live-smoke (ручной).
"""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest

from scripts.utils.database_manager import DatabaseManager
from scripts.utils.dates import format_date, moscow_yesterday
from scripts.utils.env_reader import read_metrica_credentials
from scripts.utils.paths import get_raw_partition_path, get_storage_root

# Digit-префикс пакета: импорт строкой через importlib (как 2.7).
p81 = importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")


def _skip_if_no_environment() -> None:
    """Skip live-smoke без кредов Метрики или без корня хранилища (не ложный красный)."""
    try:
        read_metrica_credentials()
        get_storage_root()
    except ValueError as exc:
        pytest.skip(f"нет кредов/хранилища для live-smoke ({exc}) — пропущено")


def _loaded_row_count(source: str, day: str) -> int:
    """Число строк дня по ``load_state`` со ``status='loaded'`` (assert, что день отмечен)."""
    partition = get_raw_partition_path(source, day)
    assert partition.is_file(), f"партиция {partition} не создана — день не загружен"
    with DatabaseManager.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT row_count FROM load_state "
            "WHERE source = ? AND date = ? AND status = 'loaded'",
            [source, day],
        ).fetchone()
    assert row is not None, f"день {source}/{day} не помечен loaded в load_state"
    return int(row[0])


@pytest.mark.live
def test_ingest_range_increment_and_hot_window_live() -> None:
    """Реальный диапазон 3 дня, прогон ДВАЖДЫ: 2-й skip'ает вне окна (FR-9), перезаливает окно (FR-11)."""
    _skip_if_no_environment()
    source = "visits"
    y = moscow_yesterday()
    d_old = format_date(y - timedelta(days=2))  # вне окна (N=1) → на 2-м прогоне skip
    d_mid = format_date(y - timedelta(days=1))  # вне окна (N=1) → на 2-м прогоне skip
    d_hot = format_date(y)  # == якорь (вчера по МСК) → в окне [вчера] → перезалив всегда

    # --- Прогон 1: привести диапазон в состояние «все три дня загружены» ---
    # Окно (N=1) гарантирует d_hot в наборе; d_old/d_mid грузятся, если ещё не были загружены.
    first = p81.ingest_range(source, d_old, d_hot, hot_window_days=1)
    assert first.source == source
    assert d_hot in first.loaded_dates  # якорь всегда в наборе (окно)
    # Независимо от предсуществующего состояния — после прогона 1 все три дня подтверждённо loaded.
    rows_old = _loaded_row_count(source, d_old)
    rows_mid = _loaded_row_count(source, d_mid)
    rows_hot_first = _loaded_row_count(source, d_hot)
    assert rows_old >= 0 and rows_mid >= 0 and rows_hot_first >= 0

    # --- Прогон 2: тот же диапазон/окно — здесь проверяется сам риск ---
    second = p81.ingest_range(source, d_old, d_hot, hot_window_days=1)
    # FR-9 (инкремент): d_old/d_mid подтверждённо-загружены и ВНЕ окна → пропущены (не перекачаны).
    assert second.skipped_dates == [d_old, d_mid], (
        f"инкремент сломан: ожидался skip [{d_old}, {d_mid}], получено {second.skipped_dates}"
    )
    # FR-11 (hot-window): d_hot перезалит ВСЕГДА, хотя подтверждённо-загружен (дизъюнкция > skip).
    assert second.loaded_dates == [d_hot], (
        f"hot-window сломан: ожидался перезалив [{d_hot}], получено {second.loaded_dates}"
    )
    # Согласованность итога: суммарные строки 2-го прогона == строкам единственного загруженного дня.
    rows_hot_second = _loaded_row_count(source, d_hot)
    assert second.total_rows == rows_hot_second
    # FR-10 (идемпотентность): перезалив окна не разрушил пропущенные дни — они остались loaded.
    assert _loaded_row_count(source, d_old) == rows_old
    assert _loaded_row_count(source, d_mid) == rows_mid
