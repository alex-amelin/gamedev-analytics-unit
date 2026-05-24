"""Live-smoke вендоренного клиента против РЕАЛЬНОГО Logs API (история 1.3).

Обязателен по project-context: моки не отражают реальный контракт API (имена/типы
полей, формат ответов) — он расходится незаметно. Этот тест дёргает реальный
Management API дешёвым info-методом (`get_counter_info`) — ОДИН запрос, уважает
rate-limit (≤5000/day).

По умолчанию ВЫКЛЮЧЕН (`addopts = "-m 'not live'"` в pyproject) — в CI не гоняется.
Явный ручной прогон (нужны креды в `.env` per-game хранилища)::

    uv run pytest -m live

Нет кредов → `pytest.skip` с понятной причиной (не ложный красный в CI/без `.env`).
"""

from __future__ import annotations

import pytest

from scripts.utils.env_reader import read_metrica_credentials
from scripts.utils.metrica_client import MetricaClient


@pytest.mark.live
def test_get_counter_info_live() -> None:
    """Реальный `get_counter_info` через креды 1.2 → непустой dict со счётчиком (AC #4)."""
    try:
        creds = read_metrica_credentials()
    except ValueError as exc:
        pytest.skip(f"нет кредов Метрики ({exc}) — live-smoke пропущен")

    client = MetricaClient(token=creds.token, counter_id=creds.counter_id)
    info = client.get_counter_info()

    assert isinstance(info, dict)
    assert info, "ответ Management API пуст — контракт расходится"
