"""Клиент Яндекс Метрики Logs API на чистом ``requests``.

vendored from directaiq @ 7718bd65, seam: creds injected

Перенесён из соседнего репозитория directaiq (``scripts/utils/metrica_client.py``)
с минимальной развязкой шва кредов и обрезкой лишнего:

- **шов кредов развязан** — конструктор больше не зовёт ``AuthManager`` изнутри, а
  принимает готовые ``token``/``counter_id`` инъекцией от env-ридера (1.2). Клиент
  не импортирует ``env_reader`` — креды инжектит вызывающая сторона (CLI 1.6);
- **обрезаны** агрегатный Stat/Reporting API, цели-отчёты, Direct-атрибуция,
  ``upload_offline_conversions`` и зависимость ``polars`` — остался единственный
  HTTP-транспорт к Logs API (на ``requests``) плюс лёгкие management-info-методы;
- **сохранены как есть** rate-limit (≤30 req/s, ≤5000/day) и retry/backoff на
  429/500/502/503 — устойчивость к лимитам не переписывается (NFR-3).

Это единственная точка HTTP-доступа к Logs API в проекте. Токен живёт только в
заголовке сессии — в атрибуты/логи/``repr`` не попадает (NFR-5).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, NoReturn

import requests

__all__ = ["MetricaClient"]

logger = logging.getLogger(__name__)


class MetricaClient:
    """Client for Yandex Metrica API with rate limiting."""

    BASE_URL = "https://api-metrika.yandex.net"
    MAX_REQUESTS_PER_SECOND = 30
    MAX_REQUESTS_PER_DAY = 5000
    MAX_ROWS_PER_REQUEST = 100000

    def __init__(self, token: str, counter_id: int) -> None:
        """
        Initialize Metrica client with injected credentials.

        Args:
            token: OAuth token (lives only in the session header, NFR-5)
            counter_id: Metrica counter ID (already validated by env-reader 1.2)
        """
        self.counter_id = counter_id

        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"OAuth {token}", "Content-Type": "application/json"}
        )
        del token  # токен живёт только в заголовке сессии (NFR-5)

        # Rate limiting
        self.last_request_time: float = 0.0
        self.requests_count_today = 0
        self.current_date = datetime.now().date()

    def _rate_limit(self) -> None:
        """Apply rate limiting: max 30 requests/second, 5000/day."""
        now = time.time()
        current_date = datetime.now().date()

        # Reset daily counter if new day
        if current_date != self.current_date:
            self.requests_count_today = 0
            self.current_date = current_date

        # Check daily limit
        if self.requests_count_today >= self.MAX_REQUESTS_PER_DAY:
            raise RuntimeError(f"Daily API limit exceeded ({self.MAX_REQUESTS_PER_DAY} requests)")

        # Apply per-second rate limiting
        time_since_last = now - self.last_request_time
        min_interval = 1.0 / self.MAX_REQUESTS_PER_SECOND

        if time_since_last < min_interval:
            sleep_time = min_interval - time_since_last
            logger.debug(f"Rate limiting: sleeping {sleep_time:.3f}s")
            time.sleep(sleep_time)

        self.last_request_time = time.time()
        self.requests_count_today += 1

    @staticmethod
    def _check_response_errors(data: dict[str, Any]) -> None:
        """Check API response body for error payloads.

        Raises:
            RuntimeError: If the response contains an ``errors`` key.
        """
        if "errors" in data:
            error_msg = "; ".join([err.get("text", "Unknown error") for err in data["errors"]])
            raise RuntimeError(f"Metrica API error: {error_msg}")

    # Retry configuration for transient HTTP errors.
    # _MAX_RETRIES=4 → 1 первичная попытка + 3 ретрая, задержки _RETRY_DELAYS[0..2]
    # (30/60/120) все задействованы. Поднято с вендоренного 3 по решению ревью
    # (story 1.3): при 3 все три задержки в списке не использовались (120 был мёртв),
    # а docstring/спека обещали «30s, 60s, 120s» — теперь поведение совпадает.
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503}
    _MAX_RETRIES = 4
    _RETRY_DELAYS = [30, 60, 120]

    def make_request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Make a request to Metrica API with error handling and retry for transient errors.

        Retries up to 3 times with exponential backoff (30s, 60s, 120s) for
        HTTP 429, 500, 502, 503 status codes.

        Args:
            endpoint: API endpoint path
            params: Request parameters

        Returns:
            API response data

        Raises:
            RuntimeError: If API request fails after all retries
        """
        url = f"{self.BASE_URL}{endpoint}"
        last_exception = None

        for attempt in range(self._MAX_RETRIES):
            self._rate_limit()

            try:
                logger.debug(f"Making request to {endpoint} with params: {params}")
                response = self.session.get(url, params=params, timeout=(10, 300))
                response.raise_for_status()

                data: dict[str, Any] = response.json()
                self._check_response_errors(data)

                return data

            except requests.exceptions.RequestException as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if status_code in self._RETRYABLE_STATUS_CODES and attempt < self._MAX_RETRIES - 1:
                    delay = self._RETRY_DELAYS[attempt]
                    logger.warning(
                        f"HTTP {status_code} on {endpoint}, attempt {attempt + 1}/{self._MAX_RETRIES}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                    last_exception = e
                    continue

                error_detail = f"HTTP request failed: {e}"
                if hasattr(e, "response") and e.response is not None:
                    try:
                        error_body = e.response.json()
                        if "message" in error_body:
                            error_detail += f" - API Message: {error_body['message']}"
                        elif "errors" in error_body:
                            errors = "; ".join([err.get("text", str(err)) for err in error_body["errors"]])
                            error_detail += f" - API Errors: {errors}"
                    except Exception:
                        error_detail += f" - Response body: {e.response.text[:200]}"
                raise RuntimeError(error_detail) from e
            except ValueError as e:
                raise RuntimeError(f"Invalid JSON response: {e}") from e

        # Should not reach here, but just in case
        raise RuntimeError(f"All {self._MAX_RETRIES} retries failed for {endpoint}") from last_exception

    def get_counter_info(self) -> dict[str, Any]:
        """
        Get information about the Metrica counter.

        Returns:
            Counter information
        """
        return self.make_request(f"/management/v1/counter/{self.counter_id}", {})

    def get_goals(self) -> dict[str, Any]:
        """
        Get all goals for the Metrica counter.

        Returns:
            Dictionary containing goals information from Management API

        Raises:
            RuntimeError: If API request fails
        """
        logger.debug(f"Fetching goals for counter {self.counter_id}")
        return self.make_request(f"/management/v1/counter/{self.counter_id}/goals", {})

    def get_counters(self) -> list[dict[str, Any]]:
        """
        Get list of all Metrica counters available to the user.

        Returns:
            List of counter dictionaries with id, name, site, status, permission

        Raises:
            RuntimeError: If API request fails
        """
        logger.debug("Fetching available Metrica counters")
        response: dict[str, Any] = self.make_request("/management/v1/counters", {})

        # Extract counters from response
        counters: list[dict[str, Any]] = []
        if "counters" in response:
            for counter in response["counters"]:
                counters.append(
                    {
                        "id": counter.get("id"),
                        "name": counter.get("name"),
                        "site": counter.get("site2", {}).get("site", ""),
                        "status": counter.get("status"),
                        "permission": counter.get("permission"),
                        "owner_login": counter.get("owner_login"),
                    }
                )

        logger.info(f"Found {len(counters)} available counter(s)")
        return counters

    # Logs API methods

    def create_log_request(
        self,
        date1: str,
        date2: str,
        fields: list[str],
        source: str = "visits",
        attribution: str = "CROSS_DEVICE_LAST_SIGNIFICANT",
    ) -> dict[str, Any]:
        """
        Create a new Logs API request.

        Args:
            date1: Start date (YYYY-MM-DD)
            date2: End date (YYYY-MM-DD)
            fields: List of fields to export
            source: Source of logs ('visits' or 'hits')
            attribution: Attribution model

        Returns:
            Created request information
        """
        endpoint = f"/management/v1/counter/{self.counter_id}/logrequests"
        params = {
            "date1": date1,
            "date2": date2,
            "fields": ",".join(fields),
            "source": source,
            "attribution": attribution,
        }

        # createLogRequest is a POST request
        self._rate_limit()
        url = f"{self.BASE_URL}{endpoint}"

        try:
            logger.debug(f"Creating log request for {source} ({date1} - {date2})")
            response = self.session.post(url, params=params, timeout=(10, 300))
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            self._check_response_errors(data)
            return data
        except requests.exceptions.RequestException as e:
            self._handle_request_error(e)
            raise  # unreachable, satisfies type checker

    def get_log_requests(self) -> list[dict[str, Any]]:
        """
        Get list of all log requests for the counter.

        Returns:
            List of log requests
        """
        response = self.make_request(f"/management/v1/counter/{self.counter_id}/logrequests", {})
        result: list[dict[str, Any]] = response.get("requests", response.get("allow_log_requests", []))
        return result

    def get_log_request(self, request_id: int) -> dict[str, Any]:
        """
        Get information about a specific log request.

        Args:
            request_id: ID of the log request

        Returns:
            Log request information
        """
        response = self.make_request(f"/management/v1/counter/{self.counter_id}/logrequest/{request_id}", {})
        result: dict[str, Any] = response.get("log_request", {})
        return result

    def download_log_request_part(self, request_id: int, part_number: int) -> bytes:
        """
        Download a specific part of a processed log request.

        Args:
            request_id: ID of the log request
            part_number: Part number to download

        Returns:
            Binary content of the part (TSV data)
        """
        endpoint = f"/management/v1/counter/{self.counter_id}/logrequest/{request_id}/part/{part_number}/download"
        self._rate_limit()
        url = f"{self.BASE_URL}{endpoint}"

        try:
            logger.debug(f"Downloading part {part_number} for request {request_id}")
            response = self.session.get(url, timeout=(10, 300))
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            self._handle_request_error(e)
            raise  # unreachable, satisfies type checker

    def clean_log_request(self, request_id: int) -> dict[str, Any]:
        """
        Clean (delete data of) a processed log request to free up quota.

        Args:
            request_id: ID of the log request

        Returns:
            API response
        """
        endpoint = f"/management/v1/counter/{self.counter_id}/logrequest/{request_id}/clean"
        self._rate_limit()
        url = f"{self.BASE_URL}{endpoint}"

        try:
            logger.debug(f"Cleaning request {request_id}")
            response = self.session.post(url, timeout=(10, 300))
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            self._check_response_errors(data)
            return data
        except requests.exceptions.RequestException as e:
            self._handle_request_error(e)
            raise  # unreachable, satisfies type checker

    def evaluate_log_request(
        self,
        date1: str,
        date2: str,
        fields: list[str],
        source: str = "visits",
    ) -> dict[str, Any]:
        """
        Evaluate possibility of creating a log request.

        Args:
            date1: Start date
            date2: End date
            fields: List of fields
            source: Source ('visits' or 'hits')

        Returns:
            Evaluation result
        """
        endpoint = f"/management/v1/counter/{self.counter_id}/logrequests/evaluate"
        params = {
            "date1": date1,
            "date2": date2,
            "fields": ",".join(fields),
            "source": source,
        }

        return self.make_request(endpoint, params)

    def _handle_request_error(self, e: requests.exceptions.RequestException) -> NoReturn:
        """Helper to process request errors consistent with make_request logic"""
        error_detail = f"HTTP request failed: {e}"
        if hasattr(e, "response") and e.response is not None:
            try:
                error_body = e.response.json()
                if "message" in error_body:
                    error_detail += f" - API Message: {error_body['message']}"
                elif "errors" in error_body:
                    errors = "; ".join([err.get("text", str(err)) for err in error_body["errors"]])
                    error_detail += f" - API Errors: {errors}"
            except Exception:
                error_detail += f" - Response body: {e.response.text[:200]}"
        raise RuntimeError(error_detail) from e
