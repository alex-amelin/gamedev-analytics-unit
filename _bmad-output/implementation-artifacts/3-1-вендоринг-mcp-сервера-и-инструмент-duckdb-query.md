# Story 3.1: Вендоринг MCP-сервера и инструмент `duckdb_query`

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита (агент),
I want MCP-сервер с единым инструментом `duckdb_query` поверх рабочего слоя,
so that выполнять произвольный SQL к данным игры (view'ы `visits`/`hits`) прямо из Claude Code — канал **только чтения**, не трогающий замок писателя.

## Acceptance Criteria

1. **Given** вендоренные `directaiq_mcp_server.py` + `tools/core.py`, **When** перенесены в `scripts/mcp/gdau_mcp_server.py` + `scripts/mcp/tools/core.py`, **Then** сервер поднимается на офиц. `mcp` SDK (`mcp.server.fastmcp.FastMCP`) и регистрируется через `.mcp.json` запуском `python -m scripts.mcp.gdau_mcp_server` (architecture.md:507).
2. **Given** единый инструмент, **When** агент его вызывает, **Then** доступен `duckdb_query(query, format, limit)`; произвольный SQL к view'ам `visits`/`hits` (2.6) выполняется и возвращает результат в запрошенном формате.
3. **Given** канал чтения, **When** MCP выполняет запрос, **Then** соединение **read-only** (`DatabaseManager.connection(read_only=True)`, 2.1); `.writer.lock` (2.5) **не берётся** и чтение им не блокируется.
4. **Given** `format ∈ {json, markdown, csv}`, **When** он задан, **Then** результат в этом формате (по умолчанию `json`; неизвестное значение отсечено `Literal`-типом инструмента, в ядре — дефолт `json`).
5. **Given** directaiq-брендинг и швы, **When** сервер вендорится, **Then** идентификаторы переименованы под gdau (`FastMCP("gdau_mcp")`, без `directaiq_*`), **And** сервер поднимается **без** завязки на `config_manager`/`auth_manager`/`ConfigManager` (их в этом репо нет — не вендорить; см. РЕШЕНИЕ о скоупе).
6. **Given** невалидный/синтаксически битый SQL от агента, **When** он выполняется, **Then** возвращается понятная ошибка (текст + подсказка), сервер остаётся жив (исключение ловится, не валит процесс). _[edge-case: битый SQL]_
7. **Given** запрос с записью через якобы read-only канал (`INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP`/`ALTER`/`ATTACH … (READ_WRITE)`/`COPY … TO`/`PRAGMA`/`SET`/`INSTALL`/`LOAD`), **When** он приходит, **Then** запись невозможна **двумя слоями**: (а) соединение открыто `read_only=True` И (б) statement-level guard пропускает только read-операции. _Эмпирически (DuckDB 1.5.3, проверено): `read_only=True` блокирует `CREATE`/`INSERT`/`DROP` (`InvalidInputException`), но **`COPY … TO` и `PRAGMA` проходят** — значит одного `read_only` НЕ достаточно, guard обязателен._ _[edge-case: обход read-only]_
8. **Given** запрос до первой выгрузки (БД/view'ы ещё не созданы), **When** он выполняется, **Then** понятное сообщение «данных пока нет, запусти `gdau-logs update`» (наследуется из `DatabaseManager` 2.1: read-only до создания `gdau.duckdb` → `RuntimeError` с этим текстом), а не «голое» `IOException`. _[edge-case: запрос до данных]_
9. **Given** конкурентную замену партиции оркестратором (`os.replace`, 2.2/2.7) во время чтения, **When** запрос читает Parquet-glob view'а, **Then** **однократный** retry на транзиентную ошибку чтения партиции (короткий backoff), затем — понятная ошибка, если не сошлось. _[edge-case: чтение во время записи]_
10. **Given** пустой/`None`/из пробелов `query`, невалидный `format` или `limit ≤0` / абсурдно большой, **When** разбираются аргументы, **Then** валидация: пустой query → подсказка (не сырой запрос в БД); неизвестный format → дефолт `json`; `limit` клампится в `[1, MAX_LIMIT]` (≤0 → дефолт, > MAX → MAX). _[edge-case: невалидные аргументы инструмента]_
11. **Given** разорительный/«убегающий» запрос (cross join), **When** он выполняется, **Then** действует прерывание по верхней границе времени. _Эмпирически (DuckDB 1.5.3, проверено): PRAGMA/SET `statement_timeout` **не существует** (`CatalogException: unrecognized configuration parameter`) → реализовать watchdog-таймером + `conn.interrupt()` (метод есть), не несуществующим PRAGMA._ _[edge-case: runaway-запрос без таймаута]_

---

## Главные риски / решения (читать ДО кода)

> Эта история — **вендоринг скелета MCP-сервера directaiq + единый инструмент `duckdb_query` под наш рабочий слой**, с развязкой швов и read-only-дисциплиной. Сервер ничего не пишет в БД; он открывает **read-only**-соединение (2.1) к `gdau.duckdb` и гоняет SQL по view'ам `visits`/`hits` (2.6). Архитектура называет это **«лёгкой доработкой»** вендоренного инструмента (architecture.md:235-238), интерфейс `duckdb_query(query, format, limit)` сохраняется.

### ✅ РЕШЕНИЕ (зафиксировано, требует подтверждения Шефа на скоуп): **вариант A — тонкий read-канал; сервисные команды и семантика — в 3.2/3.3**

**Корень.** Эпик 3 разбит на три истории: **3.1** (этот сервер + `duckdb_query`), **3.2** (сервисные команды `--context/--tables/--schema/--sample/--export` + авто-экспорт >500 + audit-лог), **3.3** (контекст/семантика из каталога + снятие `config_manager`/goal-плейсхолдеров/`_COST_COLUMN_SEMANTICS`/regex-fallback). Вопрос: сколько кода `core.py` приходит в 3.1.

**Зафиксировано A:** 3.1 вендорит **только** путь исполнения произвольного SQL — сервер-модуль + `tools/core.py` с `execute_query` (read-only conn + форматтеры json/markdown/csv + классификатор SQL-ошибок), `handle_query` (входная точка; в 3.1 — только plain SQL, без спец-команд), валидацией аргументов, statement-guard'ом записи, таймаутом и retry. **НЕ** вендорятся: `process_sql_placeholders`/`get_config` (config_manager — goal-плейсхолдеры, 3.3), `_handle_context`/`_handle_schema`/`_export_query`/`--tables`/`--sample` (сервисные команды, 3.2), `_save_audit_log`/`get_mcp_output_dir`/`get_output_dir` (аудит/экспорт, 3.2), `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/`_GENERIC_MONEY_COL_RE` (Direct/НДС-семантика, 3.3).

**Почему A (учитывая ограничения и цели):**
- **`config_manager` в этом репо НЕ вендорился** (осознанно вырезан, project-context «не тащить инфру directaiq»; `database_manager.py`/`paths.py` его уже исключили). Вендоренный `scripts/mcp/utils/common.py:get_config()` импортирует `scripts.utils.config_manager` → в нашем репо это **`ImportError` на старте** → сервер не поднимется. То есть вариант «принести весь `core.py` как есть, чистить потом» **физически не загружается** и нарушил бы AC #1/#5. Вариант A — единственный, что загружается зелёным.
- **AC #5 прямо требует** «без остаточной завязки на `directaiq_*`» и «без `config_manager`» — в 3.1, не «потом». A удовлетворяет дословно (этой завязки просто нет в принесённом коде).
- **Граница эпика.** Сервисные команды эпик явно отдаёт 3.2, контекст/семантику — 3.3. Тащить их в 3.1 = выход за скоуп истории и дубль работы 3.2/3.3.
- **Простота-первой (NFR-6).** Тонкий первый срез read-канала: поднимается сервер, гоняется SQL, read-only защищён. Наращивание (сервис/контекст) — отдельными зелёными историями.

**Что это значит для `handle_query` в 3.1:** входная точка существует и в 3.1 исполняет **только произвольный SQL** (никакого роутинга `--context`/`--tables`/…). Если агент пришлёт `--tables` сейчас — это уйдёт в DuckDB как SQL → понятная синтаксическая ошибка (AC #6), не падение. Роутинг спец-команд добавляет 3.2 поверх той же `handle_query`. **Докстринг инструмента в 3.1 не рекламирует спец-команды, которых ещё нет** (чтобы агент не звал несуществующее).

> ⚠️ **Подтверждение Шефа:** скоуп-границу 3.1↔3.2↔3.3 (что именно тонкий срез read-канала, без сервис-команд/контекста/семантики) подтвердить. Реализацию это не блокирует — вариант A зафиксирован по ограничениям (config_manager не загрузится иначе) и границам эпика; если Шеф хочет иной раздел — сказать до dev-story.

### Риски (решены в дизайне ниже)

- **Риск №1 — read-only НЕ блокирует `COPY … TO` и `PRAGMA` (AC #7, критично).** Проверено на DuckDB 1.5.3: под `read_only=True` `CREATE/INSERT/DROP` → `InvalidInputException` (хорошо), но `COPY (SELECT …) TO 'file' (…)` **успешно пишет файл на диск** (именно поэтому directaiq-экспорт работает через read-only conn), а `PRAGMA disable_optimizer` **проходит**. Значит «открыли read-only» ≠ «писать нельзя». **Нужен второй слой — statement-level guard:** разрешать только read-операции (allowlist ведущего ключевого слова: `SELECT`/`WITH`/`FROM`/`DESCRIBE`/`EXPLAIN`/`SHOW`/`VALUES`/`SUMMARIZE`/`TABLE`/`PIVOT`/`UNPIVOT`), отклонять всё прочее (`COPY`/`PRAGMA`/`SET`/`ATTACH`/`INSTALL`/`LOAD`/`CREATE`/`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`CALL`/`CHECKPOINT`/`EXPORT`/`IMPORT`). **Плюс запрет мульти-стейтмента:** срезать хвостовой `;`; если в остатке ещё есть `;` → отказ (иначе `SELECT 1; COPY (...) TO 'x'` обойдёт проверку ведущего слова — второй стейтмент пишет файл). Два слоя (read-only conn + guard + one-statement) = запись невозможна.
- **Риск №2 — таймаут не через PRAGMA (AC #11).** `statement_timeout` в DuckDB **нет** (проверено: `CatalogException`). Реализация: перед `conn.execute(...)` завести `threading.Timer(STATEMENT_TIMEOUT_S, conn.interrupt)`; по завершении/исключению — `timer.cancel()`. Прерывание поднимает `duckdb.InterruptException` → форматировать как «запрос превысил лимит времени ~Ns, упростите/добавьте фильтры». `conn.interrupt()` в 1.5.3 есть (проверено). **Гонка callback'а:** `timer.cancel()` НЕ отменяет уже-запущенный коллбэк → на граничном тайминге `conn.interrupt()` может выстрелить уже после `execute`. Безопасно, т.к. `conn` закрывается в том же `with`-блоке сразу за исполнением (interrupt на закрывающемся/per-call соединении — no-op, в следующий запрос не «протекает»); соединение per-call, не переиспользуется. Если решишь иначе — добавить флаг-«завершено» с проверкой в коллбэке.
- **Риск №3 — `.env` и `GDAU_DATA_ROOT` для read-канала.** MCP-чтение **не** ходит в Logs API → креды Метрики НЕ нужны (`read_metrica_credentials` НЕ звать — упадёт fail-loud без кредов зря). Нужен только `GDAU_DATA_ROOT` (резолюция `gdau.duckdb`). Claude Code **не грузит `.env` сам** ([[mcp-env-delivery]]) → сервер-модуль грузит сам: `load_dotenv(find_dotenv(usecwd=True), override=False)` в шапке (cwd MCP-процесса = каталог хранилища, где лежит `.env`). **Гоча [[dotenv-usecwd-gotcha]]:** `load_dotenv()` без `usecwd=True` ищет от каталога МОДУЛЯ (в wheel — site-packages, мимо `.env` оператора). `paths.get_storage_root()` читает `GDAU_DATA_ROOT` лениво (в момент вызова), не на импорте, поэтому фатального порядка импортов нет — но `.env` грузим в шапке до первого `duckdb_query`. (Образец загрузки — `env_reader._load_env`, но он creds-ориентирован и приватный; сервер делает свой `load_dotenv`, дублирование тут осознанно и минимально.)
- **Риск №4 — retry только на транзиентном чтении (AC #9).** Оркестратор (2.7) подменяет партицию `os.replace` (2.2); MCP-чтение view → glob `data/raw/{source}/*.parquet` может на миг наткнуться на исчезнувший/заменяемый файл → `duckdb.IOException`. **Однократный** retry с коротким сном (напр. 0.1s) **только** на IO/транзиентную ошибку чтения, **не** на синтаксис/каталог (битый SQL не ретраить — это AC #6). После повторного фейла — обычная классифицированная ошибка. _Ложного retry на пустом источнике не будет: `build_view_ddl` для источника без партиций (2.6, `has_partitions=False`) не использует `read_parquet` вовсе (`CAST(NULL …) WHERE false`), поэтому `IOException` бьёт только в настоящую гонку `os.replace`, не в «легитимно пусто»._
- **Риск №5 — лимит и переполнение ответа (AC #10).** directaiq-дефолт `limit=0` = «без лимита» + авто-экспорт >500 строк (3.2). В 3.1 авто-экспорта **нет**, поэтому неограниченный результат залил бы ответ агенту. **Меняем контракт лимита (осознанно, отличие от directaiq):** `limit ≤ 0 → DEFAULT_LIMIT` (напр. 100), `limit > MAX_LIMIT → MAX_LIMIT` (напр. 10000), иначе как есть → результат всегда ограничен `[1, MAX_LIMIT]`. JSON-форматтер несёт `has_more`/`next_offset` (наследуется), чтобы агент видел усечение. Авто-экспорт больших результатов — забота 3.2 (тогда дефолт лимита можно будет вернуть к «0 = без лимита + авто-экспорт»).
- **Риск №6 — сервер не должен падать (AC #6).** `duckdb_query` оборачивает `handle_query`; любое `duckdb.Error`/`Exception` ловится **внутри** `core.py` и возвращается строкой-сообщением (как directaiq: `except duckdb.Error → _format_sql_error`, `except Exception → "**Error:** {type}: {msg}"`). Голых исключений из инструмента наружу не выпускать — иначе MCP-сессия рвётся.
- **Риск №7 — мусорные dev-репо артефакты при тестах.** Тесты гоняют против временного хранилища (`tmp_path` + `monkeypatch.setenv(GDAU_DATA_ROOT)`), как `test_database_manager.py`/`test_views.py`. БД/parquet/`.env` в dev-репо не создавать. `gdau.duckdb` создаётся write-conn'ом в тестовой фикстуре (или через `views.create_views`), MCP читает его read-only.

---

## Tasks / Subtasks

- [x] **Task 1 — Ядро `scripts/mcp/tools/core.py` (вендоринг тонкого среза; AC #2/#4/#6/#7/#9/#10/#11)**
  - [x] Шапка-пометка вендоринга: `# vendored from directaiq @ <ref> (scripts/mcp/tools/core.py), seam: read-only + statement-guard; trimmed: config_manager/placeholders/context/schema/export/audit/Direct-VAT-semantics → 3.2/3.3`.
  - [x] **Форматтеры** (перенести verbatim, переименовать брендинг): `format_result_json`, `format_result_markdown`, `format_result_csv` (`columns, rows, limit`). JSON несёт `total_rows`/`has_more`/`next_offset` (для усечения по лимиту, риск №5).
  - [x] **`_format_sql_error(e, query) -> str`** (перенести verbatim) — классификация (`does not exist`→подсказка `--tables`/`--schema` будут в 3.2; `syntax error`; `type mismatch`; `division by zero`).
  - [x] **Statement-guard `_reject_if_not_readonly(sql) -> str | None`** (новое, риск №1): **сначала срезать ведущие комментарии** (`-- …\n` и `/* … */`) И пробелы, потом смотреть ведущее слово — иначе `'/* x */ COPY (…) TO …'`/`'-- c\nCOPY …'` обойдут allowlist (проверено вживую: под read_only такой запрос ПИШЕТ файл). Срезать хвостовой `;`; если ещё есть `;` → отказ (один стейтмент). Ведущее ключевое слово не в allowlist (`SELECT/WITH/FROM/DESCRIBE/EXPLAIN/SHOW/VALUES/SUMMARIZE/TABLE/PIVOT/UNPIVOT`) → отказ с понятным текстом «канал только для чтения, операция X запрещена». Регистронезависимо.
  - [x] **`_clamp_limit(limit) -> int`** (новое, риск №5/AC #10): `≤0 → DEFAULT_LIMIT`; `> MAX_LIMIT → MAX_LIMIT`; константы `DEFAULT_LIMIT`/`MAX_LIMIT` модульными.
  - [x] **`execute_query(query, output_format="json", limit=DEFAULT_LIMIT) -> str`** (адаптировать directaiq):
    - [x] guard записи (риск №1) → если отказ, вернуть его сразу (до открытия соединения).
    - [x] `display_limit = _clamp_limit(limit)`; нормализовать `output_format` (не в {json,markdown,csv} → `json`, AC #4/#10).
    - [x] `with DatabaseManager.connection(read_only=True) as conn:` (2.1; до создания БД → `RuntimeError` с текстом про `gdau-logs update`, AC #8 — **не** глушить, пробросить как понятную ошибку).
    - [x] **таймаут (риск №2/AC #11):** `timer = threading.Timer(STATEMENT_TIMEOUT_S, conn.interrupt); timer.start()`; `try: conn.execute(...).fetchall() finally: timer.cancel()`.
    - [x] **retry (риск №4/AC #9):** обернуть исполнение в однократный повтор на `duckdb.IOException` (или транзиентный `duckdb.Error` по сигнатуре чтения parquet) с `time.sleep(0.1)`; синтаксис/каталог НЕ ретраить.
    - [x] `if conn.description is None: return "_Запрос выполнен (без результата)_"`; иначе форматировать по `output_format`.
    - [x] `except duckdb.InterruptException → "запрос превысил лимит времени …"`; `except duckdb.Error → _format_sql_error`; `except Exception → "**Error:** {type}: {msg}"` (риск №6 — наружу не выпускать).
  - [x] **`handle_query(query, output_format="json", limit=DEFAULT_LIMIT) -> str`** (входная точка): `query = (query or "").strip()`; пустой → подсказка «дай SQL-запрос …» (AC #10). В 3.1 — **только** `return execute_query(query, output_format, limit)` (без роутинга спец-команд; их добавит 3.2 поверх этой же функции).
- [x] **Task 2 — Сервер `scripts/mcp/gdau_mcp_server.py` (вендоринг + развязка; AC #1/#3/#5)**
  - [x] Шапка-пометка вендоринга (как Task 1).
  - [x] **Bootstrap `.env` (риск №3):** в шапке (до импортов `scripts.*`) `from dotenv import find_dotenv, load_dotenv; load_dotenv(find_dotenv(usecwd=True), override=False)` ([[dotenv-usecwd-gotcha]]/[[mcp-env-delivery]]). При необходимости — `sys.path.insert(0, project_root)` (как directaiq; под `uv run` обычно не нужно — проверить).
  - [x] `from mcp.server.fastmcp import FastMCP`; `from mcp.types import ToolAnnotations`; `from pydantic import Field`; `from scripts.mcp.tools.core import handle_query`. **`mcp = FastMCP("gdau_mcp")`** (брендинг gdau, AC #5).
  - [x] **`@mcp.tool(name="duckdb_query", annotations=ToolAnnotations(title="DuckDB Query", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))`** — `readOnlyHint=True` (в 3.1 экспорта нет → канал чисто читающий; в directaiq был `False` из-за `--export`).
    - [x] **mypy strict + `@mcp.tool`:** под `strict=true` включён `disallow_untyped_decorators`; если SDK-декоратор `@mcp.tool` придёт нетипизированным/`Any` → допустим точечный `# type: ignore[misc]` **на строке декоратора** с комментарием-почему (SDK-декоратор не несёт типов). НЕ ослаблять strict глобально и НЕ добавлять стаб для `mcp`. Если ignore не понадобился — не добавлять.
  - [x] `def duckdb_query(query: Annotated[str, Field(...)], format: Annotated[Literal["json","markdown","csv"], Field(default="json", ...)]="json", limit: Annotated[int, Field(default=…, ge=0, ...)]=…) -> str:` → `return handle_query(query, format, limit)`. **Docstring под нашу схему:** упомянуть view'ы `visits`/`hits`, snake_case-колонки, что канал только для чтения; **НЕ** рекламировать `--context/--tables/--schema/--sample/--export` (их нет в 3.1) и НЕ упоминать Direct/НДС/goal-плейсхолдеры/`t10_*`/`t18_*`.
  - [x] `if __name__ == "__main__": mcp.run()`. **НЕ** вендорить `_save_audit_log`/`get_mcp_output_dir` (3.2) и `scripts/mcp/utils/common.py` (config_manager).
  - [x] `scripts/mcp/tools/__init__.py` — завести пакет (как у directaiq, минимальный docstring).
- [x] **Task 3 — `.mcp.json` (регистрация; AC #1)**
  - [x] В корне dev-репо `.mcp.json`: `{"mcpServers":{"gdau":{"command":"uv","args":["run","python","-m","scripts.mcp.gdau_mcp_server"]}}}` — кросс-платформенно (не `bash`/`activate.sh` как directaiq), `python -m scripts.mcp.gdau_mcp_server` (architecture.md:507). `.mcp.json` симлинкуется в хранилище контрактом 4.1 → запускается с cwd хранилища (там `.env`/`pyproject.toml`-симлинк). Финальную форму лаунчера допустимо уточнить в init 4.3 — 3.1 поставляет рабочую `uv run python -m`-форму.
- [x] **Task 4 — Спека `docs/mcp-query.md` (новый компонент; часть DoD)**
  - [x] Человеческим языком (3 вопроса project-context): **что делает** (агент шлёт SQL/формат/лимит → таблица/json/csv по view'ам `visits`/`hits`); **зачем** (канал чтения/анализа, замыкает «спросил → ответ в ту же сессию», SM-1/UJ-3); **контракт с другими** (read-only к `gdau.duckdb` 2.1, view'ы 2.6, лок писателя не берёт 2.5; до первой выгрузки → подсказка `gdau-logs update`; только чтение — запись отклоняется). Отметить, что сервисные команды/контекст/семантика — следующие истории (3.2/3.3).
- [x] **Task 5 — Offline-тесты `tests/test_mcp_core.py` (ядро) + `tests/test_gdau_mcp_server.py` (сервер)**
  - [x] Фикстура: `tmp_path` + `monkeypatch.setenv(DATA_ROOT_ENV, …)`; создать `gdau.duckdb` write-conn'ом + пару view'ов/таблиц (можно `views.create_views` поверх tmp-партиции parquet, либо простую таблицу — для SQL-исполнения достаточно). MCP читает read-only.
  - [x] **AC #2/#4:** `SELECT` отдаёт строки в json/markdown/csv; формат уважается.
  - [x] **AC #6:** битый SQL (`SELEC * FORM x`) → строка с `**SQL Error**`, исключение не вылетает.
  - [x] **AC #7 (двух-слойно):** `INSERT`/`CREATE`/`DROP` → отказ; `COPY (SELECT …) TO 'f.csv'` → **отказ guard'ом** (и файл НЕ создан); `PRAGMA …`/`ATTACH …`/`SET …`/`INSTALL …` → отказ; мульти-стейтмент `SELECT 1; COPY … TO …` → отказ; **comment-bypass `'/* x */ COPY (…) TO …'` и `'-- c\nCOPY (…) TO …'` → отказ** (единственный найденный путь обхода allowlist — зацементировать тестом). (Проверить именно guard, т.к. read-only сам `COPY TO`/`PRAGMA` пропускает — риск №1.)
  - [x] **AC #8:** хранилище без `gdau.duckdb` → `handle_query("SELECT 1")` отдаёт понятное сообщение про `gdau-logs update` (не сырой `IOException`).
  - [x] **AC #9:** monkeypatch `conn.execute` бросить `duckdb.IOException` один раз, потом успех → один retry → результат; бросать дважды → классифицированная ошибка (один повтор, не бесконечный).
  - [x] **AC #10:** пустой/`"   "`/`None` query → подсказка; `limit=0`/`-5` → `DEFAULT_LIMIT`; `limit=10**9` → `MAX_LIMIT`; неизвестный format → json.
  - [x] **AC #11:** runaway (`SELECT count(*) FROM range(1e12) a, range(1e12) b` или генератор) с малым `STATEMENT_TIMEOUT_S` (инъекция/monkeypatch) → `duckdb.InterruptException` → сообщение про лимит времени; таймер отменяется на быстром запросе (нет ложного прерывания).
  - [x] **AC #1/#3/#5 (сервер):** модуль импортируется и регистрирует инструмент `duckdb_query`; `FastMCP("gdau_mcp")`; **ast/import-анти-зависимость**: в `scripts/mcp/**` нет импорта `config_manager`/`auth_manager`/`directaiq`/`scripts.mcp.utils.common` (как `test_database_manager.py` проверяет вырезанную инфру по узлам импорта, не подстрокой); соединение открывается `read_only=True` (проверить, что `DatabaseManager.connection` зовётся с `read_only=True` — напр. monkeypatch-шпион).
- [x] **Гейты перед сдачей**
  - [x] `uv run mypy scripts` → зелено (config `strict=true`; новые `scripts/mcp/gdau_mcp_server.py` + `scripts/mcp/tools/core.py` + `__init__.py`; type hints везде, без `Any`-дыр; аннотации FastMCP/pydantic типизированы). CI гоняет `mypy scripts` на матрице **ubuntu+windows** (.github/workflows/tests.yml); локально на win32 полезен доп. кросс-чек `mypy scripts --platform linux` (ловит `sys.platform`-ветки — у 3.1 их нет, но это house-convention).
  - [x] `uv run pytest` (offline) → зелено; маркер `live` не вводится (см. ниже).
  - [x] `uv.lock` не менялся (`mcp>=1.2` уже в зависимостях; `pydantic` — транзитив `mcp`; `python-dotenv`/`duckdb` уже есть). Если mypy потребует стаб для `mcp`/`pydantic` — это сигнал к обсуждению, не молча добавлять зависимость.
  - [x] Чек-лист «Definition of Done» пройден.

### Review Findings

_Code-review 2026-05-25 (3 слоя Opus: Blind Hunter / Edge Case Hunter / Acceptance Auditor). Все 11 AC — PASS; гейты перепрогнаны зелёными (mypy strict 22 файла, pytest MCP 30 passed, `uv.lock`/`pyproject.toml` не менялись). 4 patch / 0 decision / 3 defer / 9 dismiss._

- [x] [Review][Patch] **`EXPLAIN ANALYZE COPY (…) TO 'file'` обходит read-only guard (нарушение AC #7)** [scripts/mcp/tools/core.py:63,98] — `EXPLAIN` в allowlist ведущих слов, поэтому `_reject_if_not_readonly` пропускает запрос; эмпирически подтверждено (DuckDB 1.5.3): `EXPLAIN ANALYZE COPY (SELECT * FROM visits) TO 'leak.csv'` **пишет файл** под `read_only=True` (тогда как `EXPLAIN COPY` без ANALYZE — нет). Это ровно тот вектор записи через read-канал, который AC #7 обязан закрыть. Фикс: после извлечения ведущего слова срезать префикс `EXPLAIN [ANALYZE]` и валидировать ведущее слово **вложенного** стейтмента против allowlist (`EXPLAIN SELECT` → ок; `EXPLAIN ANALYZE COPY` → отказ). Источник: Edge Case Hunter (High, верифицировано в ревью). **✅ Исправлено 2026-05-25:** regex-срез `EXPLAIN [ANALYZE]` перед проверкой allowlist + регресс-тест `test_guard_rejects_explain_analyze_copy_and_no_file_written` (отказ + файл не создан) + `EXPLAIN SELECT`/`EXPLAIN ANALYZE SELECT` добавлены в allow-набор.
- [x] [Review][Patch] **CSV: строка заголовка не квотируется по RFC4180** [scripts/mcp/tools/core.py:219] — значения квотируются (`,`/`"`/`\n`), а заголовок `",".join(columns)` — нет. Колонка-алиас с запятой/кавычкой (`SELECT 1 AS "a,b"`) ломает соответствие числа колонок шапки и строк. Источник: Blind Hunter (Low). **✅ Исправлено 2026-05-25:** общий хелпер `_csv_quote` применён к заголовку И значениям (+ `\r`); тест `test_csv_header_is_rfc4180_quoted`.
- [x] [Review][Patch] **Markdown: заголовок не экранирует `|`** [scripts/mcp/tools/core.py:160] — значения экранируют `|` (стр. 173), заголовок `" | ".join(columns)` — нет. Колонка-алиас с `|` вставляет лишний разделитель в шапку → таблица разъезжается. Источник: Blind Hunter (Low). **✅ Исправлено 2026-05-25:** хелпер `_md_escape` применён к заголовку; тест `test_markdown_header_escapes_pipe`.
- [x] [Review][Patch] **Markdown: значение ячейки со встроенным `\n` ломает однострочность ряда** [scripts/mcp/tools/core.py:170-174] — экранируется только `|`, перевод строки в значении (свободный текст/referer) — нет → один логический ряд разрывается на несколько физических строк. CSV-путь это квотирует (стр. 227), markdown — нет. Источник: Edge Case Hunter (Low). **✅ Исправлено 2026-05-25:** `_md_escape` заменяет `\r`/`\n` на пробел; тест `test_markdown_cell_newline_does_not_break_row`.
- [x] [Review][Defer] **Лимит применяется после полного `fetchall()` — нет защиты памяти на огромных результатах** [scripts/mcp/tools/core.py:284] — deferred: `MAX_LIMIT` ограничивает только ответ агенту (намерение риска №5 — display-cap, выполнено), но `SELECT * FROM hits` материализует все строки в память до усечения; реальная защита больших результатов = авто-экспорт >500, осознанно отнесён к 3.2.
- [x] [Review][Defer] **Retry переиспользует то же соединение + даёт свежий таймаут (≈2× в худшем) + узкая гонка interrupt→retry** [scripts/mcp/tools/core.py:313-322] — deferred: AC #9 «однократный повтор» соблюдён; повтор на том же `conn` после `IOException` может (а) дать суммарно ~60 c при двух почти-таймаутах и (б) в крайне узком окне (таймер выстрелил на границе) словить InterruptException на retry → ложное «превысил лимит». Низкоприоритетное упрочнение (свежий conn на повтор) — узкая гонка.
- [x] [Review][Defer] **Два теста AC #11 используют 0.5 c wall-clock-бюджет → риск флака на нагруженном CI** [tests/test_mcp_core.py] — deferred: `test_fast_query_not_interrupted`/`test_runaway_query_interrupted_by_timeout` завязаны на стенные 0.5 c; на загруженном раннере быстрый запрос может перевалить бюджет (ложное прерывание). Упрочнение — расширить запас по времени.

## Dev Notes

### Рекомендуемый контракт 3.1 (вариант A; сервис/контекст/семантика — 3.2/3.3)

| Имя | Сигнатура | Смысл | Где |
|---|---|---|---|
| `duckdb_query` | `(query: str, format: Literal["json","markdown","csv"]="json", limit: int=…) -> str` | MCP-инструмент (FastMCP), тонкая обёртка → `handle_query` | `gdau_mcp_server.py` (новое) |
| `handle_query` | `(query, output_format="json", limit=…) -> str` | входная точка: пустой query → подсказка; иначе `execute_query` (в 3.1 без роутинга спец-команд) | `tools/core.py` (новое) |
| `execute_query` | `(query, output_format="json", limit=…) -> str` | guard записи → read-only conn → timeout+retry → fetch → формат; ловит все ошибки в строку | `tools/core.py` (новое) |
| `_reject_if_not_readonly` | `(sql) -> str \| None` | statement-guard (allowlist + one-statement), риск №1/AC #7 | `tools/core.py` (новое) |
| `_clamp_limit` | `(limit) -> int` | `[1, MAX_LIMIT]`, AC #10 | `tools/core.py` (новое) |
| `format_result_{json,markdown,csv}` | `(columns, rows, limit) -> str` | форматтеры (вендоринг verbatim) | `tools/core.py` |
| `_format_sql_error` | `(e, query) -> str` | классификация SQL-ошибки (вендоринг verbatim) | `tools/core.py` |

**Карта примитивов, которые зовём (сверены с фактическим кодом 2026-05-25):**
- `DatabaseManager.connection(read_only=True)` (`database_manager.py:39`) — контекст-менеджер; read-only до создания `gdau.duckdb` → `RuntimeError("БД не инициализирована: … — запусти gdau-init или gdau-logs update")` **до** `duckdb.connect` (AC #8 наследуется бесплатно). Закрытие в `finally` (AC #3 — хэндл не течёт). **Лок не берётся** (чтение, FR-15/2.5).
- `views.create_views(conn, …)` (`views.py:117`) / `build_view_ddl` — для тестовой фикстуры (создать `visits`/`hits` поверх tmp-партиции). В проде view'ы заводит init 4.3 / `ingest_range` 2.7; MCP их только читает.
- `paths.get_db_path()` (`paths.py:92`) — путь `gdau.duckdb`; fail-loud при не заданном/несуществующем `GDAU_DATA_ROOT` (наследуется через `DatabaseManager`).
- **НЕ зовём:** `read_metrica_credentials` (чтение API не делает — креды не нужны, риск №3); `writer_lock` (чтение лок не берёт); `MetricaClient`/`p81`/`parquet_store`/`load_state` (это путь записи Epic 1/2).

### Что НЕ вендорим в 3.1 (приходит в 3.2/3.3) — чтобы dev не притащил по инерции

| Из directaiq `core.py`/server | Куда | Почему не в 3.1 |
|---|---|---|
| `process_sql_placeholders` + `get_config` (`{{DATE_30D}}`/`{{PRIMARY_GOAL_ID}}`/`{{GOAL_COLUMNS}}`) | **3.3** | завязка на `config_manager` (нет в репо → ImportError); goal-плейсхолдеры не наша схема |
| `_handle_context` / `_handle_schema` / `--tables` / `--sample` | **3.2** | сервисные команды навигации |
| `_export_query` / авто-экспорт >500 / `--export` / `get_output_dir` | **3.2** | экспорт + порог |
| `_save_audit_log` / `get_mcp_output_dir` / `scripts/mcp/utils/common.py` | **3.2** | audit-лог |
| `_COST_COLUMN_SEMANTICS` / `_annotate_money_column` / `_GENERIC_MONEY_COL_RE` | **3.3** | Direct/НДС-семантика, заменяется семантикой каталога |

### Паттерны (соблюдать — снижают цикл ревью)
- `from __future__ import annotations` первой строкой; русские docstrings/комментарии (модульный обязателен), английские идентификаторы; type hints везде, `mypy --strict`, без `Any`-дыр; абсолютные импорты от корня (`from scripts.utils.database_manager import DatabaseManager`); `logger = logging.getLogger(__name__)` (диагностика — `logging`, не `print`).
- **Вендоренный код — с шапкой-пометкой** «vendored from directaiq @ <ref>, seam: …, trimmed: …» (project-context: вендоренное держим сравнимым с источником, развязка только в обозначенных швах: read-only + guard).
- **Комментарии «почему», не «что»** — особенно у guard'а (read-only пропускает COPY TO/PRAGMA — риск №1) и таймера (нет statement_timeout — риск №2): зафиксировать причину у кода.
- **Не тащить** инфру directaiq (`config_manager`/`auth_manager`/`BaseScript`) и тяжёлые зависимости. Новых зависимостей не добавлять — `mcp`/`pydantic`/`duckdb`/`python-dotenv` уже в стеке.
- **Read-only — инвариант канала** (project-context «CLI=запись, MCP=только чтение»): соединение `read_only=True` + guard; писать в БД/брать лок нельзя ни при каких аргументах.

### Границы 3.1 (не выходить)
- Трогаем: `scripts/mcp/gdau_mcp_server.py` (новое), `scripts/mcp/tools/core.py` (новое), `scripts/mcp/tools/__init__.py` (новое), `.mcp.json` (новое), `docs/mcp-query.md` (новое), `tests/test_mcp_core.py` + `tests/test_gdau_mcp_server.py` (новые). `scripts/mcp/__init__.py` уже есть (не трогать сверх нужды).
- **Не** реализуем: сервисные команды/контекст/схему/сэмпл/экспорт/авто-экспорт/audit (3.2), семантику каталога/снятие `config_manager`-швов/goal-плейсхолдеров/regex-fallback (3.3 — у нас их просто нет в принесённом коде).
- **Не** трогаем код Epic 1/2 (клиент/оркестратор/запись/каталог/view'ы/лок) — только **читаем** через `DatabaseManager`/`views`.

### Project Structure Notes
- Раскладка по architecture.md:461-463: `scripts/mcp/gdau_mcp_server.py` + `scripts/mcp/tools/core.py`; запуск MCP — через `.mcp.json` (`python -m scripts.mcp.gdau_mcp_server`, architecture.md:507), **НЕ** через `[project.scripts]` (там только `gdau-logs`/`gdau-init`). Entry-point для MCP не заводить.
- `scripts/mcp/__init__.py` существует (docstring «MCP-сервер доступа агента к данным (Epic 3)»). `scripts/mcp/tools/__init__.py` — завести.
- Тесты: плоские `tests/test_<area>.py` (как в репо). `conftest.py` нет — `tmp_path`/`monkeypatch` напрямую. Маркер `live` + `addopts="-m 'not live'"` (1.3) — для **live против реального Logs API**; MCP-чтение в Logs API не ходит.
- `gdau.duckdb`/`*.parquet`/`.env` — артефакты хранилища (`GDAU_DATA_ROOT`); в dev-репо не создаются/не коммитятся. `.mcp.json` — коммитится (конфиг, не секрет; симлинкуется в хранилище 4.1).
- `mcp`/FastMCP: офиц. `mcp` SDK, `mcp.server.fastmcp.FastMCP` (НЕ отдельный `fastmcp` 3.x — другая архитектура, architecture.md:165-166). Версия SDK в lock; API `@mcp.tool`/`ToolAnnotations`/pydantic `Field` сверить с установленной (репозиторий пинит `mcp>=1.2`).
- Не переводить на src-layout, не переименовывать пакет `scripts` (hatchling `packages=["scripts"]`).

### Live-smoke / DoD
- **Live неприменим** (как 2.1/2.6): MCP-чтение **не дёргает внешний Logs API** — читает локальный `gdau.duckdb`. Мандат live-smoke (project-context) касается контракта внешнего API; здесь его нет. Достаточно offline против временного DuckDB.
- **Ручной smoke (опционально, не тест):** поднять сервер `uv run python -m scripts.mcp.gdau_mcp_server` против `G:\gdau-smoke` ([[gdau-smoke-live-storage]] — там есть `gdau.duckdb` с данными) и проверить `duckdb_query("SELECT count(*) FROM visits")` из Claude Code. Описать в `docs/mcp-query.md` как проверить.

### Эмпирические факты (DuckDB 1.5.3, проверено в этой сессии — грунт под AC #7/#11)
- `read_only=True`: `CREATE/INSERT/DROP` → `InvalidInputException` (блок), но **`COPY (SELECT…) TO 'file'` пишет файл** и **`PRAGMA …` проходит** → нужен statement-guard (риск №1, AC #7).
- `SET/PRAGMA statement_timeout` → `CatalogException: unrecognized configuration parameter` (не существует) → таймаут через `threading.Timer` + `conn.interrupt()` (метод есть), риск №2/AC #11.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story 3.1] (строки 375-393) — 11 AC, edge-cases (битый SQL, обход read-only incl. COPY TO/PRAGMA/ATTACH READ_WRITE, запрос до данных, чтение во время записи, невалидные аргументы/лимит, runaway-таймаут). [#Epic 3] (371-373) — «лёгкая доработка + сервис-команды + контекст», раздел на 3.1/3.2/3.3.
- [Source: prd.md#FR-17] (строки 259-266) — единый `duckdb_query(query, format, limit)`, произвольный SQL → результат в json/markdown/csv, авто-экспорт >500 (это 3.2). [#FR-18] (268-275) — контекст/семантика из каталога, снятие Direct/НДС (это 3.3); доработка «лёгкая», интерфейс сохраняется. [#SM-1] (343) / [#UJ-3] — «спросил → ответ в ту же сессию». [#Один писатель] (234) — чтение лока не требует.
- [Source: architecture.md] — :235-238 (MCP=канал чтения, лёгкая доработка: заменить `_COST_COLUMN_SEMANTICS`/нейтрализовать regex/убрать goal-плейсхолдеры/`config_manager` — это 3.3; интерфейс+сервис-команды сохраняются); :426 (маппинг файлов `directaiq_mcp_server.py`+`tools/core.py` → `gdau_mcp_server.py`+`tools/core.py`); :461-463 (раскладка `scripts/mcp/`); :507 (`.mcp.json` → `python -m scripts.mcp.gdau_mcp_server`); :515-516, :539 (поток чтения: Claude Code → MCP `duckdb_query` → read-only DuckDB → view'ы; запись=lock, чтение=read-only без лока); :165-166 (офиц. `mcp` SDK FastMCP, не `fastmcp` 3.x); :601 (OQ#4 residual — финальный список правок `core.py` снятия `config_manager`/плейсхолдеров → 3.3).
- [Source: scripts/utils/database_manager.py:39] — `DatabaseManager.connection(read_only=True)`: RuntimeError до создания БД (AC #8), `finally`-close (AC #3), лок не берёт.
- [Source: scripts/utils/views.py:52,117] — `build_view_ddl`/`create_views(conn, …)`; view'ы `visits`/`hits` (snake_case, `TRY_CAST`, HUGEINT) — то, что MCP читает.
- [Source: scripts/utils/paths.py:92] — `get_db_path()` fail-loud при битом `GDAU_DATA_ROOT`. [scripts/utils/env_reader.py:62] — `_load_env` (образец загрузки `.env`: storage `.env` + `find_dotenv(usecwd=True)`); MCP делает свой `load_dotenv` (риск №3).
- [Source: G:\git\directaiq\scripts\mcp\directaiq_mcp_server.py + tools\core.py + .mcp.json] — источник вендоринга ([[directaiq-vendor-source]]): структура сервера, `@mcp.tool`/`ToolAnnotations`, `handle_query`/`execute_query`/форматтеры/`_format_sql_error` (берём), `process_sql_placeholders`/`_handle_context`/`_handle_schema`/`_export_query`/`_save_audit_log`/`_COST_COLUMN_SEMANTICS`/`common.get_config` (НЕ берём — 3.2/3.3).
- [Source: _bmad-output/project-context.md] — каналы (MCP=только чтение), read-only до init→RuntimeError не IOException, вендоринг с шапкой+развязка швов, не тащить `config_manager`, docs/<component>.md как часть DoD, тесты по import-узлам не подстрокой.
- [Memory] [[mcp-env-delivery]] (Claude Code не грузит `.env` — сервер грузит сам / `uv --env-file`), [[dotenv-usecwd-gotcha]] (`find_dotenv(usecwd=True)`), [[gdau-smoke-live-storage]] (`G:\gdau-smoke` для ручного smoke), [[gdau-env-contract]] (`GDAU_DATA_ROOT`).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Claude Opus 4.7, 1M context) — dev-story workflow.

### Debug Log References

Эмпирическая проверка фактов DuckDB 1.5.3 (грунт под AC #7/#11), воспроизведено в этой сессии:

- `read_only=True`: `CREATE`/`INSERT`/`DROP` → `InvalidInputException` (блок), но `COPY (SELECT…) TO 'file'` **пишет файл** и `PRAGMA …` **проходит** → одного `read_only` мало, statement-guard обязателен.
- `SET/PRAGMA statement_timeout` → `CatalogException: unrecognized configuration parameter` (не существует) → таймаут через `threading.Timer` + `conn.interrupt()` (метод есть).
- `InterruptException`/`IOException` — подклассы `duckdb.Error` → `except InterruptException` стоит ДО `except duckdb.Error`.
- Watchdog проверен на «убегающем» cross join `range(1e9)×range(1e9)`: быстрый запрос (`SELECT 1`) не прерывается, runaway прерывается через ~0.6 c при `timeout=0.5`.
- mypy strict: `@mcp.tool`-декоратор SDK типизирован → `# type: ignore[misc]` НЕ понадобился (не добавлен).

Одна правка по ходу тестов: guard режет опечатку ведущего слова (`SELEC …`) как операцию не из allowlist (корректный fail-closed) — тест AC #6 переведён на `SELECT * FORM visits` (валидное ведущее слово SELECT проходит guard, падает уже в парсере движка).

### Completion Notes List

Реализован **вариант A — тонкий read-канал** (скоуп подтверждён Шефом): сервер `FastMCP("gdau_mcp")` + единый инструмент `duckdb_query(query, format, limit)` → `handle_query` → `execute_query` поверх read-only соединения `gdau.duckdb` (2.1) и view'ов `visits`/`hits` (2.6).

- **AC #1/#5:** сервер на офиц. `mcp` SDK (`mcp.server.fastmcp.FastMCP`), регистрация через `.mcp.json` (`uv run python -m scripts.mcp.gdau_mcp_server`); брендинг `gdau`, **без** `config_manager`/`auth_manager`/`directaiq_*` (закреплено ast-анти-зависимостью по import-узлам).
- **AC #2/#4:** произвольный SQL → результат в `json`/`markdown`/`csv`; неизвестный формат в ядре → дефолт `json` (на инструменте отсекается `Literal`-типом).
- **AC #3/#7 (read-only двумя слоями):** соединение `read_only=True` + statement-guard `_reject_if_not_readonly` (allowlist ведущего слова + срез ведущих комментариев + запрет мульти-стейтмента). Режет `COPY … TO`/`PRAGMA`/`SET`/`ATTACH`/`INSTALL`/`LOAD`/`INSERT`/`CREATE`/`DROP`/… и comment-bypass `/* */ COPY`/`-- \nCOPY` (единственный найденный путь обхода). `.writer.lock` не берётся.
- **AC #6:** все ошибки ловятся внутри `core.py` и возвращаются строкой (`_format_sql_error`/`**Error:**`) — сервер не падает.
- **AC #8:** запрос до первой выгрузки → `RuntimeError` «… запусти gdau-logs update» из `DatabaseManager` ловится и отдаётся понятным текстом, не сырой `IOException`.
- **AC #9:** однократный retry на `duckdb.IOException` (гонка `os.replace` партиции); синтаксис/каталог не ретраятся.
- **AC #10:** пустой/`None`/из пробелов query → подсказка; `_clamp_limit` → `[1, MAX_LIMIT]` (`DEFAULT_LIMIT=100`, `MAX_LIMIT=10000`).
- **AC #11:** watchdog `threading.Timer` + `conn.interrupt()` → `InterruptException` → сообщение про лимит времени.

`docs/mcp-query.md` заведён (что/зачем/контракт + границы 3.2/3.3 + ручной smoke). Тесты: `tests/test_mcp_core.py` (24) + `tests/test_gdau_mcp_server.py` (6) — 30 новых.

Гейты зелёные: `mypy scripts` strict win32 + `--platform linux` (22 файла), `uv run pytest` 363 passed / 8 live deselected (было 333), `uv.lock`/`pyproject.toml` не менялись. Live неприменим (MCP-чтение в Logs API не ходит — читает локальный `gdau.duckdb`).

Изменения НЕ закоммичены — ветка `story/3.1-mcp-server` ждёт code-review и merge в `main` по workflow.

### File List

- `scripts/mcp/gdau_mcp_server.py` (новый) — сервер `FastMCP("gdau_mcp")`, инструмент `duckdb_query`, `.env`-bootstrap.
- `scripts/mcp/tools/__init__.py` (новый) — пакет инструментов.
- `scripts/mcp/tools/core.py` (новый) — `handle_query`/`execute_query`, форматтеры, statement-guard, кламп лимита, watchdog-таймаут, retry, классификатор ошибок.
- `.mcp.json` (новый) — регистрация сервера `gdau` (`uv run python -m scripts.mcp.gdau_mcp_server`).
- `docs/mcp-query.md` (новый) — спека компонента MCP-чтения.
- `tests/test_mcp_core.py` (новый) — offline-тесты ядра (AC #2/#4/#6/#7/#8/#9/#10/#11 + read-only spy).
- `tests/test_gdau_mcp_server.py` (новый) — offline-тесты сервера (AC #1/#3/#5 + ast-анти-зависимость).

## Change Log

- 2026-05-25 — dev-story: реализован вариант A (тонкий read-канал). Новые `scripts/mcp/gdau_mcp_server.py` + `scripts/mcp/tools/core.py` + `tools/__init__.py` + `.mcp.json` + `docs/mcp-query.md`; +30 offline-тестов. Все 11 AC закрыты; гейты зелёные (mypy strict win32+linux 22 файла, pytest 363/8); `uv.lock` не менялся. Status: ready-for-dev → review.

## Definition of Done

1. `scripts/mcp/gdau_mcp_server.py`: `FastMCP("gdau_mcp")`, инструмент `duckdb_query(query, format, limit)` → `handle_query`; `.env`-bootstrap (`find_dotenv(usecwd=True)`); `mcp.run()`; **без** `config_manager`/`directaiq_*`/audit. (AC #1/#3/#5)
2. `scripts/mcp/tools/core.py`: `handle_query`→`execute_query` (read-only conn 2.1, форматтеры json/markdown/csv, классификатор ошибок); statement-guard записи (allowlist+one-statement, AC #7); clamp лимита `[1,MAX]` (AC #10); watchdog-таймаут `conn.interrupt()` (AC #11); однократный retry на транзиентном чтении (AC #9); все ошибки — в строку, сервер жив (AC #6). **Без** спец-команд/плейсхолдеров/семантики (3.2/3.3). (AC #2/#4/#6/#7/#9/#10/#11)
3. Read-only гарантирован двумя слоями: `read_only=True` + guard; `.writer.lock` не берётся; запись (`INSERT/DROP/COPY TO/PRAGMA/ATTACH/INSTALL/SET`) отклонена. (AC #3/#7)
4. Запрос до первой выгрузки → понятное «нет данных, запусти `gdau-logs update`», не сырой IOException. (AC #8)
5. `.mcp.json`: сервер `gdau` через `uv run python -m scripts.mcp.gdau_mcp_server`, кросс-платформенно. (AC #1)
6. `docs/mcp-query.md` заведён (что/зачем/контракт; read-only; границы 3.2/3.3). (project-context: компонент без актуальной спеки не «готов»)
7. Offline-тесты `test_mcp_core.py` + `test_gdau_mcp_server.py`: SQL+форматы / битый SQL / двух-слойный read-only (COPY TO+PRAGMA+мульти-стейтмент отклонены) / до-данных / retry / валидация аргументов+лимит / таймаут / регистрация инструмента + import-анти-зависимость (`config_manager`/`directaiq` не импортятся). (AC #1–#11)
8. Гейты зелёные: `mypy scripts` (config `strict=true`; CI — матрица ubuntu+windows, локально доп. кросс-чек `--platform linux`), `pytest` (offline, ubuntu+windows); `uv.lock`/`pyproject.toml` не менялись. Live неприменим (MCP-чтение в Logs API не ходит).
