"""Live-smoke CLI-путей против РЕАЛЬНОГО Logs API (истории 1.6 + 2.9).

Обязателен по project-context: моки не отражают реальный контракт API — он
расходится незаметно. Два пути:

- **``evaluate``** (1.6): подтверждает, что **полный набор каталожных полей**
  (``load_catalog().metrica_fields(source)``) **принят** настоящим Logs API — т.е.
  список полей каталога-SSOT не разошёлся с API. Дёшево (GET-оценка, НЕ жжёт
  create/download-квоту), узкое окно в 1 день, по запросу на источник.
- **``update``** (2.9): сквозной end-to-end команды обновления за диапазон через
  ``main()`` — реально доезжает в сырьё + рабочий слой (view'ы), код возврата 0;
  повтор идемпотентен (AC #3/SM-2). Это первый live команды ``update`` — закрывает
  её live-DoD «зелёным end-to-end», а не фактом запуска (LESSONS Сложность 1).

По умолчанию ВЫКЛЮЧЕН (``addopts = "-m 'not live'"``) — в CI не гоняется. Явный
ручной прогон (нужны креды в ``.env`` + ``GDAU_DATA_ROOT`` per-game хранилища)::

    uv run pytest -m live
    uv run pytest -m live tests/test_logs_api_cli_live.py -s --log-cli-level=INFO  # с прогрессом

Нет кредов / нет корня хранилища → ``pytest.skip`` с понятной причиной (не ложный
красный в CI/без ``.env``).

**Внимание — side-effects ``update`` (LESSONS Сложность 6):** ``update`` пишет партиции
в **реальное хранилище** (``GDAU_DATA_ROOT``) под ``.writer.lock`` и **тратит дневную
квоту** Logs API (create→poll→download→clean на каждый день каждого источника). Запись
идемпотентна (перезапись одного файла на день), но прерванный прогон оставляет
**осиротевшие** log-запросы на стороне Метрики (``clean`` выполняется только на
happy-path) — их при необходимости убирают вручную (``gdau-logs list``/``clean``).
"""

from __future__ import annotations

import argparse
import importlib
import sys

import pytest

from scripts.tools.logs_api_cli import LogsApiCLI, main
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.dates import format_date, moscow_yesterday
from scripts.utils.env_reader import read_metrica_credentials
from scripts.utils.paths import get_raw_partition_path, get_storage_root


@pytest.mark.live
@pytest.mark.parametrize("source", ["visits", "hits"])
def test_evaluate_accepts_catalog_fields_live(source: str) -> None:
    """Реальный ``evaluate`` с каталожными полями источника → ответ несёт ``possible``.

    Прогон полного пути CLI (каталог → клиент → API): успешная оценка с присутствующим
    ``possible`` доказывает, что поля каталога приняты настоящим API (битое поле дало бы
    ``RuntimeError`` 400 из клиента — тест бы упал).
    """
    try:
        read_metrica_credentials()
    except ValueError as exc:
        pytest.skip(f"нет кредов Метрики ({exc}) — live-smoke пропущен")

    yesterday = format_date(moscow_yesterday())
    args = argparse.Namespace(date1=yesterday, date2=yesterday, source=source)

    evaluation = LogsApiCLI()._handle_evaluate(args)

    assert isinstance(evaluation, dict)
    assert "possible" in evaluation, (
        f"ответ evaluate для {source} без ключа 'possible' — контракт каталог↔API разошёлся"
    )


# === Live-smoke команды update (история 2.9) ================================

# Digit-префикс пакета p81 — для справочного DEFAULT_HOT_WINDOW_DAYS; команда грузит его сама.
_p81 = importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")


def _skip_update_if_no_environment() -> None:
    """Skip без кредов Метрики ИЛИ без корня хранилища (update пишет в реальное хранилище)."""
    try:
        read_metrica_credentials()
        get_storage_root()
    except ValueError as exc:
        pytest.skip(f"нет кредов/хранилища для live-smoke update ({exc}) — пропущено")


def _loaded_row_count(source: str, day: str) -> int:
    """Число строк дня по ``load_state`` со ``status='loaded'`` (assert, что день реально лёг)."""
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


def _view_total_rows(source: str) -> int:
    """Сколько строк видит типизированный view источника (рабочий слой 2.6 отражает сырьё)."""
    with DatabaseManager.connection(read_only=True) as conn:
        row = conn.execute(f'SELECT count(*) FROM "{source}"').fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.live
def test_update_end_to_end_both_sources_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сквозной ``update`` за 1 день по обоим источникам → данные легли, view отражает, код 0 (AC #1).

    Гоняет ПОЛНУЮ командную поверхность через ``main()`` (parser→dispatch→_handle_update→
    ingest_range per source→load_day). Узкое окно в 1 день (``date1==date2==вчера по МСК``) —
    уважает квоту и асинхронный poll. Критерий live-DoD (LESSONS Сложность 1): закрывается
    только ЗЕЛЁНЫМ end-to-end — партиция создана, ``load_state`` помечен loaded (сверка строк
    внутри ``load_day`` сошлась), команда вернула 0 (``main()`` не бросила ``SystemExit``).
    """
    _skip_update_if_no_environment()
    yesterday = format_date(moscow_yesterday())

    # main() читает sys.argv; успех → НЕ бросает SystemExit (код 0). Сбой источника →
    # SystemExit(1) (агрегация), прерывание → SystemExit(130) — любой из них завалит тест.
    monkeypatch.setattr(sys, "argv", ["gdau-logs", "update", "--date1", yesterday, "--date2", yesterday])
    main()

    # Оба источника реально доехали: партиция дня + отметка loaded (== сверка строк сошлась).
    for source in ("visits", "hits"):
        rows = _loaded_row_count(source, yesterday)
        # Рабочий слой (view) отражает записанное сразу: типизированный view ≥ строк этого дня.
        assert _view_total_rows(source) >= rows, (
            f"view {source} не отражает загруженный день (view < строк дня)"
        )


@pytest.mark.live
def test_update_idempotent_repeat_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повтор ``update`` того же дня (visits) → код 0, база не сломана (AC #3, SM-2, FR-10/11).

    ``вчера по МСК`` == якорь hot-window (дефолт N=3) → день перезаливается ОБА раза
    (идемпотентный перезалив одного файла, FR-10), а не «уже загружено, ничего не делаем».
    Повторный прогон обязан остаться зелёным (код 0) и оставить день целым (то же число строк).
    """
    _skip_update_if_no_environment()
    assert _p81.DEFAULT_HOT_WINDOW_DAYS >= 1  # вчера попадает в окно → перезалив, не skip
    yesterday = format_date(moscow_yesterday())
    argv = ["gdau-logs", "update", "--date1", yesterday, "--date2", yesterday, "--source", "visits"]

    monkeypatch.setattr(sys, "argv", argv)
    main()  # прогон 1: довести/перезалить день
    rows_first = _loaded_row_count("visits", yesterday)

    monkeypatch.setattr(sys, "argv", argv)
    main()  # прогон 2: тот же день — должен остаться код 0 и не сломать базу
    rows_second = _loaded_row_count("visits", yesterday)

    # Идемпотентность: повторный перезалив того же дня дал то же число строк (база не задвоилась).
    assert rows_second == rows_first, (
        f"повтор update сломал день: было {rows_first} строк, стало {rows_second}"
    )
