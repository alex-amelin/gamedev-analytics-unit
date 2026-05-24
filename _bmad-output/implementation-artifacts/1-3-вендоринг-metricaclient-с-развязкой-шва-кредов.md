# Story 1.3: Вендоринг `MetricaClient` с развязкой шва кредов

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want вендоренный клиент Logs API на чистом `requests` с инжекцией кредов,
so that иметь проверенный примитив доступа к Logs API без тяжёлой цепи зависимостей directaiq.

**Контекст эпика.** Третья история Epic 1 «Каркас юнита и канал Logs API». Каркас (1.1 = done) и тонкий env-ридер (1.2 = done, `scripts/utils/env_reader.py` → `read_metrica_credentials() -> MetricaCredentials(token: str, counter_id: int)`) уже стоят. Эта история кладёт **второй примитив доступа** — `scripts/utils/metrica_client.py` — вендорингом проверенного клиента из соседнего репо directaiq с обрезкой лишнего. От него зависят: CLI-примитивы жизненного цикла Logs API (1.6 — связывает env-ридер + клиент), оркестратор p81 (2.7 — водит полный цикл), и косвенно весь приём (Epic 2).

**Это развязка шва вендоринга №1 (вторая половина).** Шов: в directaiq `MetricaClient.__init__` сам зовёт `AuthManager.get_metrica_credentials()` внутри (тянет `tapi_yandex_direct` + `ConfigManager` + Direct-fallback). История 1.2 заменила `AuthManager` тонким ридером; **здесь мы развязываем вызов изнутри клиента** — конструктор больше не добывает креды сам, а **принимает готовые `token`/`counter_id` инъекцией** от вызывающей стороны (CLI 1.6 свяжет одно с другим). Клиент остаётся развязан от env-ридера (не импортирует его) — чистый шов, легко тестируется.

**Главные риски истории.**
1. **Перетащить лишнее.** Источник — 1070 строк с отчётным Stat/Reporting API, целями, Direct-атрибуцией, `upload_offline_conversions` и зависимостью `polars`. Перенести нужно **только** HTTP-плумбинг + методы жизненного цикла Logs API + лёгкие management-info-методы. Всё остальное — вырезать (AC #2, NFR-6 «простота-первой»).
2. **Реимплементировать retry/rate-limit.** Поведение устойчивости к лимитам уже есть в вендоренном коде — его надо **сохранить как есть, не переписывая** (AC #3, NFR-3). Анти-паттерн project-context: «реимплементация retry/rate-limit вне вендоренного клиента».
3. **Засветить токен.** NFR-5: токен живёт только в заголовке сессии, не в атрибутах, логах, `repr`, сообщениях об ошибках.

## Acceptance Criteria

1. **Given** исходный `metrica_client.py` directaiq, **When** он перенесён в `scripts/utils/metrica_client.py`, **Then** конструктор принимает готовые `token`/`counter_id` инъекцией от env-ридера (1.2) и НЕ зовёт `AuthManager` изнутри, **And** файл несёт шапку «vendored from directaiq @ <ref>, seam: creds injected».
2. **Given** вендоренный клиент, **When** смотрим методы, **Then** есть все методы жизненного цикла Logs API + лёгкие info-методы, **And** агрегатный Stat/Reporting API, `upload_offline_conversions` и зависимость `polars` вырезаны (только `requests`).
3. **Given** встроенные rate-limit и retry/backoff на 429/500/502/503, **When** клиент используется, **Then** это поведение сохранено из вендоренного кода и заново не реализуется (NFR-3).
4. **Given** валидные креды тестового счётчика, **When** вызывается info-метод, **Then** возвращается корректный ответ (smoke), **And** в CI живой вызов мокается, ручной прогон документирован.
5. **Given** исчерпаны ретраи на устойчивых 429/5xx ИЛИ ошибка сетевого уровня (timeout/DNS/reset, без HTTP-статуса), **When** вызывается метод, **Then** поднимается явная ошибка (ненулевой исход), без молчаливого зависания или «голого» трейсбека. _[edge-case: терминальный путь ретраев + connection-level]_
6. **Given** не-ретраябельный HTTP (401/403/404, вне набора {429,500,502,503}), **When** метод вызывается, **Then** ошибка сразу классифицируется как auth/доступ/не-найдено с понятным сообщением, без бессмысленных ретраев. _[edge-case: невалидный токен ретраится впустую]_

## Tasks / Subtasks

- [x] **Task 1 — Перенести клиент в `scripts/utils/metrica_client.py` и развязать шов кредов (AC: #1)**
  - [x] Создать `scripts/utils/metrica_client.py`. Источник для копирования: `G:\git\directaiq\scripts\utils\metrica_client.py` (ref `7718bd65`, ветка `master`, 2026-05-22). _См. Dev Notes → «Источник вендоринга и карта переноса»._
  - [x] **Шапка модуля (обязательна, AC #1):** первой строкой docstring-блока — пометка `vendored from directaiq @ 7718bd65, seam: creds injected` (формат из architecture#Structure Patterns). Модульный docstring — **на русском**: роль модуля (клиент Logs API на `requests`, креды инжектятся, reporting/polars обрезаны).
  - [x] **`from __future__ import annotations`** — первой строкой кода (инвариант проекта; в источнике её НЕТ — добавить).
  - [x] **Развязать конструктор (ядро шва):** заменить
    ```python
    def __init__(self, counter_id: str | None = None) -> None:
        token, self.counter_id = AuthManager.get_metrica_credentials()
        if counter_id:
            self.counter_id = counter_id
        ...
    ```
    на инъекцию готовых кредов:
    ```python
    def __init__(self, token: str, counter_id: int) -> None:
        self.counter_id = counter_id
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"OAuth {token}", "Content-Type": "application/json"}
        )
        del token  # токен живёт только в заголовке сессии (NFR-5)
        self.last_request_time = 0.0
        self.requests_count_today = 0
        self.current_date = datetime.now().date()
    ```
    Сигнатура — ровно `(token: str, counter_id: int)`: контракт с 1.2 (`read_metrica_credentials()` отдаёт `MetricaCredentials(token, counter_id)`) и 1.6 (CLI свяжет: `c = read_metrica_credentials(); MetricaClient(token=c.token, counter_id=c.counter_id)`). _[anti-coupling: НЕ импортировать `env_reader` в клиент — креды инжектит вызывающий; шов остаётся чистым и тестируемым]_
  - [x] **`counter_id` — `int`** (1.2 уже привёл и провалидировал). В URL-путях f-строка сериализует `int` корректно (`f"/management/v1/counter/{self.counter_id}"`). НЕ ре-валидировать здесь (валидация — зона 1.2; дублировать нельзя).
  - [x] **Убрать `from .auth_manager import AuthManager`** полностью (AC #1, #2 — нет тяжёлых зависимостей).
- [x] **Task 2 — Обрезать reporting/Stat/Direct/polars; оставить Logs API + info (AC: #2)**
  - [x] **СОХРАНИТЬ (методы жизненного цикла Logs API):** `create_log_request`, `get_log_requests`, `get_log_request`, `download_log_request_part`, `clean_log_request`, `evaluate_log_request`.
  - [x] **СОХРАНИТЬ (лёгкие management info-методы — architecture#API Patterns):** `get_counter_info`, `get_counters`, `get_goals`. Все три ходят в `/management/v1/...` через `make_request`, polars не трогают.
  - [x] **СОХРАНИТЬ (HTTP-плумбинг, без изменений):** `_rate_limit`, `_check_response_errors`, `make_request`, `_handle_request_error` и классовые константы (`BASE_URL`, `MAX_REQUESTS_PER_SECOND`, `MAX_REQUESTS_PER_DAY`, `MAX_ROWS_PER_REQUEST`, `_RETRYABLE_STATUS_CODES`, `_MAX_RETRIES`, `_RETRY_DELAYS`). _Не переписывать (AC #3) — см. Dev Notes → «Retry/rate-limit»._
  - [x] **ВЫРЕЗАТЬ (Stat/Reporting API + polars):** `get_search_queries_data`, `collect_all_search_queries`, `get_report`, `get_report_paginated`, `get_raw_report`, `get_goals_detailed`, `get_goals_stats`, статический хелпер `_detect_table_prefix`.
  - [x] **ВЫРЕЗАТЬ (Direct-специфика):** `get_direct_clients`; модульные константы `DIRECT_TO_METRICA_ATTRIBUTION`, `ATTRIBUTABLE_DIMENSIONS` (атрибуция отчётного API).
  - [x] **ВЫРЕЗАТЬ (явно названо в AC #2):** `upload_offline_conversions`.
  - [x] **Убрать `import polars as pl`** (строка 12 источника). После обрезки методов прогнать поиск `polars`/`pl\.` по файлу — должно быть **пусто** (иначе остался непорезанный метод). _[edge-case: остаточная ссылка на polars завалит импорт — polars не в зависимостях]_
  - [x] **Заменить логгер на stdlib:** в источнике `from .logging_utils import get_logger; logger = get_logger(__name__)` → у нас `import logging; logger = logging.getLogger(__name__)` (как в `env_reader.py`; project-context «только stdlib logging»). НЕ вендорить `logging_utils.py` — не нужен, anti-scope. _См. Dev Notes → «logging_utils»._
  - [x] **`__all__`** свести к `["MetricaClient"]` (убрать `ATTRIBUTABLE_DIMENSIONS`/`DIRECT_TO_METRICA_ATTRIBUTION`).
  - [x] **Импорты после обрезки:** `import logging`, `import time`, `from datetime import datetime`, `from typing import Any, NoReturn`, `import requests`. (`datetime` нужен `_rate_limit`; `NoReturn` — `_handle_request_error`.)
  - [x] **Method-docstring'и оставленных методов — НЕ переписывать** (английские, как в источнике) для сравнимости с оригиналом (project-context «вендоренное не причёсывать вразнобой»). Русский — только модульный docstring + шапка. _См. Dev Notes → «Язык docstring'ов в вендоренном коде»._
- [x] **Task 3 — Подтвердить сохранность retry/rate-limit и путей ошибок (AC: #3, #5, #6)** — _реализации нового кода нет; задача — убедиться, что вендоренное поведение не сломано переносом, и покрыть тестами_
  - [x] Убедиться, что `make_request` сохранил: цикл `range(_MAX_RETRIES)` с `_rate_limit()` на каждой попытке; retry с `time.sleep(_RETRY_DELAYS[attempt])` на статусах `{429,500,502,503}` пока `attempt < _MAX_RETRIES-1`; иначе/при не-ретраябельном статусе/при ошибке без `response` — немедленный `raise RuntimeError(error_detail)`.
  - [x] **Зафиксировать (не баг, для тестов AC #5/#6) — асимметрия ретраев в источнике:** GET-методы (`get_*`, `evaluate_log_request`) идут через `make_request` → **ретраятся**. POST/download (`create_log_request`, `clean_log_request`, `download_log_request_part`) вызывают `session.post/get` напрямую и обрабатывают ошибку через `_handle_request_error` → **НЕ ретраятся**, сразу `RuntimeError`. Это вендоренное поведение — сохранить как есть (AC #3 «заново не реализуется»), не «выравнивать». _См. Dev Notes → «Retry/rate-limit»._
  - [x] **AC #6 (не-ретраябельный HTTP без бессмысленных ретраев):** убедиться, что 401/403/404 (вне `_RETRYABLE_STATUS_CODES`) в `make_request` проваливаются мимо ветки retry сразу в `raise RuntimeError(error_detail)`; `error_detail` уже включает `str(e)` (несёт HTTP-статус, напр. «404 Client Error») + API-`message`/`errors` из тела. Этого достаточно для «классифицируется … с понятным сообщением». **Не добавлять** тяжёлой логики классификации — минимальное вендоренное поведение удовлетворяет AC (главное — «без бессмысленных ретраев»). _См. Dev Notes → «AC #6 — что именно требуется»._
  - [x] **AC #5 (терминальный путь + connection-level):** убедиться, что (а) исчерпание ретраев на устойчивых 429/5xx даёт `RuntimeError` (не «голый» трейсбек, не зависание); (б) ошибка сетевого уровня (`requests.exceptions.ConnectionError`/`Timeout`, у которой `response is None` → `status_code is None` → не в retryable-наборе) даёт немедленный `RuntimeError`.
- [x] **Task 4 — Offline-тесты `tests/test_metrica_client.py` (AC: #1, #2, #3, #5, #6)** — _см. Dev Notes → «Тестирование»_
  - [x] `from __future__ import annotations`; без сети. Мокать `requests`-сессию через `unittest.mock` (stdlib): сконструировать клиент с фиктивными `token="t"`, `counter_id=42`, затем подменить `client.session` на `MagicMock`, настраивая `session.get/post.return_value` (mock-`Response` с `.json()`, `.raise_for_status()`, `.content`, `.status_code`) или `.side_effect` (список для retry).
  - [x] **КРИТИЧНО — заглушить `time.sleep`:** `monkeypatch.setattr("scripts.utils.metrica_client.time.sleep", lambda *a, **k: None)` (autouse-fixture). Иначе retry-тесты реально спят 30/60с, а `_rate_limit` — до 1/30с. Без этого набор повиснет/будет медленным. _[ловушка: тесты ретраев «зависают»]_
  - [x] Кейсы (минимум):
    - **Конструктор-шов (AC #1):** заголовок сессии = `Authorization: OAuth t`; `counter_id == 42`; токен НЕ хранится атрибутом (`"t" not in repr(client.__dict__)` / нет поля `token`). _NFR-5._
    - **Анти-зависимости (AC #1, #2) — НЕ голой подстрокой** (модульный docstring/комментарии содержат `AuthManager`/`polars` → ложный красный): распарсить `ast` модуля и проверить, что в `Import`/`ImportFrom`-узлах нет `auth_manager`, `polars`, `tapi_yandex*`, `logging_utils`, `config_manager`. (Аналог приёма из `tests/test_env_reader.py`.)
    - **Вырезанные методы (AC #2):** `not hasattr(MetricaClient, "get_report")`, `..."upload_offline_conversions"`, `..."get_search_queries_data")` и т.п.; оставленные — `hasattr` True для всех шести Logs API + трёх info.
    - **Logs API happy-path (по одному на метод):** `create_log_request` (POST, проверить URL `/logrequests` + params: `date1/date2/source`, `fields` склеены через `","`); `get_log_request`/`get_log_requests` (status/list); `evaluate_log_request`; `clean_log_request`; `get_counter_info`; `download_log_request_part` → возвращает `bytes` из мини-TSV фикстуры.
    - **Retry на 503→200 (AC #3):** `session.get.side_effect = [<503 HTTPError>, <200 ok>]` → метод возвращает данные; `time.sleep` вызван 1 раз с `30`.
    - **Терминальный retry (AC #5):** три подряд 503 → `RuntimeError`; `time.sleep` вызван дважды (30, 60). _(Замечание: `_RETRY_DELAYS[2]=120` в источнике не используется при `_MAX_RETRIES=3` — вендоренная особенность, не «чинить».)_
    - **Connection-level (AC #5):** `session.get.side_effect = requests.exceptions.ConnectionError(...)` (без `response`) → немедленный `RuntimeError`, `session.get` вызван 1 раз (без ретраев).
    - **Не-ретраябельный HTTP (AC #6):** 401 и 404 (HTTPError с `response.status_code`) → `RuntimeError` сразу, `session.get` вызван **1 раз** (нет бессмысленных ретраев), статус присутствует в тексте ошибки.
    - **Дневной лимит (AC #3):** выставить `client.requests_count_today = MAX_REQUESTS_PER_DAY` → `_rate_limit`/`make_request` → `RuntimeError` про дневной лимит.
    - **Ошибка в теле ответа:** `_check_response_errors` на `{"errors": [{"text": "..."}]}` → `RuntimeError("Metrica API error: ...")`.
  - [x] Завести `tests/fixtures/` (каталога ещё нет): мини-TSV `tests/fixtures/logs_visits_sample.tsv` — строка заголовка + 1–2 строки данных (для download-теста; project-context требует фикстуры на мини-TSV).
- [x] **Task 5 — Live-smoke `@pytest.mark.live` (AC: #4; project-context «тесты внешнего API → обязателен live-smoke»)**
  - [x] Маркер `@pytest.mark.live`; по умолчанию выключен (`addopts = "-m 'not live'"` — завести в `pyproject.toml`, если ещё нет; см. Dev Notes → «Маркер live»). Запуск явно: `uv run pytest -m live`.
  - [x] Тест дёргает **реальный** Logs API дешёвым info-методом: `creds = read_metrica_credentials()` (из 1.2) → `MetricaClient(token=creds.token, counter_id=creds.counter_id)` → `get_counter_info()` → assert вернулся `dict` с данными счётчика. **Один запрос** — уважает rate-limit (≤5000/day).
  - [x] **Нет кредов → `pytest.skip` с понятной причиной** (ловить `ValueError` от ридера ИЛИ проверять env заранее) — не ложный красный в CI/без `.env`.
  - [x] Документировать ручной прогон в Dev Notes/Completion Notes: `uv run pytest -m live` (нужны креды в `.env` хранилища). _AC #4: «в CI живой вызов мокается» (Task 4), «ручной прогон документирован» (тут)._
- [x] **Task 6 — mypy strict под `requests` (AC: качество/CI)** — _см. Dev Notes → «mypy и requests»_
  - [x] Прогнать `uv run mypy scripts`. `requests` НЕ несёт `py.typed` → под `strict` ожидается `import-untyped`.
  - [x] **Рекомендуемый фикс — добавить стабы:** `uv add --dev types-requests` (точные типы, без `Any`-дыр; обновлённый `uv.lock` — в тот же коммит, инвариант). Альтернатива (если стабы дают трения): override в `pyproject.toml`:
    ```toml
    [[tool.mypy.overrides]]
    module = ["requests", "requests.*"]
    ignore_missing_imports = true
    ```
    Сначала прогнать mypy и убедиться, что проблема реально есть; выбрать один путь. Все методы остаются полностью аннотированы (типы — из источника; `Any` только на границе `response.json()`, как в оригинале).
- [x] **Task 7 — Спека компонента `docs/metrica-client.md` (DoD, project-context)**
  - [x] Завести `docs/metrica-client.md` человеческим языком: **что делает** (создаёт/опрашивает/скачивает/чистит лог-запросы Logs API, плюс справочные info-методы), **зачем нужен** (единственная точка HTTP к Logs API; устойчивость к лимитам), **контракт с другими** (принимает готовые токен+счётчик инъекцией от компонента кредов 1.2; даёт жизненный цикл лог-запроса оркестратору 2.7 и CLI 1.6; rate-limit/retry внутри). Сослаться на `docs/creds.md` (откуда приходят креды).
  - [x] **Осознанное решение о гранулярности (как в 1.2 с `creds.md`):** карта компонентов project-context относит `metrica_client` к `ingestion.md` (приём = client + p81 + parquet_store + load_state). Но p81/parquet_store/load_state — это Epic 2; клиент — самостоятельный транспортный примитив, потребляемый оркестратором. По прецеденту `creds.md` (выделен Шефом как сквозной примитив) завожу **отдельную** `docs/metrica-client.md`. _Подтвердить у Шефа (вынесено в финальные вопросы); если предпочтёт `ingestion.md` — переименовать тривиально._
- [x] **Task 8 — Гейты верификации (обязательны перед закрытием)**
  - [x] `uv run mypy scripts` → зелёно (strict; модуль полностью типизирован).
  - [x] `uv run pytest` → зелёно (новые offline-тесты + смоук 1.1 + тесты 1.2; live по умолчанию пропущен через `-m 'not live'`).
  - [x] Прогнать чек-лист «Definition of Done» из Dev Notes.

### Review Findings

_Code review 2026-05-24 (adversarial: Blind Hunter + Edge Case Hunter + Acceptance Auditor). Итог: 1 decision-needed, 2 patch, 6 defer, 8 dismissed. Ни один AC материально не нарушен; `requests 2.34.2` несёт py.typed (mypy strict зелёный) — подтверждено аудитором._

**Decision (разрешено 2026-05-24 → вариант 3, конвертировано в patch):**

- [x] [Review][Patch] (из decision) Поднял `_MAX_RETRIES`→4: `_RETRY_DELAYS[2]=120` теперь реально срабатывает, docstring `make_request` «exponential backoff (30s, 60s, 120s)» соответствует поведению (1 первичная + 3 ретрая 30/60/120). `test_retry_exhausted_raises` обновлён (`[call(30), call(60), call(120)]`, `call_count == 4`). Stale-ссылка `_make_request`→`make_request` в docstring `_handle_request_error` исправлена. **Сознательное отклонение от вендора по решению Шефа — затрагивает NFR-3** (3 ретрая вместо 2; добавлен поясняющий комментарий у константы). [scripts/utils/metrica_client.py:101-108, 373; tests/test_metrica_client.py:287-300] _(найдено всеми тремя слоями)_

**Patch:**

- [x] [Review][Patch] Параметризовал retry-тест всеми ретраябельными кодами {429,500,502,503} (был только 503): `test_retry_retryable_then_success` — AC #3 [tests/test_metrica_client.py:272]
- [x] [Review][Patch] Добавил 403 в `parametrize` теста `test_non_retryable_http_fails_once` (`[401, 403, 404]`) — docstring его заявлял [tests/test_metrica_client.py:311]

**Defer (наследие вендора / усиление тестов — не блокирует):**

- [x] [Review][Defer] `_check_response_errors` падает «голым» трейсбеком на нештатной форме `errors` (не-список либо элементы не-dict) вместо чистого RuntimeError [scripts/utils/metrica_client.py:97-99] — deferred, наследие вендора
- [x] [Review][Defer] POST/download-путь (`create_log_request`/`clean_log_request`) не ловит `ValueError` от `response.json()` при 2xx с не-JSON телом → непойманный JSONDecodeError (GET-путь `make_request` его ловит — асимметрия); POST-путь ошибок (`_handle_request_error`) не покрыт тестом [scripts/utils/metrica_client.py:262, 336] — deferred, наследие вендора
- [x] [Review][Defer] `get_log_requests` вернёт `None` (нарушив тип `list[dict]`), если ключ `requests` присутствует со значением `null` [scripts/utils/metrica_client.py:277] — deferred, наследие вендора
- [x] [Review][Defer] `get_counters` падает (`None.get`) на `counter["site2"]=null` и молча отдаёт `[]` с INFO-логом при переименовании ключей API; live-тестом не покрыт (live дёргает только `get_counter_info`) [scripts/utils/metrica_client.py:204-217] — deferred, наследие вендора
- [x] [Review][Defer] Расход дневной квоты при ретраях не покрыт тестом (`_rate_limit` замокан в retry-тестах): каждый ретрай инкрементит `requests_count_today`, шторм ретраев способен упереться в дневной лимит «посреди» цикла и подменить транзиентную 503 на quota-ошибку [tests/test_metrica_client.py:274] — deferred, усиление тестов
- [x] [Review][Defer] `test_token_not_stored_in_attributes`: assert по `repr(c.__dict__)` тавтологичен для поля `session` (`Session.__repr__` не дампит заголовки) — реально проверяется лишь `not hasattr(c,"token")`; NFR-5 недо-покрыт (утечка через лог/кастомный `__repr__` не ловится) [tests/test_metrica_client.py:102-107] — deferred, усиление тестов

## Dev Notes

### Источник вендоринга и карта переноса

Источник: `G:\git\directaiq\scripts\utils\metrica_client.py`, ref **`7718bd65`** (ветка `master`, 2026-05-22). Шапка модуля обязана его называть: `vendored from directaiq @ 7718bd65, seam: creds injected`. _(NB: память [[directaiq-vendor-source]] — directaiq лежит на `G:\`, не `D:\`; ссылки `D:/git/...` из старых артефактов устарели.)_

**Карта (строки — по источнику):**

| Что | Строки источника | Действие |
|---|---|---|
| Модульный docstring (англ.) | 1–6 | Заменить русским + шапка вендоринга |
| `import polars as pl` | 12 | **Вырезать** |
| `from .auth_manager import AuthManager` | 15 | **Вырезать** |
| `from .logging_utils import get_logger` | 16 | Заменить на `import logging` + `logging.getLogger(__name__)` |
| `__all__` (3 имени) | 18–22 | Свести к `["MetricaClient"]` |
| `DIRECT_TO_METRICA_ATTRIBUTION`, `ATTRIBUTABLE_DIMENSIONS` | 24–121 | **Вырезать** (Direct/reporting-атрибуция) |
| класс + константы | 126–132 | Сохранить |
| `__init__` | 134–152 | **Развязать шов** (см. Task 1) |
| `_rate_limit`, `_check_response_errors` | 154–189 | Сохранить как есть |
| retry-константы + `make_request` | 191–257 | Сохранить как есть (AC #3) |
| `get_search_queries_data`, `collect_all_search_queries` | 259–395 | **Вырезать** (Stat API) |
| `get_counter_info` | 397–404 | **Сохранить** (info) |
| `get_goals` | 406–417 | **Сохранить** (info, management) |
| `get_direct_clients` | 419–440 | **Вырезать** (Direct) |
| `get_counters` | 442–471 | **Сохранить** (info, management) |
| `_detect_table_prefix`, `get_report*`, `get_goals_detailed`, `get_goals_stats` | 473–859 | **Вырезать** (Reporting/Stat + polars) |
| Logs API: `create/get_log_requests/get_log_request/download_part/clean/evaluate` | 861–1009 | **Сохранить** (ядро истории) |
| `upload_offline_conversions` | 1011–1055 | **Вырезать** (AC #2 явно) |
| `_handle_request_error` | 1057–1070 | Сохранить как есть |

После обрезки: `grep` по `polars`/`pl.`/`AuthManager`/`/stat/v1/data` в новом файле — пусто (кроме, возможно, упоминаний в комментариях, которых быть не должно после чистки).

### Развязка шва — что именно меняем (контракт с 1.2 и 1.6)

directaiq (`metrica_client.py:134-147`): конструктор сам зовёт `AuthManager.get_metrica_credentials()` и опционально принимает строковый `counter_id`-override. Мы:
- **Убираем вызов изнутри** (шов №1) — конструктор `(token: str, counter_id: int)` принимает готовое.
- **НЕ импортируем `env_reader`** в клиент — креды инжектит вызывающая сторона (1.6 CLI). Клиент остаётся развязан и легко мокается (тест передаёт фиктивный токен).
- `counter_id` теперь `int` (1.2 валидировал; в directaiq был строкой и шёл в URL-пути f-строкой — `int` сериализуется так же).
- **Сохраняем по духу:** `Authorization: OAuth {token}` заголовок, `del token` после установки (токен не оседает в атрибутах — NFR-5), классовые лимиты/таймауты.

Контракт-цепочка (для 1.6, не реализовывать здесь): `creds = read_metrica_credentials()` → `MetricaClient(token=creds.token, counter_id=creds.counter_id)`. CLY ловит `ValueError` ридера (1.2) до конструктора; клиент креды не валидирует.

### Retry/rate-limit — сохранить, не переписывать (AC #3, NFR-3)

Вендоренная устойчивость уже реализована; project-context прямо запрещает реимплементацию. Конкретика, которую важно НЕ сломать переносом:

- **`_rate_limit`:** ≤30 req/s (sleep до `1/30`с между запросами) + дневной счётчик ≤5000 (`RuntimeError` при превышении, сброс на смене даты). Вызывается на каждой попытке внутри `make_request` и в POST/download-методах.
- **`make_request` (GET-путь):** до `_MAX_RETRIES=3` попыток; backoff `_RETRY_DELAYS=[30,60,120]` на `{429,500,502,503}`; не-ретраябельный статус/ошибка без `response`/исчерпание → `RuntimeError(error_detail)` с API-`message`/`errors` из тела.
- **Асимметрия (зафиксировать, не выравнивать):** `create_log_request`/`clean_log_request`/`download_log_request_part` идут **мимо** `make_request` (прямой `session.post/get` + `_handle_request_error`) → **без ретраев**. Это вендоренное поведение directaiq. Оркестратор p81 (2.7) знает про асимметрию и сам решает про повтор цикла; здесь — сохранить как есть.

**AC #6 — что именно требуется.** Главное — «без бессмысленных ретраев»: 401/403/404 не входят в `_RETRYABLE_STATUS_CODES`, поэтому `make_request` сразу падает в `raise RuntimeError(error_detail)` (одна попытка), а POST/download и так не ретраятся. `error_detail` несёт `str(e)` (содержит HTTP-статус, напр. «401 Client Error: Unauthorized») + API-сообщение → «понятное сообщение с классификацией» удовлетворено вендоренным текстом. **Не вводить** отдельный маппинг статус→категория и кастомные классы исключений — это усложнение против NFR-6; минимального поведения достаточно (подтверждается тестом «1 вызов, статус в тексте»).

**AC #5.** Терминальный путь ретраев и connection-level (timeout/DNS/reset, `response is None`) оба ведут в `raise RuntimeError(...)` — явная ошибка, не зависание и не «голый» трейсбек. Покрыть тестами обе ветки.

### logging_utils — не вендорить, использовать stdlib

Источник тянет `from .logging_utils import get_logger`. Архитектура числит `logging_utils.py` как вендоренный примитив «как есть», но в нашем репо его пока нет, и он не нужен: `env_reader.py` (1.2) задал прецедент — `logger = logging.getLogger(__name__)`. Делаем так же (project-context: «только stdlib logging»). Заведение `logging_utils.py` — anti-scope для 1.3 (нет потребности). Уровни логов в оставленных методах (`debug`/`info`/`warning`) — оставить как в источнике; **токен не логируется** (он только в заголовке сессии; `make_request` логирует `params`, где токена нет).

### Язык docstring'ов в вендоренном коде

project-context требует docstring'и на русском, но также «вендоренное не причёсывать вразнобой, чтобы оставалось сравнимым с источником». Разрешение конфликта: **модульный docstring — русский** (роль модуля + что обрезано) + **шапка вендоринга**; **docstring'и оставленных методов — НЕ переписывать** (английские, как в источнике) ради diff-сравнимости. Это осознанный вендоренный компромисс — отметить при ревью, не считать пробелом.

### Тестирование

- **Offline-набор (обязателен, CI):** `tests/test_metrica_client.py`. HTTP мокается через `unittest.mock` (stdlib) — без сети, без реального счётчика. **Не добавлять** `responses`/`requests-mock` в зависимости (простота-первой): достаточно подмены `client.session` на `MagicMock`.
- **Заглушить `time.sleep`** (autouse-fixture, патч `scripts.utils.metrica_client.time.sleep`) — иначе retry-тесты спят десятки секунд. Это самая частая ловушка при тестировании этого клиента.
- **Анти-зависимости — через `ast`, не подстроку** (docstring/комментарии содержат вырезанные имена → ложный красный): тот же приём, что в `tests/test_env_reader.py::test_*` (разбор import-узлов). Проверить отсутствие `polars`, `auth_manager`, `tapi_yandex*`, `logging_utils`, `config_manager` в импортах.
- **Фикстуры:** `tests/fixtures/logs_visits_sample.tsv` (мини-TSV) для download-теста — заводится впервые.
- **Live-smoke (обязателен по project-context, opt-in):** `@pytest.mark.live`, реальный `get_counter_info` через креды 1.2; нет кредов → `pytest.skip`. Освежать offline-фикстуры из реального ответа — на будущее (для 1.3 download-фикстура синтетическая, ок).
- **Маркер live в pyproject:** проверить, есть ли `[tool.pytest.ini_options]` с `markers`/`addopts`. По deferred-work 1.1 опорной pytest-конфигурации пока НЕТ. Завести минимально:
  ```toml
  [tool.pytest.ini_options]
  markers = ["live: smoke против реального Logs API (нужны креды; не в CI)"]
  addopts = "-m 'not live'"
  ```
  (По умолчанию live выключен; CI его не гоняет.) _[без `addopts` маркер `live` под `--strict-markers` даст предупреждение/ошибку]_

### mypy и requests

`requests` не поставляет `py.typed` → под `mypy --strict` ожидается `import-untyped`. Рекомендуемо: `uv add --dev types-requests` (точные стабы; `uv.lock` обновляется и коммитится в тот же коммит — инвариант «лок авторитетен»). Альтернатива — `[[tool.mypy.overrides]] ignore_missing_imports` для `requests` (паттерн заложен в 1.1 Dev Notes для untyped-импортов). Сначала прогнать mypy и подтвердить, что ошибка есть. `from typing import Any, NoReturn` — `Any` только на границе `response.json()` (аннотировано в источнике), `NoReturn` у `_handle_request_error`.

### Project Structure Notes

- Модуль кладётся в `scripts/utils/metrica_client.py` — ровно по карте соответствия архитектуры (`metrica_client.py` → `metrica_client.py`, вендорим). Каталог `scripts/utils/` уже регулярный пакет (`__init__.py` из 1.1) → `from scripts.utils.metrica_client import MetricaClient` резолвится.
- Импорты абсолютные от корня пакета. Имена snake_case (модуль/функции), класс `MetricaClient` CapWords. Type hints обязательны.
- `tests/` зеркалит `scripts/`: `tests/test_metrica_client.py`. Новый каталог `tests/fixtures/`.
- Спека — `docs/metrica-client.md` (см. Task 7; решение о гранулярности вынесено Шефу).
- Конфликтов со структурой нет. Не реорганизовывать раскладку, не переводить на src-layout (ломает hatchling-резолюцию импортов).

### Definition of Done — чек-лист самопроверки

1. `scripts/utils/metrica_client.py` создан; шапка `vendored from directaiq @ 7718bd65, seam: creds injected`; `from __future__ import annotations` первой строкой; модульный docstring русский. (AC #1)
2. Конструктор `(token: str, counter_id: int)` инъекцией; `AuthManager` не вызывается и не импортируется; `env_reader` не импортируется. (AC #1)
3. Есть все 6 методов жизненного цикла Logs API + 3 info-метода (`get_counter_info`/`get_counters`/`get_goals`). (AC #2)
4. Вырезаны: Stat/Reporting (`get_report*`, `get_search_queries*`, `get_goals_detailed/_stats`), Direct (`get_direct_clients`, атрибуция-константы), `upload_offline_conversions`, `polars`, `auth_manager`, `logging_utils`. Поиск `polars`/`pl.` — пусто. (AC #2)
5. Retry/rate-limit (`_rate_limit`/`make_request`/константы) сохранены из источника, не переписаны; асимметрия GET-retry vs POST/download-no-retry зафиксирована. (AC #3)
6. Токен не оседает в атрибутах/логах/`repr`/сообщениях (только заголовок сессии, `del token`). (NFR-5)
7. Offline-тесты покрывают: шов-конструктор, анти-зависимости (ast), наличие/отсутствие методов, happy-path 6 Logs API + info, retry 503→200, терминальный retry (AC #5), connection-level (AC #5), не-ретраябельный 401/404 «1 вызов» (AC #6), дневной лимит, ошибка в теле. `time.sleep` заглушён.
8. Live-smoke `@pytest.mark.live` (`get_counter_info`), skip без кредов; `addopts="-m 'not live'"` в pyproject. (AC #4)
9. `docs/metrica-client.md` заведён. (DoD project-context)
10. `uv run mypy scripts` и `uv run pytest` — зелёные; `uv.lock` обновлён и закоммичен, если добавлен `types-requests`.
11. Велась в отдельной ветке `story/1.3-metrica-client` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС.

### Latest Tech Information

- `requests >=2.31` (в `uv.lock`); API `Session.get/post`, `raise_for_status`, `response.json()` стабилен. Стабы — пакет `types-requests` (dev-only).
- `python-dotenv`/прочее — не касается этой истории.
- Web-ресёрч не требуется: вендорим зафиксированный проверенный код, версии — в локе.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 1.3] — user story + 6 AC (усилены edge-case hunter).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-1] — оркестрация полного цикла Logs API; rate-limit/retry наследуется от вендоренного клиента.
- [Source: _bmad-output/planning-artifacts/architecture.md#API & Communication Patterns] — что вендорим/вырезаем: HTTP-плумбинг + все методы Logs API + info (`get_counter_info`/`get_counters`/`get_goals`); вырезаем Stat/Reporting (+polars), `upload_offline_conversions`, Direct. (строки 225–228)
- [Source: _bmad-output/planning-artifacts/architecture.md#Швы вендоринга] — шов №1: `MetricaClient.__init__` зовёт `AuthManager` → инжектировать готовые креды. (строка 80)
- [Source: _bmad-output/planning-artifacts/architecture.md#Technology Stack] — `polars` в `metrica_client.py` нужен только reporting-методам, которые не переносим → клиент чисто на `requests`. (строки 161–163)
- [Source: _bmad-output/planning-artifacts/architecture.md#Structure Patterns] — вендоренный код в выделенном модуле с шапкой «vendored from directaiq @ <ref>, seam: creds injected». (строки 333–334)
- [Source: _bmad-output/planning-artifacts/architecture.md#директ aiq → наш проект (карта)] — `metrica_client.py` → `metrica_client.py` (вендорим, requests-only, шов развязан); `logging_utils.py` — как есть. (строки 420–456)
- [Source: _bmad-output/project-context.md#Logs API и креды] — единственная точка HTTP к Logs API; retry/rate-limit только из вендоренного клиента; цикл create→poll→download→clean.
- [Source: _bmad-output/project-context.md#Critical Don't-Miss Rules] — не реимплементировать retry/rate-limit вне вендоренного клиента; не тащить тяжёлые зависимости; вендоренное не причёсывать вразнобой.
- [Source: _bmad-output/project-context.md#Testing Rules] — offline-моки на мини-TSV из `tests/fixtures/` + обязательный opt-in live-smoke (`@pytest.mark.live`, skip без кредов).
- [Source: _bmad-output/implementation-artifacts/1-2-env-ридер-кредов-метрики.md#Куда инжектятся креды (контракт с 1.3)] — `creds = read_metrica_credentials()` → `MetricaClient(token=creds.token, counter_id=creds.counter_id)`; вызов `AuthManager` изнутри убирается в 1.3.
- [Source: G:/git/directaiq/scripts/utils/metrica_client.py] — оригинал (ref 7718bd65): `__init__` 134–152, `_rate_limit` 154–178, `make_request`+retry 191–257, Logs API 861–1009, `_handle_request_error` 1057–1070, info 397–471; вырезаемое — reporting/Stat 259–859, `upload_offline_conversions` 1011–1055, polars/AuthManager/атрибуция-константы.
- [Source: docs/creds.md] — компонент кредов (1.2), откуда клиент получает токен/счётчик; спека 1.3 на него ссылается.
- [Memory: directaiq-vendor-source] — источник вендоринга `G:\git\directaiq` (не `D:\`).
- [Memory: feedback-decide-and-apply] — решения о гранулярности доков/выборе стабов принимаю сам и применяю до конца; точки с реальной развилкой выношу Шефу.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Claude Opus 4.7, 1M context) — workflow `bmad-dev-story`.

### Debug Log References

- `uv run pytest tests/test_metrica_client.py -q` → 37 passed.
- `uv run pytest -q` → 58 passed, 1 deselected (live отсеян через `addopts="-m 'not live'"`).
- `uv run pytest -m live --collect-only -q` → 1 collected, 58 deselected (маркер `live` подключён; реальный прогон не запускался — внешний API + креды).
- `uv run mypy scripts` → Success, 9 source files.

### Completion Notes List

- **Шов кредов развязан (AC #1):** конструктор `MetricaClient(token: str, counter_id: int)` принимает готовые креды инъекцией; `AuthManager` не вызывается и не импортируется; `env_reader` НЕ импортируется (креды инжектит вызывающая сторона — CLI 1.6). `del token` после установки заголовка — токен живёт только в сессии (NFR-5). Шапка `vendored from directaiq @ 7718bd65, seam: creds injected` в модульном docstring; `from __future__ import annotations` первой строкой кода.
- **Обрезка (AC #2):** вырезаны Stat/Reporting (`get_report*`, `get_search_queries*`, `get_goals_detailed/_stats`, `_detect_table_prefix`), Direct (`get_direct_clients`, константы атрибуции), `upload_offline_conversions`, импорты `polars`/`auth_manager`/`logging_utils`. Логгер заменён на stdlib `logging.getLogger(__name__)`. `__all__ = ["MetricaClient"]`. Оставлены 6 методов жизненного цикла Logs API + 3 info-метода (`get_counter_info`/`get_counters`/`get_goals`). Поиск `polars`/`pl.` в коде — пусто (упоминания только в модульном docstring, тесты проверяют импорты через `ast`).
- **Retry/rate-limit сохранены как есть (AC #3, #5, #6):** `_rate_limit`/`make_request`/`_handle_request_error` + константы перенесены без изменений. Асимметрия зафиксирована и покрыта тестами: GET-методы ретраятся через `make_request`; POST/download (`create`/`clean`/`download`) идут напрямую и НЕ ретраятся. Method-docstring'и оставленных методов не переписывались (английские, как в источнике) ради diff-сравнимости — осознанный вендоренный компромисс.
- **mypy — решение (Task 6):** `requests` 2.34.2 несёт `py.typed` (нативно типизирован) → `import-untyped` не возникает, `mypy --strict` зелёный без стабов. `types-requests` НЕ добавлен (project-context: «зависимости без необходимости — нет»); `uv.lock` не менялся. Прецедент решения — [[feedback-decide-and-apply]].
- **Спека (Task 7) — решение о гранулярности:** заведена ОТДЕЛЬНАЯ `docs/metrica-client.md` (а не раздел в `ingestion.md`) по прецеденту `creds.md`: клиент — самостоятельный транспортный примитив, потребляемый оркестратором Эпика 2. Решение принято автономно ([[feedback-decide-and-apply]]); вынесено на финальное подтверждение Шефу (тривиально переименовать, если предпочтёт `ingestion.md`).
- **Live-smoke (AC #4):** `tests/test_metrica_client_live.py` (`@pytest.mark.live`) дёргает реальный `get_counter_info` через креды 1.2; нет кредов → `pytest.skip`. Ручной прогон: `uv run pytest -m live` (нужны креды в `.env` хранилища). В CI живой вызов мокается (offline-набор), live отсеян по умолчанию.

### File List

- `scripts/utils/metrica_client.py` (новый) — вендоренный клиент Logs API с развязанным швом кредов.
- `tests/test_metrica_client.py` (новый) — offline-тесты (37 кейсов).
- `tests/test_metrica_client_live.py` (новый) — live-smoke `@pytest.mark.live`.
- `tests/fixtures/logs_visits_sample.tsv` (новый) — мини-TSV фикстура для download-теста.
- `docs/metrica-client.md` (новый) — человекочитаемая спека компонента.
- `pyproject.toml` (изменён) — добавлен `[tool.pytest.ini_options]`: маркер `live` + `addopts="-m 'not live'"`.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (изменён) — статус 1-3 → in-progress → review.

## Change Log

| Дата | Изменение |
|---|---|
| 2026-05-24 | Создана story 1.3 (context engine): вендоринг `MetricaClient` из directaiq @ 7718bd65 с развязкой шва кредов (конструктор инъекцией), обрезкой reporting/Stat/Direct/polars/`upload_offline_conversions`, сохранением retry/rate-limit; offline-моки + live-smoke; спека `docs/metrica-client.md`. Статус → ready-for-dev. |
| 2026-05-24 | Реализована story 1.3: создан `scripts/utils/metrica_client.py` (шов развязан, обрезка выполнена, retry/rate-limit сохранены); offline-тесты (37) + live-smoke; `docs/metrica-client.md`; pytest-конфиг (маркер `live`). mypy strict + pytest зелёные; `types-requests` не понадобился (requests несёт py.typed). Статус → review. |
