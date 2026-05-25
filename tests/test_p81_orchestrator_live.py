"""Live-smoke оркестратора p81 против РЕАЛЬНОГО Logs API (история 2.7).

**Обязателен** по project-context: p81 — первый end-to-end цикл против реального API после
1.6, а моки не отражают реальный контракт (имена/типы полей, формат TSV, статусы
асинхронного цикла) — он расходится незаметно. Этот тест гоняет полный реальный цикл
``ingest_day`` за **узкое окно в 1 день** (вчера по МСК): create→poll→download→write→verify→
load_state→clean, уважая rate-limit (≤5000/day) и реальный асинхронный poll (~30s).

По умолчанию ВЫКЛЮЧЕН (``addopts = "-m 'not live'"`` в pyproject) — в CI не гоняется.
Явный ручной прогон (нужны креды + ``GDAU_DATA_ROOT`` per-game хранилища)::

    uv run pytest -m live

Нет кредов / нет корня хранилища → ``pytest.skip`` с понятной причиной (не ложный красный).

**Освежение фикстур.** Реальный wire-формат TSV (разделитель, заголовок-на-часть, формат
массивов ``[v1,v2]``/``[]`` и экранирование строковых массивов — неподтверждённая предпосылка
решения 2.6 вариант A) подтверждается именно здесь. После успешного прогона обновляй
``tests/fixtures/logs_visits_sample.tsv`` / ``tests/fixtures/logs_hits_sample.tsv`` из
реального ответа, чтобы offline-моки не расходились с API (закрытие defer 2.3).

**Внимание: запись в реальное хранилище.** Тест пишет партицию вчерашнего дня в
``GDAU_DATA_ROOT`` (идемпотентно — перезапись одного файла) под ``.writer.lock``. Это
осознанный side-effect live-smoke (opt-in, ручной).
"""

from __future__ import annotations

import importlib

import duckdb
import pytest

from scripts.utils.catalog import load_catalog
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.dates import format_date, moscow_yesterday
from scripts.utils.env_reader import read_metrica_credentials
from scripts.utils.paths import get_raw_partition_path, get_storage_root

# Digit-префикс пакета: импорт строкой через importlib (риск №3).
p81 = importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")


def _skip_if_no_environment() -> None:
    """Skip live-smoke без кредов Метрики или без корня хранилища (не ложный красный)."""
    try:
        read_metrica_credentials()
        get_storage_root()
    except ValueError as exc:
        pytest.skip(f"нет кредов/хранилища для live-smoke ({exc}) — пропущено")


def _assert_loaded_and_catalog_contract(source: str, day: str, written: int) -> None:
    """Партиция создана, load_state loaded, колонки партиции ⊆ storage-имён каталога."""
    partition = get_raw_partition_path(source, day)
    assert partition.is_file(), f"партиция {partition} не создана"

    with DatabaseManager.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT status, row_count FROM load_state WHERE source = ? AND date = ?",
            [source, day],
        ).fetchone()
    assert row is not None, "строки load_state нет — день не отмечен"
    assert row[0] == "loaded", f"статус {row[0]!r} != 'loaded'"
    assert row[1] == written, f"row_count {row[1]} != возврата {written}"

    # Контракт API↔каталог: каждое родное поле ответа смаплено в каталоге (иначе
    # write_partition упал бы) → колонки партиции (storage-имена) ⊆ имён каталога источника.
    storage_names = set(load_catalog().duckdb_types(source).keys())
    con = duckdb.connect()
    try:
        names = [
            c[0]
            for c in con.execute(
                f"SELECT * FROM read_parquet('{partition.as_posix()}')"
            ).description
        ]
    finally:
        con.close()
    extra = set(names) - storage_names
    assert not extra, f"колонки партиции вне каталога {source}: {extra} (контракт разошёлся)"


@pytest.mark.live
def test_ingest_day_visits_live() -> None:
    """Реальный полный цикл ingest_day('visits', вчера): день загружен, контракт каталога сошёлся."""
    _skip_if_no_environment()
    day = format_date(moscow_yesterday())
    written = p81.ingest_day("visits", day)
    assert written >= 0
    _assert_loaded_and_catalog_contract("visits", day, written)


@pytest.mark.live
def test_ingest_day_hits_live() -> None:
    """Реальный полный цикл ingest_day('hits', вчера): второй источник независимо (AC #4)."""
    _skip_if_no_environment()
    day = format_date(moscow_yesterday())
    written = p81.ingest_day("hits", day)
    assert written >= 0
    _assert_loaded_and_catalog_contract("hits", day, written)
