# Story 3.2: Сервисные команды MCP и авто-экспорт/аудит

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита (агент),
I want команды навигации по данным (`--tables`/`--schema`/`--sample`) и безопасный вывод больших результатов (`--export` + авто-экспорт >500 строк + audit-лог каждого вызова),
so that ориентироваться в рабочем слое и не переполнять ответ — поверх тонкого read-канала `duckdb_query` из 3.1.

## Acceptance Criteria

1. **Given** спец-команды в `query`, **When** вызываются `--tables`, `--schema [TABLE]`, `--sample TABLE [N]`, `--export "SELECT..." file.{csv|parquet|json}`, **Then** каждая отрабатывает: `--tables` — список таблиц/view рабочего слоя; `--schema` — схема всех объектов; `--schema TABLE` — колонки/типы одной таблицы; `--sample TABLE [N]` — N строк-примеров; `--export` — результат SELECT в файл под `data/results/`. Роутинг добавляется в **существующую** `handle_query` (3.1) ПЕРЕД fall-through на `execute_query`. _(`--context` — НЕ здесь, он в 3.3: требует семантику каталога.)_
2. **Given** результат > порога (по умолчанию 500 строк), **When** он возвращается, **Then** авто-экспорт в `data/results/` вместо переполнения ответа; возвращается статус-сообщение `«Результат велик (N строк). Экспортирован в …»`. Порог — модульная константа `AUTO_EXPORT_THRESHOLD = 500`.
3. **Given** каждый вызов инструмента `duckdb_query`, **When** он выполнен, **Then** пишется audit-лог-конверт (`{tool, timestamp, parameters, result}`) в `data/mcp_output/` (JSON-файл с таймстампом в имени). Аудит — в обёртке инструмента (`gdau_mcp_server.py`), ПОСЛЕ получения результата.
4. **Given** `--schema`/`--sample` с именем таблицы, **When** оно обрабатывается, **Then** имя валидируется **двумя слоями**: (а) regex-санитизация `^[A-Za-z0-9_]+$` (отсекает инъекцию/спецсимволы) И (б) проверка существования против реальных объектов БД (`information_schema`); несуществующая таблица → понятная ошибка not-found (со списком известных), идентификатор в SQL **квотируется** (`"name"` с удвоением `"`). Без инъекции через имя. _[edge-case: несуществующая таблица / инъекция через идентификатор]_
5. **Given** `--export` с путём вне storage (абсолютный / `..`-traversal / разделители каталогов), **When** он резолвится, **Then** путь принудительно под `data/results/`: резолвится как `(results_dir / filename).resolve()` + проверка `is_relative_to(results_dir)`; выход за пределы → **отказ** (понятная ошибка, файл не пишется). _[edge-case: запись вне storage]_
6. **Given** `--export` в существующий файл или с неизвестным расширением (вне {csv,parquet,json}), **When** он выполняется, **Then** расширение **валидируется** (неизвестное → отказ, НЕ молчаливое до-приписывание `.csv` как в directaiq) И существующий файл **не перезаписывается молча** (отказ с предложением другого имени). Авто-экспорт (AC #2) использует таймстамп-имя → коллизия исключена. _[edge-case: клоббер / неверный формат экспорта]_
7. **Given** каталоги `data/results/` или `data/mcp_output/` отсутствуют, **When** пишется экспорт/аудит, **Then** они создаются на месте записи (`mkdir(parents=True, exist_ok=True)`). Каталоги — под `GDAU_DATA_ROOT` (хранилище, не dev-репо), резолверы `paths.py` остаются **чистыми** (без `mkdir`), `mkdir` делает вызывающий код. _[edge-case: отсутствующие каталоги вывода]_
8. **Given** результат ровно на пороге (500 строк), **When** решается про авто-экспорт, **Then** граница строго `> AUTO_EXPORT_THRESHOLD` (500 строк → отдаётся inline; 501 → авто-экспорт), без off-by-one. _[edge-case: граница порога]_
9. **Given** запись audit-лога падает (нет каталога/диск полон/сериализация), **When** обрабатывается вызов, **Then** это **WARNING** в `logging` (не `except: pass` directaiq, не сырой проброс) — сам read-запрос не валится из-за логирования, результат агенту возвращается. _[edge-case: сбой аудита не валит чтение]_
10. **Given** `--sample TABLE` с `N` ≤0 или без `N`, **When** команда выполняется, **Then** `N` ≥1: дефолт `DEFAULT_SAMPLE = 5` при отсутствии, клампинг `max(1, N)` при ≤0 (отличие от directaiq: `0`.isdigit()=True → `LIMIT 0` пустой результат — у нас клампится). _[edge-case: невалидный N]_

---

## Главные риски / решения (читать ДО кода)

> Эта история — **наращивание сервисного слоя поверх тонкого read-канала 3.1**. 3.1 принёс `gdau_mcp_server.py` + `tools/core.py` с `handle_query`→`execute_query` (read-only conn + форматтеры + statement-guard + clamp + таймаут + retry), но **только путь произвольного SQL** (без роутинга спец-команд). 3.2 добавляет: роутинг `--tables`/`--schema`/`--sample`/`--export` в `handle_query`, авто-экспорт >500 в `execute_query`, `_export_query`, audit-лог в обёртке инструмента, два path-резолвера в `paths.py`. Вендоринг directaiq: `_export_query`/`_validate_table_name`/`_handle_schema`/роутинг + `_save_audit_log`/`get_mcp_output_dir` — но **БЕЗ** `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/regex-fallback (Direct/НДС → 3.3), `--context`/goal-плейсхолдеров/`config_manager` (3.3).

### ⚠️ ЗАВИСИМОСТЬ ОТ 3.1 (читать первым) — 3.1 УЖЕ реализована в дереве (untracked, ветка `story/3.1-mcp-server`)

3.2 **расширяет файлы 3.1**, и они **уже реализованы**: 3.1 доведена dev-story (все 11 AC, pytest 363 passed), переведена в `review` и ждёт code-review+merge на ветке `story/3.1-mcp-server` (файлы ещё **не закоммичены** — `git status` показывает `??`): `scripts/mcp/tools/core.py` (368 строк), `scripts/mcp/gdau_mcp_server.py`, `scripts/mcp/tools/__init__.py`, `tests/test_mcp_core.py`, `tests/test_gdau_mcp_server.py`, `docs/mcp-query.md`, `.mcp.json`.

- **НЕ писать 3.1 заново** — она реализована. Перед 3.2: (а) дождаться code-review+merge 3.1 в `main` (3-1 → `done`), (б) ветку 3.2 ответвить от обновлённого `main`, (в) **прочитать фактический код** `core.py`/`gdau_mcp_server.py` — контракт ниже сверен **с фактической реализацией** (см. блок «Фактический контракт 3.1»), не с планом.
- **Фактический контракт 3.1 (сверено с `scripts/mcp/tools/core.py`):**
  - `execute_query(query, output_format="json", limit=DEFAULT_LIMIT) -> str`: guard `_reject_if_not_readonly(query)` **до** соединения → `_clamp_limit` → нормализация формата → `with DatabaseManager.connection(read_only=True) as conn:` → `rows = _execute_with_timeout(conn, query, STATEMENT_TIMEOUT_S)` (НЕ голый `fetchall`) → `except duckdb.IOException` однократный retry → `conn.description`-гард → форматтер. **Все ошибки ловятся ВНУТРИ и возвращаются строкой** (отличие от плана!): `except duckdb.InterruptException` (таймаут), **`except RuntimeError → str(exc)`** (AC #8 «нет данных» — глушится здесь, не пробрасывается), `except duckdb.Error → _format_sql_error`, `except Exception → "**Error:** …"`.
  - `handle_query(query, ...)`: `cleaned = (query or "").strip()`; пустой → подсказка; иначе `execute_query(cleaned, ...)`. **Роутинг 3.2 матчить по `cleaned`** (3.1 уже сделал strip — второй раз не делать).
  - Константы блоком: `DEFAULT_LIMIT=100`, `MAX_LIMIT=10_000`, `STATEMENT_TIMEOUT_S=30.0`, `_RETRY_SLEEP_S=0.1`, `_SUPPORTED_FORMATS`, `_READ_ONLY_LEADING_KEYWORDS`. `__all__ = ["DEFAULT_LIMIT","MAX_LIMIT","execute_query","handle_query"]`.
  - `_format_sql_error` 3.1 **НЕ** упоминает `--tables`/`--schema` (их не было); при «does not exist» подсказывает view'ы `visits`/`hits`.
  - `gdau_mcp_server.py`: `FastMCP("gdau_mcp")`, `ToolAnnotations(readOnlyHint=True, …)`, обёртка `duckdb_query` → `return handle_query(...)` (без аудита).
- **Что 3.2 МЕНЯЕТ в коде 3.1 (регрессия — не сломать существующие тесты 3.1):**
  - `handle_query`: + роутинг спец-команд по `cleaned` ПЕРЕД финальным `return execute_query(cleaned, …)`. (Теста «`--tables` уходит как SQL» в 3.1 **нет** — заменять нечего, добавить новые тесты роутинга.)
  - `execute_query`: + ветка авто-экспорта `>AUTO_EXPORT_THRESHOLD` **после** `rows`/`columns` (после retry-ветки и `conn.description`-гарда, перед dispatch форматтера), **переиспользуя уже открытый `conn`** для `COPY` (см. риск №2 — не открывать второе соединение). Не сломать таймаут/retry/clamp.
  - `duckdb_query` (обёртка): + `_save_audit_log(...)` после `handle_query`; **перевернуть `readOnlyHint=True → False`** (канал пишет файлы экспорта — как directaiq; `destructiveHint=False` остаётся). Обновить docstring/`Field`: рекламировать `--tables`/`--schema`/`--sample`/`--export`; **НЕ** `--context` (3.3)/Direct/НДС/goal.

### Риск №1 — statement-guard 3.1 запрещает `COPY … TO`, а экспорт им пишет (критично, развязка скоупа)

3.1 `_reject_if_not_readonly` **по дизайну отклоняет `COPY`** (allowlist ведущего слова: `SELECT/WITH/FROM/DESCRIBE/EXPLAIN/SHOW/VALUES/SUMMARIZE/TABLE/PIVOT/UNPIVOT`) — потому что под `read_only=True` `COPY … TO` **реально пишет файл** (эмпирика 3.1). Но 3.2 `--export`/авто-экспорт **обязаны** писать файл через `COPY (SELECT…) TO 'path'`. **Развязка:** `COPY` строит **сам сервер** с **контролируемым путём** под `data/results/` — это НЕ сырой ввод агента через guard. Дисциплина:
- **Внутренний SQL экспорта — read-only**: перед оборачиванием в `COPY` прогнать пользовательский SELECT через `_reject_if_not_readonly(export_sql)` (защита: `--export "DROP TABLE visits" x.csv` → отказ guard'ом ДО построения COPY). directaiq этого НЕ делал — у нас обязательно (defense-in-depth).
- **`COPY`-обёртку guard НЕ проходит** (её строит сервер, путь — наш, валидированный AC #5/#6). Запись идёт только в `data/results/`, `.writer.lock` не берётся, `gdau.duckdb` не мутируется (пишется отдельный файл-результат).
- Итог: канал «read-only к БД» + «пишет файлы-результаты в storage» — ровно поэтому `readOnlyHint=False` (как directaiq).

### Риск №2 — авто-экспорт: переиспользовать открытый `conn`, не открывать второе соединение

Факт. `execute_query` (3.1) держит **открытый read-only `conn`** внутри `with` и уже получил `rows`/`columns`. directaiq при `len(result) > 500` звал `_export_query`, который **открывал ВТОРОЕ соединение** к тому же файлу и гонял `COPY (query) TO` → двойной прогон запроса + вложенный коннект. Для read-only-аналитики это корректно (множественные read-only-коннекты разрешены), но избыточно.
- **✅ Рекомендация:** вынести COPY в общий хелпер `_run_copy_export(conn, sql, output_path, ext) -> str` (строит `COPY (sql) TO 'safe' (…)`, считает строки, возвращает текст). **Авто-экспорт** зовёт его с **уже открытым `conn`** (без второго соединения/двойного fetch). **`--export`** (из роутинга `handle_query`, своего conn нет) → `_export_query` открывает свой read-only conn и зовёт тот же хелпер. Так путь записи файла — один, тестируется один раз.
- **Точка вставки авто-экспорта в `execute_query`:** ПОСЛЕ retry-ветки и `conn.description`-гарда (где уже есть `rows`/`columns`), ПЕРЕД dispatch форматтера — `if len(rows) > AUTO_EXPORT_THRESHOLD: return «Результат велик (N строк). » + _run_copy_export(conn, query, auto_path, "csv")`. Не сломать таймаут/retry/`_clamp_limit`/обработку `RuntimeError`/`InterruptException` (она оборачивает весь блок).
- Альтернатива (directaiq-форма, второе соединение) допустима, но менее экономна — если выберешь, закрепить тестом идентичность результата.

### Риск №3 — path-резолверы остаются чистыми, mkdir на месте записи (AC #7)

`paths.py` — инвариант «все `get_*` чистые, **никогда не `mkdir`**» (data в dev-репо не пишется; mkdir — забота вызывающего). Добавить **чистые** резолверы:
- `get_results_dir() -> Path` → `{root}/data/results` (без mkdir).
- `get_mcp_output_dir() -> Path` → `{root}/data/mcp_output` (без mkdir; **отличие от directaiq**, где `get_mcp_output_dir` делал mkdir — у нас mkdir в `_save_audit_log`/`_export_query`).
- Оба — под уже-валидированным `get_storage_root()` (fail-loud при битом `GDAU_DATA_ROOT`). Добавить в `__all__`. `mkdir(parents=True, exist_ok=True)` — в `_export_query` и `_save_audit_log` (это storage, не dev-репо → запись легальна). Каталоги `data/` уже в `.gitignore` (артефакты не коммитятся).

### Риск №4 — `--export` безопасность пути/клоббер/расширение (AC #5/#6, ужесточение vs directaiq)

directaiq `_export_query`: добавляет `.csv` если расширение неизвестно (молчит), резолвит под `output_dir` + `is_relative_to`, **молча перезаписывает через `COPY TO`**. Наши AC #5/#6 строже:
- **Расширение:** только `{.csv, .parquet, .json}`; иное → **отказ** (не до-приписывать `.csv`).
- **Traversal/abs:** `output_path = (get_results_dir() / filename).resolve()`; если `not output_path.is_relative_to(get_results_dir().resolve())` → отказ. (Абсолютный `filename` или `../..` уводит за пределы → ловится. `is_relative_to` — Python 3.9+, у нас 3.13.)
- **Клоббер:** `output_path.exists()` → **отказ** («файл уже есть, выбери другое имя») — НЕ молчаливый `COPY TO` поверх. (AC допускает «отказ или уникальное имя»; отказ проще/безопаснее.) Авто-экспорт (серверное таймстамп-имя `auto_export_{YYYYMMDD_HHMMSS}.csv`) коллизий не даёт; на всякий — если существует, добавить счётчик/микросекунды.
- **Формат `COPY`:** `.parquet` → `(FORMAT PARQUET)`; `.json` → `(FORMAT JSON, ARRAY true)`; иначе `(HEADER, DELIMITER ',')` (CSV). Путь экранировать `'` → `''` в литерале.

### Риск №5 — авто-экспорт vs clamp-лимита 3.1 (РЕШЕНИЕ ЗАФИКСИРОВАНО: вариант A)

3.1 осознанно сменил контракт лимита на `[1, MAX_LIMIT]` (clamp, чтобы тонкий срез без авто-экспорта не заливал ответ) и пометил: «когда появится авто-экспорт (3.2), дефолт лимита можно вернуть к 0=без лимита». Теперь авто-экспорт есть — что делать с лимитом?
- **✅ Зафиксировано A (Шеф делегировал «делай как считаешь с учётом специфики проекта», 2026-05-25):** **оставить** `_clamp_limit [1, MAX_LIMIT]` 3.1 как **дисплей-потолок** + добавить авто-экспорт `>500` как **отдельный страж переполнения**. Порог считается по **`len(rows)`** (полный fetch), не по `display_limit` — они ортогональны. `DEFAULT_LIMIT`/`MAX_LIMIT`/`_clamp_limit` 3.1 **не трогать**.
- **Осознанная граница 101–500 строк (зафиксировать в AC #2/тесте, чтобы не удивляло):** при дефолтном `limit=100` результат 101–500 строк → **усекается до 100 inline** с `has_more=true`/`next_offset` (НЕ авто-экспортируется: `len(rows) ≤ 500`). Агент при нужде поднимает `limit` (до `MAX_LIMIT`) или зовёт `--export`. Авто-экспорт — страж только для `>500`, не замена clamp. (>500 строк уходят в файл целиком, без усечения.)
- **Почему A, не B (вернуть directaiq `limit=0 = без лимита`):** B ближе к букве FR-17, но **меняет только-что сданный контракт 3.1 в review** (`_clamp_limit`, `ge=0`-поле pydantic, тесты лимита) — churn **без выигрыша в надёжности** в модели «один оператор» (переполнение бьёт только со стороны вывода, и его уже закрывают clamp+авто-экспорт). Принцип проекта «не усложнять без реальной потребности» → A.

### Риск №6 — `--schema TABLE` БЕЗ семантики денег (граница 3.2↔3.3)

directaiq `_handle_schema` аннотирует денежные колонки через `_annotate_money_column`/`_COST_COLUMN_SEMANTICS` (Direct/НДС) и строит `CASE … semantics`. **Это 3.3** (семантика из нашего каталога FR-16). В 3.2 `--schema TABLE` отдаёт **только** `column_name, data_type` из `information_schema` (без колонки semantics, без `_annotate_money_column`, без `_COST_COLUMN_SEMANTICS`/`_GENERIC_MONEY_COL_RE`). 3.3 обогатит семантикой каталога. Не тащить Direct/НДС-аннотации в 3.2 — у нас их просто нет (геймдев, не Директ).

### Риск №7 — `_save_audit_log` не валит чтение (AC #9, ужесточение vs directaiq)

directaiq `_save_audit_log` оборачивает всё в `try/except Exception: pass` (молча). Наш AC #9 — **WARNING**, не молчание: `except Exception as e: logger.warning("Не удалось записать audit-лог: %s", e)`. Результат вычислен до аудита → его возврат от сбоя лога не зависит. mkdir каталога `data/mcp_output/` — внутри той же try (нет каталога → WARNING, не падение).

### Риск №8 — мусорные dev-репо артефакты при тестах

Как 3.1/`test_views.py`/`test_database_manager.py`: тесты против `tmp_path` + `monkeypatch.setenv(DATA_ROOT_ENV, …)`. Файлы экспорта/аудита идут в `{tmp}/data/results/` и `{tmp}/data/mcp_output/` (под tmp-root), НЕ в dev-репо. `gdau.duckdb` создаётся write-conn'ом/`views.create_views` в фикстуре; MCP читает read-only. `.env`/`*.parquet`/`*.duckdb` в dev-репо не создавать.

---

## Tasks / Subtasks

- [x] **Task 0 — Предусловие: 3.1 реализована и влита; прочитать её код**
  - [x] Убедиться, что 3.1 (`ready-for-dev`) реализована и в `main`/базовой ветке (иначе расширять нечего). Если нет — сначала 3.1.
  - [x] Прочитать фактический `scripts/mcp/tools/core.py` + `scripts/mcp/gdau_mcp_server.py` из 3.1: сигнатуры/имена констант/форму `_reject_if_not_readonly`/`_clamp_limit`/`_format_sql_error`/форматтеров. Контракт ниже сверять с фактом.
- [x] **Task 1 — Path-резолверы `scripts/utils/paths.py` (UPDATE; AC #7)**
  - [x] `get_results_dir() -> Path` → `get_storage_root() / "data" / "results"` (чистая, без mkdir; русский докстринг как у соседей).
  - [x] `get_mcp_output_dir() -> Path` → `get_storage_root() / "data" / "mcp_output"` (чистая, без mkdir — **отличие от directaiq**, где был mkdir; зафиксировать «почему» комментарием: инвариант чистых резолверов).
  - [x] Добавить оба в `__all__`.
- [x] **Task 2 — Сервисный слой `scripts/mcp/tools/core.py` (UPDATE; AC #1/#2/#4/#5/#6/#8/#10)**
  - [x] Обновить шапку-пометку вендоринга: добавить, что в 3.2 принесены `_export_query`/`_validate_table_name`/`_handle_schema`(plain)/роутинг спец-команд + авто-экспорт; `trimmed:` всё ещё без `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/`--context`/placeholders/`config_manager` → 3.3.
  - [x] **`_validate_table_name(name) -> str | None`** (вендоринг verbatim): `strip` + regex `^[A-Za-z0-9_]+$` → имя или `None`. Константа `_VALID_TABLE_NAME`.
  - [x] **Существование таблицы (AC #4, новое поверх directaiq):** хелпер открывает своё read-only соединение, читает реальные объекты (`SELECT table_name FROM information_schema.tables WHERE table_schema='main'`); искомое имя не в наборе → понятная ошибка not-found **со списком известных** (из того же запроса). Идентификатор в SQL — квотировать `"name"` с удвоением `"`. _(Двойное обращение к БД — existence-check + сам запрос — приемлемо для «одного оператора»; не усложнять переиспользованием conn.)_
  - [x] **`_run_copy_export(conn, sql, output_path, ext) -> str`** (новый общий хелпер, риск №2): `COPY ({sql}) TO '{safe}' (…)` по `ext` (`.parquet`→`(FORMAT PARQUET)`; `.json`→`(FORMAT JSON, ARRAY true)`; иначе `(HEADER, DELIMITER ',')`); путь экранировать `'`→`''`; `SELECT COUNT(*) FROM '{safe}'` (guard None у `fetchone()`) → `«Экспортировано N строк в `path`»`. Зовётся обоими путями (авто-экспорт с открытым conn, `--export` со своим).
  - [x] **`_export_query(sql, filename) -> str`** (адаптировать directaiq, риск №1/№4):
    - [x] guard внутреннего SQL: `_reject_if_not_readonly(sql)` → если отказ, вернуть его (не строить COPY).
    - [x] валидация расширения (∈ {csv,parquet,json}, иначе отказ — НЕ до-приписывать `.csv`); резолюция `(get_results_dir() / filename).resolve()` + `is_relative_to(get_results_dir().resolve())` (AC #5); `output_path.exists()` → отказ (AC #6); `get_results_dir().mkdir(parents=True, exist_ok=True)` (AC #7).
    - [x] `with DatabaseManager.connection(read_only=True) as conn:` → `_run_copy_export(conn, sql, output_path, ext)`. Ошибки: **`except RuntimeError → str(exc)`** (паритет с `execute_query` — до-данных→дружелюбный AC #8, а не «**Error:** RuntimeError»), `except duckdb.Error → _format_sql_error`, `except Exception → строка` (наружу не выпускать).
  - [x] **`execute_query` (UPDATE; AC #2/#8):** **после** retry-ветки и `conn.description`-гарда (где `rows`/`columns` уже есть), **перед** dispatch форматтера — `if len(rows) > AUTO_EXPORT_THRESHOLD:` → таймстамп-имя `auto_export_{ts}.csv` под `get_results_dir()` (mkdir; коллизия → счётчик/микросекунды) → `return «Результат велик (N строк). » + _run_copy_export(conn, query, auto_path, ".csv")` (**переиспользовать открытый `conn`**, риск №2). Константа `AUTO_EXPORT_THRESHOLD = 500` в блок констант 3.1; граница строго `>` по `len(rows)` (AC #8). **Не сломать** существующие `except InterruptException`/`RuntimeError`/`duckdb.Error`/`Exception` (они оборачивают весь `with`) и `_clamp_limit`.
  - [x] **`_handle_schema(table_name, output_format, limit) -> str`** (plain, БЕЗ семантики — риск №6): проверить существование (AC #4); `SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{name}' ORDER BY ordinal_position` — **квотированный литерал** (`execute_query` принимает только строку SQL, bind-параметр `?` не пробрасывается; имя уже прошло `_validate_table_name` `^[A-Za-z0-9_]+$` + удвоение `'` → инъекция невозможна) → `execute_query(sql, output_format, limit)` (один источник форматирования). **НЕ** строить `CASE semantics`/`_annotate_money_column`.
  - [x] **`handle_query` (UPDATE; AC #1):** факт. функция делает `cleaned = (query or "").strip()`; пустой→подсказка; иначе `execute_query(cleaned, …)`. Добавить роутинг **по `cleaned`** (не делать strip второй раз) ПЕРЕД финальным `return execute_query(cleaned, …)`:
    - [x] `cleaned == "--tables"` → `SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name` → `execute_query`.
    - [x] `cleaned == "--schema"` (ровно) → схема всех (`information_schema.columns`, schema='main') → `execute_query`.
    - [x] `cleaned.startswith("--schema ")` → `_validate_table_name(остаток)`; невалид → ошибка; иначе `_handle_schema`.
    - [x] `cleaned.startswith("--sample ")` → split; `_validate_table_name(parts[1])`; невалид → ошибка; проверить существование (AC #4); `N`: отсутствует → `DEFAULT_SAMPLE=5`, есть и `.isdigit()` → `max(1, int)` (AC #10), иначе дефолт; `SELECT * FROM "{name}" LIMIT {N}` (квотировать имя `"`-удвоением) → `execute_query`.
    - [x] `cleaned.startswith("--export ")` → `shlex.split(остаток)` (кросс-платформенно; `posix=True` по умолчанию обрабатывает кавычки); `len>=2` → `_export_query(parts[0], parts[1])`; иначе usage-ошибка; `except ValueError` shlex → ошибка.
    - [x] **НЕ** добавлять `--context` (3.3). Fall-through (любой иной текст) → `execute_query(cleaned, …)` как раньше.
    - [x] Константы `DEFAULT_SAMPLE = 5`, `AUTO_EXPORT_THRESHOLD = 500` — в блок констант 3.1 (рядом с `DEFAULT_LIMIT`/`MAX_LIMIT`; в `__all__` не обязательны — тесты обращаются через `core.X`/monkeypatch, как к `STATEMENT_TIMEOUT_S`).
- [x] **Task 3 — Audit-лог `scripts/mcp/gdau_mcp_server.py` (UPDATE; AC #3/#9)**
  - [x] **`_save_audit_log(tool_name, parameters, result) -> None`** (адаптировать directaiq, риск №7): `mcp_dir = get_mcp_output_dir(); mcp_dir.mkdir(parents=True, exist_ok=True)` (из `scripts.utils.paths`, НЕ из `common.py`); имя `{tool}_{YYYY-MM-DD_HHMMSS}.json`; конверт `{tool, timestamp(isoformat), parameters, result}` (result: попытка `json.loads`, иначе строка); `json.dump(..., ensure_ascii=False, indent=2, default=str)`. **`except Exception as e: logger.warning(...)`** (НЕ `pass`).
  - [x] **`duckdb_query` (UPDATE):** после `result = handle_query(query, format, limit)` → `_save_audit_log("duckdb_query", {"query":query,"format":format,"limit":limit}, result)` → `return result`.
  - [x] **Перевернуть `ToolAnnotations(readOnlyHint=False, …)`** (3.1 ставил True; теперь экспорт пишет файлы — как directaiq; `destructiveHint=False`, `idempotentHint=True`, `openWorldHint=False`).
  - [x] **Docstring/`Field`-описание инструмента:** рекламировать `--tables`/`--schema [TABLE]`/`--sample TABLE [N]`/`--export "SQL" file.{csv|parquet|json}` (теперь существуют) + авто-экспорт >500 + что результаты/аудит идут в `data/results/`/`data/mcp_output/`. **НЕ** упоминать `--context` (3.3), Direct/НДС/goal-плейсхолдеры/`t10_*`/`t18_*`.
  - [x] Импорт `get_mcp_output_dir` из `scripts.utils.paths` (НЕ заводить `scripts/mcp/utils/common.py` — это `config_manager`-шов, его в репо нет).
- [x] **Task 4 — Спека `docs/mcp-query.md` (UPDATE; часть DoD)**
  - [x] Дополнить (3 вопроса project-context): **что делает** — сервисные команды навигации + безопасный вывод больших результатов; **зачем** — ориентироваться без ручного перебора, не переполнять ответ; **контракт** — `--tables`/`--schema`/`--sample` (read), `--export`/авто-экспорт пишут **только** в `data/results/` (traversal/клоббер/расширение защищены), audit каждого вызова в `data/mcp_output/`, лок писателя по-прежнему не берётся. Отметить, что `--context`/семантика каталога — следующая история (3.3).
- [x] **Task 5 — Тесты (UPDATE существующих 3.1 + новые кейсы)**
  - [x] **`tests/test_mcp_core.py`** (расширить фикстуру 3.1: tmp-`gdau.duckdb` с view'ами `visits`/`hits` через `views.create_views` поверх tmp-партиции):
    - [x] **AC #1:** `--tables` → есть `visits`/`hits`; `--schema` → колонки всех; `--schema visits` → колонки visits (без колонки semantics — риск №6); `--sample visits 3` → ≤3 строки.
    - [x] **AC #4:** `--schema nonexist`/`--sample nonexist` → not-found (не сырой DuckDB-err); `--schema "visits; DROP …"` / спецсимволы → отклонено `_validate_table_name`; имя квотируется (таблица с `_`/смешанным регистром читается).
    - [x] **AC #2/#8:** результат 500 строк → inline (формат), 501 → авто-экспорт (сообщение + файл в `{tmp}/data/results/` существует, число строк верное). Граница `>` (off-by-one закреплён).
    - [x] **AC #5:** `--export "SELECT 1" ../evil.csv` и абсолютный путь → отказ, файл вне `data/results/` НЕ создан.
    - [x] **AC #6:** неизвестное расширение (`x.txt`) → отказ (НЕ создан `x.txt.csv`); экспорт в существующий файл → отказ (исходный не перезаписан). `.parquet`/`.json`/`.csv` → файл создан с верным форматом.
    - [x] **Риск №1:** `--export "DROP TABLE visits" x.csv` → отказ guard'ом (visits цела, файл не создан).
    - [x] **AC #7:** при отсутствии `data/results/` экспорт его создаёт.
    - [x] **AC #8 (до данных):** хранилище без `gdau.duckdb` → `--export "SELECT 1" x.csv` (и `--schema`/`--sample`/`--tables`) → дружелюбная подсказка про `gdau-logs update` (не `**Error:** RuntimeError`). Грунт под `except RuntimeError` в `_export_query`.
    - [x] **AC #10:** `--sample visits` (без N) → 5 строк; `--sample visits 0` / `-3` → ≥1 (клампинг/дефолт).
    - [x] **AC #2 граница 101–500 (риск №5):** результат 300 строк при дефолтном limit=100 → inline усечён до 100 с `has_more=true`, файл НЕ создан (НЕ авто-экспорт).
    - [x] **Регресс 3.1:** теста «`--tables` уходит как SQL» в 3.1 **нет** — заменять нечего; добавить новые тесты роутинга. Существующие тесты 3.1 (happy-path/guard/clamp/timeout/retry/до-данных) — оставить зелёными (вставка роутинга/авто-экспорта их не ломает).
  - [x] **`tests/test_gdau_mcp_server.py`** (расширить):
    - [x] **AC #3:** вызов `duckdb_query` пишет JSON-конверт в `{tmp}/data/mcp_output/` (поля `tool/timestamp/parameters/result`).
    - [x] **AC #9:** monkeypatch `get_mcp_output_dir`/`mkdir`/`open` бросить → `duckdb_query` всё равно возвращает результат, аудит-сбой → WARNING (не исключение). Проверить, что результат идентичен запросу без сбоя аудита.
    - [x] **readOnlyHint=False** в `ToolAnnotations` (перевёрнут vs 3.1).
    - [x] **Анти-зависимость (как 3.1, по import-узлам ast):** в `scripts/mcp/**` нет импорта `config_manager`/`auth_manager`/`directaiq`/`scripts.mcp.utils.common`; audit берёт `get_mcp_output_dir` из `scripts.utils.paths`.
  - [x] **`tests/test_paths.py`** (если есть — расширить; AC #7): `get_results_dir()`/`get_mcp_output_dir()` дают `{root}/data/results`/`{root}/data/mcp_output`, **не делают mkdir** (каталога нет после вызова), fail-loud при битом `GDAU_DATA_ROOT` (наследуется из `get_storage_root`).
- [x] **Гейты перед сдачей**
  - [x] `uv run mypy scripts` → зелено (`strict=true`; новые функции типизированы, без `Any`-дыр; `fetchone()`/`fetchall()` → guard None; матрица CI ubuntu+windows, локально доп. `--platform linux`).
  - [x] `uv run pytest` (offline) → зелено; маркер `live` не вводится (MCP-чтение в Logs API не ходит).
  - [x] `uv.lock`/`pyproject.toml` не менялись (`mcp`/`pydantic`/`duckdb`/`python-dotenv` уже есть; `shlex`/`json`/`datetime`/`re` — stdlib).
  - [x] Чек-лист «Definition of Done» пройден; `docs/mcp-query.md` обновлён (компонент без актуальной спеки не «готов»).

## Dev Notes

### Рекомендуемый контракт 3.2 (поверх 3.1; semantics/`--context` — 3.3)

| Имя | Сигнатура | Смысл | Где |
|---|---|---|---|
| `handle_query` | `(query, output_format="json", limit=…) -> str` | **UPDATE 3.1**: + роутинг `--tables`/`--schema`/`--sample`/`--export` перед `execute_query` | `tools/core.py` |
| `execute_query` | `(query, output_format="json", limit=…) -> str` | **UPDATE 3.1**: + ветка авто-экспорта `>500` между fetch и формат | `tools/core.py` |
| `_export_query` | `(sql, filename) -> str` | guard внутреннего SQL → расширение/traversal/клоббер → mkdir → свой conn → `_run_copy_export`; `except RuntimeError→str` (AC #8) | `tools/core.py` (новое в 3.2) |
| `_run_copy_export` | `(conn, sql, output_path, ext) -> str` | общий COPY-хелпер (авто-экспорт переиспользует открытый conn, `--export` — свой); риск №2 | `tools/core.py` (новое в 3.2) |
| `_validate_table_name` | `(name) -> str \| None` | regex `^[A-Za-z0-9_]+$` (вендоринг verbatim) | `tools/core.py` (новое в 3.2) |
| `_handle_schema` | `(table, fmt, limit) -> str` | колонки/типы одной таблицы (БЕЗ семантики — 3.3) | `tools/core.py` (новое в 3.2) |
| `_save_audit_log` | `(tool, params, result) -> None` | JSON-конверт в `data/mcp_output/`; сбой → WARNING | `gdau_mcp_server.py` (новое в 3.2) |
| `get_results_dir` | `() -> Path` | `{root}/data/results` (чистая, без mkdir) | `paths.py` (новое в 3.2) |
| `get_mcp_output_dir` | `() -> Path` | `{root}/data/mcp_output` (чистая, без mkdir) | `paths.py` (новое в 3.2) |

**Карта примитивов, которые зовём:**
- `DatabaseManager.connection(read_only=True)` (`database_manager.py:39`) — для `--export`/авто-экспорта (`COPY … TO` под read-only — пишет файл-результат, эмпирика 3.1), `--schema`/`--sample`/`--tables` (чтение `information_schema`). Лок не берётся (FR-15/2.5).
- `views.create_views(conn, …)` (`views.py:117`) — только тестовая фикстура (создать `visits`/`hits` поверх tmp-партиции). В проде view'ы заводит init 4.3 / `ingest_range` 2.7.
- `paths.get_results_dir()`/`paths.get_mcp_output_dir()` (новое 3.2) — пути экспорта/аудита; fail-loud при битом `GDAU_DATA_ROOT` (через `get_storage_root`).
- **3.1 (обязательно переиспользовать, не дублировать):** `_reject_if_not_readonly` (guard внутреннего SQL экспорта), `_clamp_limit`, `_format_sql_error`, `format_result_{json,markdown,csv}`.
- **НЕ зовём:** `read_metrica_credentials`/`MetricaClient`/`p81`/`parquet_store`/`load_state`/`writer_lock` (путь записи Epic 1/2); `config_manager`/`scripts.mcp.utils.common` (нет в репо).

### Что НЕ вендорим в 3.2 (приходит в 3.3) — чтобы dev не притащил по инерции

| Из directaiq `core.py`/server | Куда | Почему не в 3.2 |
|---|---|---|
| `--context` / `_handle_context` | **3.3** | требует семантику каталога + row counts/date ranges/config-цели |
| `_COST_COLUMN_SEMANTICS` / `_annotate_money_column` / `_GENERIC_MONEY_COL_RE` | **3.3** | Direct/НДС-семантика → заменяется семантикой каталога FR-16 |
| `process_sql_placeholders` / `get_config` (`{{DATE_30D}}`/`{{PRIMARY_GOAL_ID}}`/…) | **3.3** | goal-плейсхолдеры + завязка на `config_manager` (нет в репо → ImportError) |
| `scripts/mcp/utils/common.py` (`get_config`/`get_mcp_output_dir` directaiq) | **не вендорим** | `get_config`=config_manager (3.3); `get_mcp_output_dir` берём из нашего `paths.py` |
| семантика-аннотации в `_handle_schema` (`CASE … semantics`) | **3.3** | `--schema TABLE` в 3.2 = plain колонки/типы |

### Паттерны (соблюдать — снижают цикл ревью)
- `from __future__ import annotations` первой строкой; русские docstrings/комментарии (модульный обязателен), английские идентификаторы; type hints везде, `mypy --strict`, без `Any`-дыр; абсолютные импорты от корня; `logger = logging.getLogger(__name__)` (диагностика — `logging`, не `print`; особенно WARNING аудита).
- **Вендоренный код — с обновлённой шапкой-пометкой** «vendored from directaiq @ <ref>, seam: …, trimmed: …» (вендоренное держим сравнимым с источником; развязка только в обозначенных швах: guard внутреннего SQL экспорта + path-резолверы из нашего `paths.py` + WARNING-аудит вместо `pass`).
- **Комментарии «почему», не «что»** — особенно у развязки `COPY` мимо guard'а (риск №1: путь строит сервер, не агент), у `readOnlyHint=False` (канал пишет файлы-результаты), у чистых path-резолверов без mkdir (риск №3).
- **Read-only к БД — инвариант** (project-context «CLI=запись, MCP=только чтение»): `gdau.duckdb` не мутируется, `.writer.lock` не берётся; экспорт пишет **отдельный файл-результат** в `data/results/`, не в БД.
- **Не тащить** инфру directaiq (`config_manager`/`auth_manager`) и тяжёлые зависимости. Новых зависимостей не добавлять.

### Границы 3.2 (не выходить)
- **Трогаем (UPDATE):** `scripts/mcp/tools/core.py`, `scripts/mcp/gdau_mcp_server.py`, `scripts/utils/paths.py`, `docs/mcp-query.md`, `tests/test_mcp_core.py`, `tests/test_gdau_mcp_server.py` (+ `tests/test_paths.py` если есть). `.mcp.json` — **не трогаем** (из 3.1, форма не меняется).
- **Не** реализуем: `--context`/семантику каталога/снятие `config_manager`-швов/goal-плейсхолдеров/regex-fallback/Direct-НДС (3.3 — их в принесённом коде нет).
- **Не** трогаем код Epic 1/2 (клиент/оркестратор/запись/каталог/view'ы/лок) — только **читаем** через `DatabaseManager`/`views`/`information_schema`. `paths.py` — только аддитивно (два новых чистых резолвера, существующие не менять).

### Project Structure Notes
- Раскладка architecture.md:461-463: `scripts/mcp/gdau_mcp_server.py` + `scripts/mcp/tools/core.py`. Запуск MCP — через `.mcp.json` (`python -m scripts.mcp.gdau_mcp_server`), НЕ через `[project.scripts]`.
- **`data/results/` и `data/mcp_output/`** в схеме хранилища architecture.md:495-497 **не перечислены** (там только `data/raw/` + `data/duckdb/`), но явно заданы epics 3.2 (AC #2/#3) и PRD addendum:62/74 (directaiq: авто-экспорт→`data/results/`, audit→`data/mcp_output/`). 3.2 заводит их **on-demand** (`mkdir` при первой записи). `data/` уже в `.gitignore` (артефакты не коммитятся). init-шаблон (4.2) их предсоздавать не обязан — создаются на месте.
- Тесты — плоские `tests/test_<area>.py`; `conftest.py` нет (`tmp_path`/`monkeypatch` напрямую). Маркер `live` не для MCP-чтения.
- `gdau.duckdb`/`*.parquet`/`.env`/файлы экспорта/аудита — артефакты хранилища (`GDAU_DATA_ROOT`); в dev-репо не создаются/не коммитятся.
- Не переводить на src-layout, не переименовывать пакет `scripts` (hatchling `packages=["scripts"]`).

### Live-smoke / DoD
- **Live неприменим** (как 3.1/2.1/2.6): MCP-чтение и экспорт **не дёргают внешний Logs API** — читают локальный `gdau.duckdb`, пишут файл-результат локально. Мандат live-smoke (project-context) касается контракта внешнего API; здесь его нет. Достаточно offline против временного DuckDB.
- **Ручной smoke (опционально, не тест):** против `G:\gdau-smoke` ([[gdau-smoke-live-storage]]) поднять сервер и проверить `--tables`, `--schema visits`, `--sample visits 3`, `--export "SELECT * FROM visits LIMIT 10" v.csv` (файл в `data/results/`), и что вызовы пишут аудит в `data/mcp_output/`. Описать в `docs/mcp-query.md`.

### Эмпирические факты (DuckDB 1.5.3, из 3.1 — грунт под экспорт)
- Под `read_only=True` `COPY (SELECT…) TO 'file'` **успешно пишет файл** (именно поэтому экспорт работает через read-only conn). Запись в `gdau.duckdb` (`CREATE/INSERT/DROP`) при этом заблокирована (`InvalidInputException`) — экспорт пишет отдельный файл-результат, не мутирует БД.
- `_format_sql_error` 3.1 при «does not exist» подсказывает view'ы `visits`/`hits` (команд `--tables`/`--schema` в 3.1 не было). В 3.2 эти команды **появляются** → опционально вернуть в ветку does-not-exist подсказку «используй `--tables`/`--schema TABLE`». **Не блокер** — мелочь, согласовать тон.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story 3.2] (строки 395-412) — 10 AC: сервис-команды `--tables`/`--schema [TABLE]`/`--sample TABLE [N]`/`--export`; авто-экспорт >500 → `data/results/`; audit → `data/mcp_output/`; edge-cases (несуществующая таблица/инъекция, traversal, клоббер/формат, отсутствующие каталоги, граница порога, сбой аудита, невалидный N). [#Epic 3] (371-373) — раздел на 3.1/3.2/3.3; `--context`/семантика → 3.3.
- [Source: prd.md#FR-17] (строки 259-266) — единый `duckdb_query(query, format, limit)` + сервис-команды `--tables`/`--schema`/`--sample`/`--export`; авто-экспорт >500 (порог по умолчанию 500). [#FR-18] (268-275) — `--context`/семантика каталога/снятие Direct-НДС → **3.3** (не здесь). [#Один писатель] (234) — чтение лока не требует.
- [Source: prd addendum.md] (:62) — directaiq-поведение: спец-команды в `query`, авто-export >500 в `data/results/`, audit-лог каждого вызова в `data/mcp_output/`. (:66) — «лёгкая доработка» = семантика каталога + снятие Direct/goal (это 3.3, не 3.2). (:74) — `data/results/` как создаваемый каталог.
- [Source: architecture.md] — :49, :238 (единый инструмент + сервис-команды `--context/--tables/--schema/--sample/--export` сохраняются; лёгкая доработка); :461-463 (раскладка `scripts/mcp/`); :507 (`.mcp.json` → `python -m scripts.mcp.gdau_mcp_server`); :540 (поток: запрос → результат json/md/csv; большой → файл-экспорт); :495-497 (раскладка `data/` — `results/`/`mcp_output/` не перечислены, заводятся on-demand).
- [Source: _bmad-output/implementation-artifacts/3-1-вендоринг-mcp-сервера-и-инструмент-duckdb-query.md] — **база, на которую встаёт 3.2**: `handle_query`/`execute_query`/`_reject_if_not_readonly`/`_clamp_limit`/`_format_sql_error`/форматтеры; решение «вариант A — тонкий read-канал, сервис/контекст/семантика → 3.2/3.3»; `readOnlyHint=True` (3.2 переворачивает в False); контракт лимита `[1,MAX_LIMIT]` (риск №5).
- [Source: G:\git\directaiq\scripts\mcp\tools\core.py] — вендоринг: `_export_query` (286-323), `_validate_table_name` (60-65), `handle_query`-роутинг (526-583), `_handle_schema` (482-518, **без** semantics-части у нас), `AUTO_EXPORT_THRESHOLD`/авто-экспорт (219-246). **НЕ берём:** `process_sql_placeholders`/`_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/`_handle_context`.
- [Source: G:\git\directaiq\scripts\mcp\directaiq_mcp_server.py] — `_save_audit_log` (51-73, у нас `except → WARNING` вместо `pass`), вызов аудита в обёртке (145-147), `ToolAnnotations(readOnlyHint=False)` (81-90). [common.py] — `get_mcp_output_dir` (у нас в `paths.py`, без mkdir); `get_config` НЕ берём (config_manager → 3.3).
- [Source: scripts/utils/paths.py] — инвариант чистых резолверов без mkdir (:11-13, :60-62); `get_storage_root` fail-loud (:49-89); `__all__` (:40-46). Добавить `get_results_dir`/`get_mcp_output_dir`.
- [Source: scripts/utils/database_manager.py:39] — `DatabaseManager.connection(read_only=True)`; RuntimeError до создания БД, `finally`-close, лок не берёт.
- [Source: scripts/utils/views.py:117] — `create_views(conn, sources=VALID_SOURCES)` для тестовой фикстуры; view'ы `visits`/`hits`.
- [Source: _bmad-output/project-context.md] — каналы (MCP=только чтение), вендоринг с шапкой+развязка швов, не тащить `config_manager`, чистые path-резолверы без mkdir, docs/<component>.md как DoD, тесты по import-узлам не подстрокой.
- [Memory] [[mcp-env-delivery]] (Claude Code не грузит `.env`), [[dotenv-usecwd-gotcha]], [[gdau-smoke-live-storage]] (`G:\gdau-smoke` для ручного smoke), [[gdau-env-contract]] (`GDAU_DATA_ROOT`), [[parallel-epic3-epic4-worktrees]] (стык `.mcp.json`→4.3).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Amelia, dev-story workflow), 2026-05-26.

### Debug Log References

- **Task 0 (предусловие):** 3.1 уже влита в `main` (commit `3e5e4dc`), файлы `scripts/mcp/**` трекаются, дерево чистое, ветка `story/3.2-mcp-service-commands` ответвлена. Фактический контракт 3.1 (`execute_query`/`handle_query`/`_reject_if_not_readonly`/`_clamp_limit`/`_format_sql_error`/`_execute_with_timeout`/форматтеры/блок констант/`__all__`) сверен с реальным `core.py` — совпал с описанием в story.
- **DuckDB-smoke (де-риск перед тестами):** подтвердил эмпирически на DuckDB 1.5.3 — `COPY (…) TO` в `.csv`/`.parquet`/`.json` пишет файл; пере-чтение `SELECT COUNT(*) FROM '{file}'` корректно во всех трёх форматах; `range(501)` = 501 строка; `information_schema.tables/columns WHERE table_schema='main'` отдаёт view'ы visits/hits и их колонки. Это грунт под `_run_copy_export` и роутинг `--tables`/`--schema`.
- **`--sample N` клампинг:** выбран `.isdigit()`-вариант (как предписано Task 2/AC #10) — `'0'.isdigit()=True → max(1,0)=1`; отрицательное/нечисловое (`'-3'.isdigit()=False`) → `DEFAULT_SAMPLE`. Так `N` всегда ≥1 и нет риска `int('--5')`-ValueError.
- **Гейты:** `uv run mypy scripts` (strict) зелёный на win32 И `--platform linux` (23 файла); `uv run pytest` — 414 passed / 4 skipped / 8 live deselected (было 367 в 3.1, +47 тестов 3.2); `uv.lock`/`pyproject.toml` не менялись; data-артефактов в dev-репо нет.

### Completion Notes List

Реализовано поверх тонкого read-канала 3.1 (UPDATE существующих файлов, без нового пути записи):

- **Task 1 — `paths.py`:** добавлены чистые резолверы `get_results_dir()` → `{root}/data/results` и `get_mcp_output_dir()` → `{root}/data/mcp_output` (БЕЗ `mkdir` — инвариант, отличие от directaiq где `get_mcp_output_dir` делал mkdir); оба в `__all__`; fail-loud наследуется из `get_storage_root` (AC #7, риск №3).
- **Task 2 — `tools/core.py` (сервисный слой):** роутинг `--tables`/`--schema [TABLE]`/`--sample TABLE [N]`/`--export` в `handle_query` по `cleaned` ПЕРЕД fall-through на `execute_query` (AC #1); ветка авто-экспорта `> AUTO_EXPORT_THRESHOLD=500` в `execute_query` после `conn.description`-гарда, **переиспользуя открытый conn** (риск №2), граница строго `>` по `len(rows)` ортогонально `_clamp_limit` (AC #2/#8, риск №5); общий `_run_copy_export` (COPY по расширению, путь экранируется `''`, COUNT пере-чтением); `_export_query` (guard внутреннего SQL → расширение ∈{csv,parquet,json} → traversal/abs через `is_relative_to` → запрет клоббера → `mkdir` → свой conn; `except RuntimeError → str` для AC #8, AC #5/#6/#7, риск №1/№4); `_handle_schema` plain БЕЗ семантики (риск №6 — 3.3); двух-слойная валидация имени `_validate_table_name` (regex `^[A-Za-z0-9_]+$`) + `_check_table_exists` (сверка с `information_schema`, not-found со списком) + квотирование идентификатора `"name"` (AC #4); `--sample` дефолт `DEFAULT_SAMPLE=5` + клампинг `max(1, N)` через `.isdigit()` (AC #10); `--export` через `shlex.split`.
- **Task 3 — `gdau_mcp_server.py`:** `_save_audit_log` пишет конверт `{tool, timestamp, parameters, result}` в `data/mcp_output/` (`mkdir` на месте, сбой → `logger.warning`, НЕ `except:pass`, риск №7/AC #3/#9); вызывается в обёртке `duckdb_query` ПОСЛЕ `handle_query`; `readOnlyHint` перевёрнут `True→False` (канал пишет файлы-результаты, `destructiveHint=False`); docstring/`Field` рекламируют новые команды + авто-экспорт >500; `get_mcp_output_dir` из `scripts.utils.paths` (НЕ `common.py`).
- **Task 4 — `docs/mcp-query.md`:** добавлены сервисные команды, безопасность экспорта (только `data/results/`, traversal/клоббер/расширение), журнал обращений (`data/mcp_output/`), обновлены «Границы» (3.2 done, осталась 3.3) и ручной smoke.
- **Task 5 — тесты:** расширены `test_mcp_core.py` (новая фикстура `views_db` с реальными view'ами через `create_views` + 6 visits/2 hits/таблица `Mixed_Case`; покрыты AC #1/#2/#4/#5/#6/#7/#8/#10, риск №1/№5), `test_gdau_mcp_server.py` (audit-конверт AC #3, audit-сбой→WARNING AC #9, `readOnlyHint=False`, импорт `get_mcp_output_dir` из `paths`), `test_paths.py` (резолверы 3.2: пути, отсутствие mkdir, fail-loud). Регресс-тесты 3.1 не тронуты и остаются зелёными.

**Скоуп соблюдён:** НЕ принесены `--context`/`_handle_context`, `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`, goal-плейсхолдеры/`config_manager`, semantics в `--schema` (всё → 3.3). Read-only к БД сохранён: `gdau.duckdb` не мутируется, `.writer.lock` не берётся (экспорт пишет отдельный файл-результат). Зависимостей не добавлено (`shlex`/`json`/`datetime`/`re` — stdlib). Live неприменим (MCP-чтение/экспорт в Logs API не ходят).

### File List

- `scripts/utils/paths.py` (UPDATE) — `get_results_dir`/`get_mcp_output_dir` + `__all__`.
- `scripts/mcp/tools/core.py` (UPDATE) — роутинг спец-команд, авто-экспорт, `_run_copy_export`/`_export_query`/`_auto_export_path`/`_handle_schema`/`_handle_sample`/`_handle_export`/`_validate_table_name`/`_check_table_exists`/`_invalid_table_name_msg`, константы `AUTO_EXPORT_THRESHOLD`/`DEFAULT_SAMPLE`/`_VALID_TABLE_NAME`/`_EXPORT_EXTENSIONS`.
- `scripts/mcp/gdau_mcp_server.py` (UPDATE) — `_save_audit_log`, вызов аудита в обёртке, `readOnlyHint=False`, docstring/`Field`.
- `docs/mcp-query.md` (UPDATE) — сервисные команды + безопасность экспорта + аудит + граница 3.3.
- `tests/test_mcp_core.py` (UPDATE) — фикстура `views_db` + тесты AC #1/#2/#4/#5/#6/#7/#8/#10 + риски №1/№5.
- `tests/test_gdau_mcp_server.py` (UPDATE) — audit AC #3/#9, `readOnlyHint=False`, источник `get_mcp_output_dir`.
- `tests/test_paths.py` (UPDATE) — резолверы 3.2 (AC #7).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (UPDATE) — статус 3-2 ready-for-dev → in-progress → review.

## Change Log

| Дата | Изменение |
|---|---|
| 2026-05-26 | dev-story: реализованы сервисные команды MCP (`--tables`/`--schema [TABLE]`/`--sample TABLE [N]`/`--export`) + авто-экспорт >500 строк в `data/results/` + audit-лог каждого вызова в `data/mcp_output/` поверх тонкого read-канала 3.1 (FR-17). Все 10 AC закрыты, +47 тестов, гейты зелёные (mypy strict win32+linux, pytest 414 passed). Статус → review. |

## Definition of Done

1. `scripts/mcp/tools/core.py` (UPDATE поверх 3.1): `handle_query` роутит `--tables`/`--schema [TABLE]`/`--sample TABLE [N]`/`--export`, иначе `execute_query`; `execute_query` авто-экспортирует `>500` строк в `data/results/`; `_export_query` (guard внутреннего SQL + расширение/traversal/клоббер + mkdir + `COPY … TO`); `_handle_schema` plain (без семантики); `_validate_table_name` + проверка существования + квотирование. **Без** `--context`/семантики/плейсхолдеров (3.3). (AC #1/#2/#4/#5/#6/#8/#10)
2. `scripts/mcp/gdau_mcp_server.py` (UPDATE): `_save_audit_log` пишет конверт в `data/mcp_output/`, сбой → WARNING (не валит чтение); вызывается в обёртке `duckdb_query`; `readOnlyHint=False` (экспорт пишет файлы); docstring/`Field` рекламируют новые команды, не `--context`/Direct. `get_mcp_output_dir` из `scripts.utils.paths`, не `common.py`. (AC #3/#9)
3. `scripts/utils/paths.py` (UPDATE): чистые `get_results_dir()`/`get_mcp_output_dir()` (без mkdir), в `__all__`; fail-loud наследуется из `get_storage_root`. (AC #7)
4. Безопасность экспорта: путь принудительно под `data/results/` (traversal/abs → отказ), расширение ∈{csv,parquet,json} (иначе отказ), без молчаливого клоббера, внутренний SQL проходит read-only guard 3.1. (AC #4/#5/#6, риск №1)
5. Read-only к БД сохранён: `gdau.duckdb` не мутируется, `.writer.lock` не берётся; экспорт пишет отдельный файл-результат.
6. `docs/mcp-query.md` обновлён (сервис-команды + экспорт-безопасность + аудит; граница 3.3). (project-context: компонент без актуальной спеки не «готов»)
7. Тесты (UPDATE 3.1 + новые): сервис-команды/форматы / not-found+инъекция имени / авто-экспорт граница `>500` / traversal+abs отказ / клоббер+расширение / `--export "DROP…"` отказ guard'ом / отсутствующий каталог создаётся / `--sample` N-дефолт+клампинг / audit-конверт записан / audit-сбой→WARNING не валит / readOnlyHint=False / import-анти-зависимость (`config_manager`/`directaiq`/`common` не импортятся). Регресс-тесты 3.1 зелёные. (AC #1–#10)
8. Гейты зелёные: `mypy scripts` (`strict=true`; CI ubuntu+windows, локально доп. `--platform linux`), `pytest` (offline, ubuntu+windows); `uv.lock`/`pyproject.toml` не менялись. Live неприменим (MCP-чтение/экспорт в Logs API не ходят).
9. **Зависимость 3.1 учтена:** 3.1 прошла code-review и влита в `main` ДО старта 3.2; ветка 3.2 от обновлённого `main`; меняемые в коде 3.1 места (handle_query-роутинг по `cleaned`, execute_query-авто-экспорт с переиспользованием conn, `_export_query` `except RuntimeError`, readOnlyHint-флип, docstring) не сломали тесты 3.1 (pytest 363+ остаётся зелёным).

## Review Findings (Code Review 2026-05-26)

_Источник: bmad-code-review — 3 слоя Opus (Blind Hunter / Edge Case Hunter / Acceptance Auditor). Acceptance Auditor: все 10 AC PASS, 8 рисков PASS, DoD выполнен, гейты перепрогнаны зелёными (mypy strict 23 файла, pytest 78 по затронутым файлам). Триаж: 0 decision-needed, 4 patch, 1 defer, 7 dismiss._

- [x] [Review][Patch] `--sample TABLE <unicode-цифра>` роняет инструмент необработанным `ValueError` [scripts/mcp/tools/core.py:604] — `parts[2].isdigit()` истинно для надстрочных/дробных юникод-цифр (`²`, `⁵`), но `int(parts[2])` бросает `ValueError`. Исключение идёт мимо try/except (он в `execute_query`, а `int()` срабатывает ДО её вызова) и мимо обёртки `duckdb_query` → нарушение инварианта «все ошибки → строка, сервер жив» (AC #6 3.1), вдобавок аудит вызова пропускается. Фикс: `.isdigit()` → `.isdecimal()` (точно совпадает с тем, что принимает `int()`). Подтверждено эмпирически. source: edge
- [x] [Review][Patch] Audit-лог: имя файла с точностью до секунды → второй вызов в ту же секунду молча затирает первый [scripts/mcp/gdau_mcp_server.py:68] — имя `{tool}_{YYYY-MM-DD_HHMMSS}.json` + `open("w")`; два вызова `duckdb_query` в пределах одной секунды (типичный режим «агент водит CLI») теряют первую запись журнала → нарушение AC #3 «каждый вызов журналируется». `_auto_export_path` ровно этот риск уже закрывает микросекундами — аудит нет. Фикс: добавить `%f` (микросекунды) в имя файла. source: blind+edge
- [x] [Review][Patch] `--export "…" sub/out.csv` (подкаталог) проходит `is_relative_to`, но запись падает сырым IOException + утечка абсолютного пути [scripts/mcp/tools/core.py:537-555] — `sub/out.csv` остаётся внутри `data/results/` (`is_relative_to`=True), `exists()`=False, но `mkdir` создаёт только `results_dir`, не `results_dir/sub` → `COPY … TO` → `**SQL Error:** IO Error: Cannot open file "<абс.путь хранилища>"`. Правдоподобный ввод → недружелюбная ошибка + утечка пути (ниже планки «ошибки → дружелюбная строка»). Фикс: отклонять имя с подкаталогом (файл строго прямой потомок `data/results/`) понятным сообщением. source: edge
- [x] [Review][Patch] Комментарий «без … повторного fetch» вводит в заблуждение — авто-экспорт фактически повторно исполняет запрос [scripts/mcp/tools/core.py:380, 474] — `_run_copy_export` делает `COPY ({query})` + `SELECT COUNT(*)`, т.е. `query` исполняется ещё раз после fetch ради `len(rows)`. Переиспользование conn (риск №2) соблюдено, но «без повторного fetch» неверно: для недетерминированных запросов содержимое файла может разойтись с заявленным числом строк, плюс двойная стоимость. Для детерминированной аналитики приемлемо → фикс: привести комментарий/докстринг в соответствие коду (правдивое «почему»). source: blind+edge
- [x] [Review][Defer] Авто-экспорт >500 для не-обёртываемых read-команд (`DESCRIBE`/`SHOW`/`SUMMARIZE`) даёт ложную SQL-ошибку [scripts/mcp/tools/core.py:381-386] — deferred: введено этим изменением, но крайне узко (нужна метаданные-команда, дающая >500 строк; у `visits`/`hits` ~30 колонок — недостижимо), фикс добавляет ветвление вопреки «простота-первой». Записано в `deferred-work.md`. source: edge
