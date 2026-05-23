---
title: "Addendum (PRD): Game Dev Analytics Unit"
status: final
created: 2026-05-23
updated: 2026-05-23
relates_to: prd.md
---

# Addendum — техническая глубина для архитектуры

Не часть PRD-капабилити. Это вход для `bmad-create-architecture`: зафиксированные **реальные примитивы `directaiq`** (свежо извлечены из кода 2026-05-23) и **наши осознанные отличия**. Дополняет, не заменяет аддендум брифа (`briefs/brief-gamedev-analytics-unit-2026-05-23/addendum.md`), где зафиксированы решения «как сейчас → наше решение» по 11 пунктам.

> Все пути относительны `G:\git\directaiq` (на этой машине через Bash: `/g/git/directaiq`). Номера строк — на момент 2026-05-23, могут сдвинуться.

---

## A. Вендоримый клиент Logs API

`scripts/utils/metrica_client.py` — класс `MetricaClient` (чистый `requests.Session`, строки 126–1071). Публичные методы жизненного цикла (строки 862–1010):

- `create_log_request(date1, date2, fields: list[str], source="visits", attribution="CROSS_DEVICE_LAST_SIGNIFICANT") -> dict` — POST, возвращает `{"log_request": {request_id, status, ...}}`.
- `get_log_request(request_id) -> dict` — GET статус: `status ∈ {processed, canceled, processing_failed}`, `parts: [{part_number, size}]`.
- `get_log_requests() -> list` — список всех запросов.
- `download_log_request_part(request_id, part_number) -> bytes` — **возвращает сырые bytes (TSV), на диск НЕ пишет** (это делает caller).
- `clean_log_request(request_id) -> dict` — POST, освобождает квоту.
- `evaluate_log_request(date1, date2, fields, source) -> dict` — dry-run: `{possible, max_possible_day_quantity}`.

Встроено: rate-limit (`_rate_limit`, строки 154–178; 30 req/s, 5000 req/day), retry 3× с backoff [30,60,120] на 429/500/502/503 (строки 191–257), `_check_response_errors` (180–189).

**Наше решение:** вендорить клиент как примитив (он уже Метрика-ориентирован, не Direct). Оркестрацию писать свою под Parquet (см. C).

⚠️ **Шов вендоринга:** `MetricaClient.__init__` (строка ~141) сам зовёт `AuthManager.get_metrica_credentials()` → вендоринг «как есть» тянет цепь `AuthManager → ConfigManager → tapi_yandex_direct`. При переносе развязать конструктор: инжектить готовые креды (`token`, `counter_id`) от тонкого env-ридера (§B, FR-4), а не звать `AuthManager` изнутри клиента. Это условие реализуемости FR-4.

## B. Креды

`scripts/utils/auth_manager.py::get_metrica_credentials()` (строки 376–393): токен через `_get_token_with_fallback("YANDEX_METRICA_TOKEN", ...)` (328–354) с fallback на `YANDEX_DIRECT_TOKEN`; `YANDEX_METRICA_COUNTER_ID` обязателен. Связан с `tapi_yandex_direct`/`ConfigManager` — **не чистый drop-in**.

**Наше решение:** тонкий env-ридер, читает `YANDEX_METRICA_TOKEN` + `YANDEX_METRICA_COUNTER_ID`, **без Direct-fallback**, без зависимости на ConfigManager. (FR-4.)

## C. Оркестратор загрузки — что берём, что меняем

`scripts/8x_metrica_logs_api/p81_load_logs.py` — класс `MetricaLogsLoader` (148–944). Цикл: `_process_date_range` → `_ensure_request` → `_wait_for_request` (poll 30s, max 60 мин, max 5 consecutive errors) → `_process_single_request` (скачать части → import → clean).

**Ключевые места `directaiq` и наши дельты (FR-ссылки):**

| Аспект | directaiq (как сейчас) | Наше решение (FR) |
|---|---|---|
| Состояние «что загружено» | `_get_loaded_dates_duckdb()` (265–285): `SELECT DISTINCT date`. Мета-таблица `table_metadata` существует в схеме (lazy-init, `BaseScript._log_table_operation()`), но загрузчик логов её для чекпойнта **не использует**. | Мета-таблица состояния как источник истины + реконсиляция против фактического `count()`/партиций (FR-12). |
| Идемпотентность | skip-loaded; `--force` дропает таблицу целиком. Перезалить один день нельзя. | Per-day перезалив = перезапись одной Parquet-партиции (FR-10). |
| Hot-window | Нет автоперезалива; `get_moscow_safe_end_date()` лишь clamp'ит `date2` (329–333). | Явное окно N дней (FR-11). Clamp сохраняем (FR-5). |
| Сверка строк | **WARNING, не fail** (`_import_to_db`, 763–786: `if imported != csv_rows: logger.warning(...)`, `exit_code=0`). | **Жёсткий fail** (FR-13). |
| Типизация | Строгий `CAST(... AS HUGEINT)` + `read_csv(types={...})` (105–145, 722–787). Битая ячейка → падает весь день. ID в HUGEINT, т.к. Metrica visitID > 2^63. | Сырой Parquet строками + `TRY_CAST` в рабочем слое (FR-6, FR-7). **Учесть HUGEINT** для visitID/clientID/watchID. |
| Crash-recovery | Частичный день при крэше считается загруженным (нужен `--force`). | Атомарная запись temp→rename (FR-14). |
| Скачивание | `download_log_request_part` → bytes → пишутся в `/data/logs/req_{id}/`, затем import, затем удаление TSV. | Свой путь: bytes → Parquet-партиция дня (минуя долгоживущий TSV или через temp). |

**Поля-ориентир (`directaiq`, p81 строки 51–95):** `VISITS_FIELDS` (~25 полей: visitID, clientID, date, dateTime, isNewUser, regionCity, deviceCategory, operatingSystem, goalsID/goalsDateTime, visitDuration, pageViews, startURL, referer, bounce, UTM*, watchIDs, + ecommerce-поля goalsPrice/goalsOrder/purchaseRevenue/purchaseID). `HITS_FIELDS` (~12: watchID, clientID, date, dateTime, URL, title, referer, goalsID, deviceCategory, isPageView, regionCity, ecommerce). **Наш заданный список** — отфильтровать под геймдев (убрать Direct/ecommerce-специфику, оставить поведенческие/retention; watchIDs↔watchID для join visits↔hits) — Open Question #1.

**CLI-ориентир:** `scripts/tools/logs_api_cli.py` (29–124) — подкоманды `list/create/status/download/clean/evaluate/load`, флаги `--date1/--date2/--fields/--source/--attribution/--request-id/--part/--output/--clean/--force`.

## D. MCP `duckdb_query`

Сервер: `scripts/mcp/directaiq_mcp_server.py` (81–149); ядро: `scripts/mcp/tools/core.py` (584 стр.); конфиг — `.mcp.json`. Интерфейс — **единственный инструмент** `duckdb_query(query: str, format: json|markdown|csv, limit: int) -> str`. Спец-команды в `query`: `--context` (семантика + таблицы со схемой/row counts/диапазонами дат + конфиг целей), `--tables`, `--schema [TABLE]`, `--sample TABLE [N]`, `--export "SELECT..." file.{csv|parquet|json}`. Поведение: авто-export при >500 строк в `data/results/`; audit-лог каждого вызова в `data/mcp_output/`.

**Завязка на схему directaiq:** сервер сам по себе schema-agnostic (работает с любой DuckDB через `DatabaseManager`), НО хардкодит семантику денег/НДС в `_COST_COLUMN_SEMANTICS` (`scripts/mcp/tools/core.py:26–39`) и читает goal_ids из `config_manager.py` для плейсхолдеров (`{{DATE_30D}}`, `{{PRIMARY_GOAL_ID}}` и т.п.).

**Наше решение (FR-17, FR-18):** переиспользовать как есть; «лёгкая» доработка чуть шире замены одного dict — реально три точки: (1) заменить `_COST_COLUMN_SEMANTICS` на нашу семантику колонок из каталога (FR-16) **и нейтрализовать regex-fallback** `(cost|.*_revenue)` (`core.py:~26–48`) — НДС/деньги Директа к геймдеву неприменимы, revenue в Метрике иной; (2) убрать/заменить goal-плейсхолдеры (`{{PRIMARY_GOAL_ID}}` и т.п., `core.py:~97–99`), завязанные на `config_manager`; (3) проверить остальные завязки на `config_manager`. Вердикт «лёгкая» в силе (интерфейс инструмента сохраняется), но это не однострочная правка. Объём — Open Question #4.

## E. Двух-репо: init и контракт симлинков

`scripts/nushell/init_project.nu` (14–347) — шаги-**ориентир** (точную последовательность пересобрать из живого `init_project.nu` на фазе архитектуры; перечень ниже укрупнён и может расходиться с кодом на ±1 шаг): проверка свободного имени `../{name}` → копирование `templates/external_storage/` → запись имён миграций в `.migrations_applied` → симлинки по `templates/paths-to-symlink.csv` → генерация `.env` (`DIRECTAIQ_DATA_ROOT=../{name}`) → проверка Python 3.13 → shared venv в `../shared_python_env/.venv` → `uv sync` (`SKIP_AUTO_MIGRATE=1`) → создание DuckDB + миграции схемы (`scripts/utils/migrate.py`) → `git init` + initial commit (с `git reset HEAD -- .env`, т.е. `.env` не коммитится).

**Контракт симлинков** (`templates/paths-to-symlink.csv`, 22 пути): `.claude/agents/*`, `.claude/commands/*`, `.claude/hooks`, `.claude/settings.json`, `.claude/skills/duckdb`, `.mcp.json`, `activate.sh`, `claude-code-docs`, `development-docs`, `marketing-methodology`, `pyproject.toml`, `scripts`, `toolkit.nu`, `yandex-docs`.

**Шаблон хранилища** (`templates/external_storage/`): `.claude/` (agents/commands/skills/settings.local.json), `.env`, `.gitignore`, `CLAUDE.md`, `config/` (project_config.yaml и пр.), `data/duckdb/` (пусто, БД создаётся в init), `data/incremental_source_csv/`, `data/standard_app_scripts_output/`, `scripts-local/`. Создаваемые пользователем: `data/results/`, `data/todo/`, `data/uploads/`.

**Наше решение (FR-19, FR-20, FR-21):** тот же паттерн, переименовать `DIRECTAIQ_DATA_ROOT` под наш проект, урезать шаблон под геймдев (убрать Direct/marketing-скилы и конфиги), заменить набор симлинкуемых скилов/команд на свои. Конкретный контракт путей и имя init-команды — за архитектурой.

## F. Каталог схемы — формат-ориентир

`directaiq` держит единый data dictionary в `development-docs/data-architecture.md` (~509 стр.): по каждой таблице — тип, примерный row count, семантика полей, особенности API, контракты (PK/UNIQUE), кросс-ссылки. Таблицы логов там — `t81_metrica_logs_visits`/`t81_metrica_logs_hits` (NO PK, ID в HUGEINT).

**Наше решение (FR-16):** свой каталог под наш заданный список — единый источник для DDL рабочего слоя и для `--context`/`--schema` MCP. Формат можно взять у `directaiq`, наполнение — своё.

## G. Открытые тех-вопросы (зеркало §8 PRD)

1. Точный список полей visits/hits (старт — отфильтрованные `VISITS_FIELDS`/`HITS_FIELDS`).
2. N в hot-window (черновик 3; directaiq использует 21 день для атрибуции Метрики — но это не сырьё).
3. Раскладка/именование Parquet-партиций; рабочий слой = view'ы поверх Parquet или материализованные таблицы.
4. Объём доработки MCP сверх `_COST_COLUMN_SEMANTICS` и плейсхолдеров.
5. Порог DuckDB→ClickHouse — замерить на первой выгрузке (тот же Parquet заливается в ClickHouse при нужде).
