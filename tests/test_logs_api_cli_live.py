"""Live-smoke CLI-пути ``evaluate`` против РЕАЛЬНОГО Logs API (история 1.6).

Обязателен по project-context: моки не отражают реальный контракт API — он
расходится незаметно. Уникальная ценность этого теста поверх live-smoke клиента
(1.3 бьёт только ``get_counter_info``): подтвердить, что **полный набор каталожных
полей** (``load_catalog().metrica_fields(source)``, 115 полей) **принят** настоящим
Logs API — т.е. список полей каталога-SSOT не разошёлся с API.

Дёшево и безопасно — через ``evaluate`` (GET-оценка): НЕ жжёт create/download-квоту,
узкое окно в 1 день (``date1 = date2 = вчера по МСК``), по одному запросу на источник.

По умолчанию ВЫКЛЮЧЕН (``addopts = "-m 'not live'"``) — в CI не гоняется. Явный
ручной прогон (нужны креды в ``.env`` per-game хранилища)::

    uv run pytest -m live

Нет кредов → ``pytest.skip`` с понятной причиной (не ложный красный в CI/без ``.env``).
"""

from __future__ import annotations

import argparse

import pytest

from scripts.tools.logs_api_cli import LogsApiCLI
from scripts.utils.dates import format_date, moscow_yesterday
from scripts.utils.env_reader import read_metrica_credentials


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
