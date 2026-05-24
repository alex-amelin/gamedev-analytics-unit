"""Offline-тесты вендоренного клиента Logs API (история 1.3).

Покрывают дисциплину, а не только happy-path:
- шов кредов (конструктор инъекцией, токен не оседает в атрибутах — NFR-5);
- анти-зависимости (нет `polars`/`auth_manager`/`tapi_yandex*`/`logging_utils` в
  import-узлах — проверка по `ast`, не по подстроке: модульный docstring сам их
  упоминает → наивный поиск дал бы ложный красный);
- наличие 6 методов жизненного цикла Logs API + 3 info-методов и отсутствие
  вырезанных reporting/Direct/`upload_offline_conversions` (AC #2);
- happy-path каждого оставленного метода;
- сохранённое поведение retry/rate-limit: retry 503→200 (AC #3), терминальное
  исчерпание ретраев и connection-level (AC #5), не-ретраябельный 401/404 без
  бессмысленных ретраев (AC #6), дневной лимит, ошибка в теле ответа.

Без сети и без реального счётчика: HTTP мокается через `unittest.mock` (stdlib);
`responses`/`requests-mock` в зависимости намеренно НЕ добавляются (простота-первой).
`time.sleep` заглушён autouse-фикстурой — иначе retry-тесты реально спали бы
десятки секунд (самая частая ловушка при тестировании этого клиента).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest
import requests

from scripts.utils.metrica_client import MetricaClient

FIXTURES = Path(__file__).parent / "fixtures"


# --- Инфраструктура моков ----------------------------------------------------


@pytest.fixture(autouse=True)
def mock_sleep(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Заглушить `time.sleep` в модуле клиента (autouse).

    Без этого retry-тесты реально спят `_RETRY_DELAYS` (30/60/120с), а `_rate_limit` —
    до 1/30с. Возвращает мок, чтобы тесты ретраев могли проверить задержки.
    """
    sleep = MagicMock()
    monkeypatch.setattr("scripts.utils.metrica_client.time.sleep", sleep)
    return sleep


def _ok_response(json_body: dict[str, Any] | None = None, content: bytes = b"") -> MagicMock:
    """Mock-`Response` для успешного ответа (`raise_for_status` ничего не делает)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {} if json_body is None else json_body
    resp.content = content
    return resp


def _error_response(
    status_code: int,
    *,
    message: str | None = None,
    json_body: dict[str, Any] | None = None,
) -> MagicMock:
    """Mock-`Response`, чей `raise_for_status` бросает `HTTPError` с реальным текстом.

    Текст HTTPError несёт статус (как `raise_for_status` в реальном `requests`:
    «401 Client Error: ...») — это и есть «понятное сообщение» для AC #6.
    `e.response` указывает на этот же mock (с `.status_code` и `.json()`).
    """
    resp = MagicMock()
    resp.status_code = status_code
    text = message or f"{status_code} Client Error"
    err = requests.exceptions.HTTPError(text, response=resp)
    resp.raise_for_status.side_effect = err
    resp.json.return_value = {} if json_body is None else json_body
    resp.text = text
    return resp


@pytest.fixture
def client() -> MetricaClient:
    """Клиент с фиктивными кредами и подменённой `session` на `MagicMock`."""
    c = MetricaClient(token="tok-secret-xyz", counter_id=42)
    c.session = MagicMock()
    return c


# --- Шов конструктора (AC #1, NFR-5) ----------------------------------------


def test_constructor_injects_token_into_session_header() -> None:
    """Токен инъекцией уходит в заголовок сессии `Authorization: OAuth <token>` (AC #1)."""
    c = MetricaClient(token="tok-secret-xyz", counter_id=42)
    assert c.session.headers["Authorization"] == "OAuth tok-secret-xyz"
    assert c.counter_id == 42
    assert isinstance(c.counter_id, int)


def test_token_not_stored_in_attributes() -> None:
    """Токен не оседает в атрибутах/`repr` экземпляра — только в заголовке (NFR-5)."""
    c = MetricaClient(token="tok-secret-xyz", counter_id=42)
    assert not hasattr(c, "token")
    # session.__repr__ не дампит заголовки → секрет не утечёт через repr(__dict__).
    assert "tok-secret-xyz" not in repr(c.__dict__)


# --- Анти-зависимости (AC #1, #2) через ast, не подстроку --------------------


def test_no_forbidden_imports() -> None:
    """Среди реальных import-узлов нет вырезанных тяжёлых зависимостей (AC #1, #2).

    Намеренно по AST, а не по подстроке: модульный docstring упоминает `AuthManager`
    и `polars` (что обрезано) → наивный поиск дал бы ложный красный.
    """
    import scripts.utils.metrica_client as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("polars", "auth_manager", "tapi_yandex", "logging_utils", "config_manager")
    offenders = {name for name in imported if any(bad in name for bad in forbidden)}
    assert not offenders, f"запрещённые импорты в metrica_client: {offenders}"


# --- Поверхность методов: оставлено / вырезано (AC #2) -----------------------


@pytest.mark.parametrize(
    "method",
    [
        # жизненный цикл Logs API
        "create_log_request",
        "get_log_requests",
        "get_log_request",
        "download_log_request_part",
        "clean_log_request",
        "evaluate_log_request",
        # лёгкие management info-методы
        "get_counter_info",
        "get_counters",
        "get_goals",
    ],
)
def test_kept_methods_present(method: str) -> None:
    """Все 6 методов Logs API + 3 info-метода сохранены (AC #2)."""
    assert hasattr(MetricaClient, method)


@pytest.mark.parametrize(
    "method",
    [
        "get_search_queries_data",
        "collect_all_search_queries",
        "get_report",
        "get_report_paginated",
        "get_raw_report",
        "get_goals_detailed",
        "get_goals_stats",
        "get_direct_clients",
        "upload_offline_conversions",
        "_detect_table_prefix",
    ],
)
def test_cut_methods_absent(method: str) -> None:
    """Reporting/Stat/Direct/`upload_offline_conversions` вырезаны (AC #2)."""
    assert not hasattr(MetricaClient, method)


# --- Logs API happy-path -----------------------------------------------------


def test_create_log_request_posts_with_params(client: MetricaClient) -> None:
    """`create_log_request` шлёт POST на `/logrequests`, `fields` склеены через `,`."""
    client.session.post.return_value = _ok_response({"log_request": {"request_id": 1}})

    result = client.create_log_request(
        date1="2026-05-20",
        date2="2026-05-20",
        fields=["ym:s:visitID", "ym:s:dateTime"],
        source="visits",
    )

    assert result == {"log_request": {"request_id": 1}}
    client.session.post.assert_called_once()
    url = client.session.post.call_args.args[0]
    params = client.session.post.call_args.kwargs["params"]
    assert url.endswith("/management/v1/counter/42/logrequests")
    assert params["date1"] == "2026-05-20"
    assert params["date2"] == "2026-05-20"
    assert params["source"] == "visits"
    assert params["fields"] == "ym:s:visitID,ym:s:dateTime"


def test_get_log_requests_returns_list(client: MetricaClient) -> None:
    """`get_log_requests` достаёт список из ключа `requests`."""
    client.session.get.return_value = _ok_response({"requests": [{"request_id": 1}, {"request_id": 2}]})

    result = client.get_log_requests()

    assert result == [{"request_id": 1}, {"request_id": 2}]


def test_get_log_request_returns_status(client: MetricaClient) -> None:
    """`get_log_request` достаёт объект из ключа `log_request`."""
    client.session.get.return_value = _ok_response({"log_request": {"request_id": 7, "status": "processed"}})

    result = client.get_log_request(7)

    assert result == {"request_id": 7, "status": "processed"}


def test_evaluate_log_request_goes_through_make_request(client: MetricaClient) -> None:
    """`evaluate_log_request` ходит GET-ом через `make_request` и возвращает тело."""
    client.session.get.return_value = _ok_response({"log_request_evaluation": {"possible": True}})

    result = client.evaluate_log_request(
        date1="2026-05-20", date2="2026-05-20", fields=["ym:s:visitID"]
    )

    assert result == {"log_request_evaluation": {"possible": True}}


def test_clean_log_request_posts(client: MetricaClient) -> None:
    """`clean_log_request` шлёт POST на `/clean` и возвращает тело."""
    client.session.post.return_value = _ok_response({"log_request": {"status": "cleaned_by_user"}})

    result = client.clean_log_request(7)

    assert result == {"log_request": {"status": "cleaned_by_user"}}
    url = client.session.post.call_args.args[0]
    assert url.endswith("/management/v1/counter/42/logrequest/7/clean")


def test_get_counter_info_returns_dict(client: MetricaClient) -> None:
    """`get_counter_info` (info-метод) возвращает тело ответа Management API."""
    client.session.get.return_value = _ok_response({"counter": {"id": 42, "name": "test"}})

    result = client.get_counter_info()

    assert result == {"counter": {"id": 42, "name": "test"}}
    url = client.session.get.call_args.args[0]
    assert url.endswith("/management/v1/counter/42")


def test_download_log_request_part_returns_bytes(client: MetricaClient) -> None:
    """`download_log_request_part` возвращает сырые `bytes` (мини-TSV из фикстуры)."""
    tsv_bytes = (FIXTURES / "logs_visits_sample.tsv").read_bytes()
    client.session.get.return_value = _ok_response(content=tsv_bytes)

    result = client.download_log_request_part(request_id=7, part_number=0)

    assert isinstance(result, bytes)
    assert result == tsv_bytes


# --- Retry / rate-limit (AC #3, #5, #6) -------------------------------------
# В тестах ретраев глушим `_rate_limit`, чтобы `time.sleep` фиксировал ТОЛЬКО
# задержки backoff (иначе межзапросный rate-limit добавил бы лишние вызовы sleep).


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_retry_retryable_then_success(
    client: MetricaClient, mock_sleep: MagicMock, status: int
) -> None:
    """Любой код из `_RETRYABLE_STATUS_CODES` → ретрай → 200: данные вернулись, sleep(30) раз (AC #3).

    Параметризовано всеми четырьмя кодами {429,500,502,503}: AC #3 называет их явно,
    логика ретрая для всех одинакова — фиксируем членство каждого в наборе.
    """
    client._rate_limit = MagicMock()  # type: ignore[method-assign]
    client.session.get.side_effect = [
        _error_response(status),
        _ok_response({"counter": {"id": 42}}),
    ]

    result = client.get_counter_info()

    assert result == {"counter": {"id": 42}}
    assert client.session.get.call_count == 2
    mock_sleep.assert_called_once_with(30)


def test_retry_exhausted_raises(client: MetricaClient, mock_sleep: MagicMock) -> None:
    """Устойчивые 503 исчерпывают ретраи → RuntimeError; sleep вызван трижды (30, 60, 120) (AC #5).

    При `_MAX_RETRIES=4`: 1 первичная попытка + 3 ретрая (attempt 0/1/2 спят 30/60/120,
    attempt 3 падает). Все три задержки `_RETRY_DELAYS` задействованы.
    """
    client._rate_limit = MagicMock()  # type: ignore[method-assign]
    client.session.get.side_effect = [_error_response(503) for _ in range(4)]

    with pytest.raises(RuntimeError):
        client.get_counter_info()

    assert client.session.get.call_count == 4
    assert mock_sleep.call_args_list == [call(30), call(60), call(120)]


def test_connection_error_raises_immediately(client: MetricaClient, mock_sleep: MagicMock) -> None:
    """Ошибка сетевого уровня (нет `response`) → немедленный RuntimeError, без ретраев (AC #5)."""
    client._rate_limit = MagicMock()  # type: ignore[method-assign]
    client.session.get.side_effect = requests.exceptions.ConnectionError("DNS fail")

    with pytest.raises(RuntimeError):
        client.get_counter_info()

    assert client.session.get.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 404])
def test_non_retryable_http_fails_once(
    client: MetricaClient, mock_sleep: MagicMock, status: int
) -> None:
    """Не-ретраябельный HTTP (401/403/404) → RuntimeError сразу, 1 вызов, статус в тексте (AC #6)."""
    client._rate_limit = MagicMock()  # type: ignore[method-assign]
    client.session.get.side_effect = [
        _error_response(status, message=f"{status} Client Error: denied")
    ]

    with pytest.raises(RuntimeError, match=str(status)):
        client.get_counter_info()

    assert client.session.get.call_count == 1
    mock_sleep.assert_not_called()


def test_daily_limit_raises(client: MetricaClient) -> None:
    """Дневной лимит исчерпан → `_rate_limit` бросает RuntimeError (AC #3)."""
    client.requests_count_today = MetricaClient.MAX_REQUESTS_PER_DAY

    with pytest.raises(RuntimeError, match="Daily API limit"):
        client._rate_limit()


def test_check_response_errors_raises_on_error_body() -> None:
    """Тело с ключом `errors` → RuntimeError с текстом ошибки API."""
    with pytest.raises(RuntimeError, match="Metrica API error"):
        MetricaClient._check_response_errors({"errors": [{"text": "boom"}]})


def test_error_body_message_surfaced_in_runtimeerror(client: MetricaClient) -> None:
    """API-`message` из тела не-ретраябельного ответа попадает в текст RuntimeError."""
    client._rate_limit = MagicMock()  # type: ignore[method-assign]
    client.session.get.side_effect = [
        _error_response(403, message="403 Client Error", json_body={"message": "no access"})
    ]

    with pytest.raises(RuntimeError, match="no access"):
        client.get_counter_info()
