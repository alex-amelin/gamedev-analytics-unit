# Story 3.3: Контекст/схема под нашу схему — снятие directaiq-специфики

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита (агент),
I want авто-контекст рабочего слоя (`--context`: таблицы/view'ы, типы, row counts, диапазоны дат) и семантику колонок из **нашего каталога** в `--context`/`--schema TABLE`,
so that ориентироваться в данных игры без ручных подсказок и без неприменимой Direct/НДС-семантики — замыкая Epic 3 (FR-18, цикл «спросил → ответ в ту же сессию»).

## Acceptance Criteria

1. **Given** `--context`, **When** он вызывается, **Then** возвращает **markdown-сводку рабочего слоя**: по каждому объекту main-схемы (view'ы `visits`/`hits` + мета-таблица `load_state`) — список колонок с типом, `row_count` и диапазон дат (`MIN`/`MAX` date-подобной колонки). Роутинг `cleaned == "--context"` добавляется в **существующую** `handle_query` (3.2) ПЕРЕД fall-through на `execute_query`. _(Формат markdown — `--context` сам собирает текст, как directaiq `_handle_context`; параметр `format` для `--context` игнорируется.)_
2. **Given** семантика колонок, **When** формируется `--context` и `--schema TABLE`, **Then** она берётся из колонки `description` **каталога** (FR-16, `Catalog.fields_for(source)`) по `storage_name` источника и согласована с каталогом. **Это и есть «замена `_COST_COLUMN_SEMANTICS`»**: у нас нет Direct/НДС-денежных колонок (геймдев) — семантика = человекочитаемые описания полей из каталога, привязанные к `visits`/`hits`.
3. **Given** regex-fallback `(cost|.*_revenue)` и хардкод НДС/денег Директа, **When** сервер дорабатывается, **Then** их в коде **нет** (никогда не вендорились в 3.1/3.2) — `_GENERIC_MONEY_COL_RE`/`_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/`_MONEY_COL_TYPES` отсутствуют в `scripts/mcp/**`, закреплено guard-тестом. _[edge-case: regex-fallback вернулся бы по инерции вендоринга]_
4. **Given** goal-плейсхолдеры (`{{PRIMARY_GOAL_ID}}`/`{{PRIMARY_GOAL_CONV}}`/`{{GOAL_COLUMNS}}`/`{{DATE_30D}}`/`{{DATE_7D}}`) и завязка на `config_manager`, **When** сервер дорабатывается, **Then** `process_sql_placeholders`/`get_config`/сами строки-плейсхолдеры **отсутствуют**, прямой завязки на `config_manager` нет — закреплено ast/строковым guard-тестом.
5. **Given** интерфейс инструмента, **When** доработка завершена, **Then** единый `duckdb_query(query, format, limit)` и сервисные команды 3.2 (`--tables`/`--schema [TABLE]`/`--sample TABLE [N]`/`--export`) работают как прежде; 3.3 — **лёгкая доработка**: добавляет `--context` + колонку `semantics` в `--schema TABLE`. Регресс-тесты 3.1/3.2 зелёные.
6. **Given** остаточный импорт `config_manager` после снятия шва, **When** стартует сервер, **Then** импорта нет и сервер поднимается **без наличия** `config_manager` (нет `ImportError`); ast-тест: `scripts/mcp/**` не импортирует `config_manager`/`auth_manager`/`directaiq`/`scripts.mcp.utils.common`. _[edge-case: остаточный шов конфига]_
7. **Given** каталог недоступен/битый симлинк, **When** строится `--context` или `--schema TABLE`, **Then** `load_catalog()` бросает `ValueError`, который ловится → понятная ошибка строкой (сервер жив, риск №6 из 3.1), **без** полу-собранного контекста с пустой семантикой. _[edge-case: каталог недоступен при контексте]_
8. **Given** колонка view отсутствует в каталоге (напр. колонки `load_state`) ИЛИ поле каталога не сопоставилось колонке view, **When** строится семантика, **Then** для несопоставленной колонки — **пустая/«unknown» семантика + WARNING-лог**, без `KeyError` (через `dict.get`, не индексацию). _[edge-case: рассинхрон view↔каталог]_
9. **Given** пустые view'ы (нет партиций), **When** строится `--context`, **Then** `row_count=0` и `date_range=null` обрабатываются корректно (`COUNT(*)`→0, `MIN`/`MAX` по пустому → `NULL`; без деления/`None`-ошибки). _[edge-case: контекст по пустым view]_

---

## Главные риски / решения (читать ДО кода)

> **3.3 — ФИНАЛЬНАЯ история Epic 3.** Она встаёт **поверх сервисного слоя 3.2** (роутинг `handle_query`, `_handle_schema`, `_validate_table_name`) и тонкого read-канала 3.1. Содержательная работа 3.3 — **ДВЕ вещи**: (1) **ДОБАВИТЬ** `--context` (`_handle_context`) с row counts/диапазонами дат + семантику колонок из **каталога**, (2) **ОБОГАТИТЬ** `--schema TABLE` (3.2 plain) колонкой `semantics` из каталога. Третья «работа» — **снятие directaiq-специфики** (`_COST_COLUMN_SEMANTICS`/regex-fallback/goal-плейсхолдеры/`config_manager`) — **сводится к ASSERT-тестам отсутствия**, см. риск №1.

### ⚠️ РИСК №1 (читать ПЕРВЫМ) — «снимать» нечего: directaiq-специфика НИКОГДА не вендорилась → ACs #3/#4/#6 = guard-тесты, не удаление

Эпик формулирует 3.3 частично как «снятие directaiq-специфики», но **вариант A в 3.1 и скоуп 3.2 сознательно НЕ принесли** ни `_COST_COLUMN_SEMANTICS`, ни `_annotate_money_column`/`_GENERIC_MONEY_COL_RE`, ни `process_sql_placeholders`/`get_config`, ни `_handle_context`, ни `config_manager`/`scripts/mcp/utils/common.py`. **Проверено по фактическому коду** (`scripts/mcp/tools/core.py` импортирует ровно `DatabaseManager`; `gdau_mcp_server.py` — `handle_query` + dotenv; нигде нет `config_manager`).

- **НЕ ходить в directaiq и НЕ вендорить эту специфику, чтобы потом удалить.** Её здесь нет. ACs #3/#4/#6 закрываются **guard-тестами**, доказывающими ОТСУТСТВИЕ (строковый/ast-чек в `core.py`/`scripts/mcp/**`), + тем, что замена (`--context`/`--schema` семантика **из каталога**) — это и есть «замена `_COST_COLUMN_SEMANTICS`».
- **Что РЕАЛЬНО делаем:** `_handle_context` (новое, **адаптируем механику** information_schema + per-object COUNT/MIN-MAX + markdown-сборка из directaiq, **но семантику берём из каталога**, а не из денег/НДС), обогащение `_handle_schema` колонкой `semantics` из каталога, роутинг `--context`, docstring/`Field` сервера + спека + тесты.
- **Из `_handle_context` directaiq НЕ переносим:** секцию `## Money / units` (Direct/НДС hot-hint), `## Goal Columns`, `## Config` (config_manager). Это директовая разметка, не наша.

### ⚠️ РИСК №2 (жёсткая зависимость порядка) — 3.3 ИМПОРТИРУЕТ код 3.2, а 3.2 ещё НЕ написана

Состояние на момент создания истории (2026-05-26): **3.1 — `done` и влита в `main`**. **3.2 — `review`: dev-story ЗАВЕРШЁН, все 10 AC закрыты, код УЖЕ в рабочем дереве** ветки `story/3.2-mcp-service-commands` (некоммичен/не влит, ждёт code-review+merge). То есть **сервисный слой, на который встаёт 3.3, уже реализован на диске** — `core.py` несёт роутинг `handle_query` (`--tables`/`--schema [TABLE]`/`--sample`/`--export`), `_handle_schema` plain, `_validate_table_name`/`_check_table_exists`; `gdau_mcp_server.py` — `_save_audit_log` + `readOnlyHint=False`; `paths.py` — `get_results_dir`/`get_mcp_output_dir`.

> **⚠️ ОБЯЗАТЕЛЬНО:** перед кодом 3.3 — **прочитать ФАКТИЧЕСКИЙ `scripts/mcp/tools/core.py`/`gdau_mcp_server.py`/`paths.py`** (3.2 уже там). Прескрипции ниже носят **справочный** характер: где они расходятся с реальным кодом — побеждает реальный код (он сдан и оттестирован). **НЕ переписывать** существующие `_handle_schema`/`handle_query`/`_check_table_exists` целиком — только **аддитивно** дополнять (semantics-колонка, ветка `--context`).

- 3.3 **добавляет `--context` в роутер 3.2** и **обогащает `_handle_schema` 3.2**. Без кода 3.2 расширять нечего (точная параллель 4.2→4.3 в sprint-status: «нет scaffold → СТОП»).
- **Task 0 (предусловие):** 3.1 **и** 3.2 реализованы и доступны в рабочем дереве/`main` ДО старта dev 3.3. Если 3.2 не реализована → **СТОП, сначала закрыть 3.2**. Перед 3.3: прочитать **фактический** `core.py`/`gdau_mcp_server.py` после 3.2 (контракт ниже сверен с планом 3.2 + фактом 3.1; сверить с реальностью после merge 3.2).
- **Фактический контракт, на который встаёт 3.3** (3.1 — факт кода; 3.2 — план story 3.2):
  - `handle_query(query, output_format="json", limit=…)`: `cleaned = (query or "").strip()`; пустой → подсказка; **роутинг 3.2 по `cleaned`** (`--tables`/`--schema`/`--schema TABLE`/`--sample`/`--export`) ПЕРЕД `return execute_query(cleaned, …)`. **3.3 матчит `--context` по `cleaned`** (strip уже сделан — второй раз не делать).
  - `_handle_schema(table_name, output_format, limit) -> str` (3.2, **plain**): проверка существования (AC #4 3.2) → `SELECT column_name, data_type FROM information_schema.columns WHERE table_name='{name}' ORDER BY ordinal_position` → `execute_query`. **3.3 добавляет колонку `semantics`** (см. риск №4).
  - `_validate_table_name(name) -> str | None` (3.2): regex `^[A-Za-z0-9_]+$`. 3.3 переиспользует.
  - `execute_query` (3.1): guard `_reject_if_not_readonly` → `_clamp_limit` → read-only conn → timeout+retry → fetch → форматтер; **все ошибки ловятся ВНУТРИ и возвращаются строкой**. 3.3 его **не трогает**.
  - `_format_sql_error`, `format_result_{json,markdown,csv}` (3.1) — переиспользуем при нужде.
  - `gdau_mcp_server.py`: после 3.2 — `_save_audit_log` в обёртке, `readOnlyHint=False`. 3.3 **не меняет** `readOnlyHint` (канал по-прежнему пишет файлы экспорта 3.2) и аудит; только дополняет docstring/`Field` про `--context`.

### Риск №3 — семантика = `description` каталога, привязка по источнику (AC #2/#8)

Каталог (`development-docs/schema-catalog.csv`, колонки `source, storage_name, metrica_field, type, description`) уже несёт человекочитаемое `description` на каждое поле (напр. `visit_id` → «Идентификатор визита, уникален в рамках одного года»). `catalog.py` (1.5) отдаёт `Catalog.fields_for(source) -> tuple[CatalogField]`, где `CatalogField.description` — это семантика. Докстринг `catalog.py` прямо помечает `fields_for` как «MCP-контекст (3.3) для семантики колонок».

- **Маппинг:** имя колонки view = `storage_name` каталога; имя view (`visits`/`hits`) = `source` каталога (`VALID_SOURCES`). Семантика колонки `c` объекта `t` = `{f.storage_name: f.description for f in catalog.fields_for(t)}.get(c)` — **только если `t in VALID_SOURCES`**; иначе (напр. `load_state`) семантики нет.
- **Толерантность (AC #8):** `dict.get(c)` → `None` для несопоставленной колонки (колонки `load_state`, дрейф) → пустая/«unknown» семантика + **WARNING-лог** (не `KeyError`, не голый проброс). `description` в каталоге может быть пустой (catalog.py это допускает) → трактуется как «unknown» так же. _(Сейчас все описания заполнены — но код обязан пережить пустое/отсутствующее.)_
- **РЕШЕНИЕ (Шеф делегировал, зафиксировано):** добавить метод **`Catalog.descriptions(source) -> dict[str, str]`** в `catalog.py` (зеркало `duckdb_types`: `{f.storage_name: f.description for f in self.fields_for(source)}`). Семантику в `core.py` НЕ дублировать — проекция живёт в SSOT-модуле каталога (project-context: «из каталога генерируется семантика MCP — не дублировать руками»; докстринг `catalog.py` уже метит `fields_for` как «MCP-контекст 3.3»). Это аддитивный чистый аксессор (нулевой риск регресса), `core.py` зовёт `catalog.descriptions(table_name)` и применяет `dict.get` (AC #8). _(Отклонён inline-вариант в `core.py`: размазывал бы семантику-проекцию мимо SSOT.)_

### Риск №4 — `--schema TABLE`: эволюция plain (3.2) → + `semantics` из каталога (AC #2)

3.2 отдаёт `--schema TABLE` как `column_name, data_type` (plain, без семантики — её отложили в 3.3). directaiq строил `semantics` так: pre-fetch колонок в Python → на каждую `_annotate_money_column` → ветка `WHEN column_name='{col}' THEN '{semantic}'` (экранируя `'`→`''`) → `CASE … ELSE NULL END AS semantics` → `execute_query`. **3.3 повторяет ЭТУ механику, но источник семантики — каталог, а не деньги/НДС:**
- Загрузить каталог (`load_catalog()`; ошибка → AC #7); если `table_name in VALID_SOURCES` → `desc = {f.storage_name: f.description for f in catalog.fields_for(table_name)}`, иначе `desc = {}`.
- Собрать `CASE` из `desc` для колонок таблицы: `WHEN column_name = '{col}' THEN '{escaped_description}'` (экранировать `'`→`''`; пустое описание/нет ключа → ветку не добавлять); `semantics_expr = "CASE … ELSE NULL END"` либо `"NULL"` если ветвей нет.
- `SELECT column_name, data_type, {semantics_expr} AS semantics FROM information_schema.columns WHERE table_schema = 'main' AND table_name = '{name}' ORDER BY ordinal_position` → `execute_query(sql, output_format, limit)` (**один источник форматирования**, как 3.2). Имя уже прошло `_validate_table_name` (`^[A-Za-z0-9_]+$`) + удвоение `'` → инъекция невозможна.
- ⚠️ **СОХРАНИТЬ `WHERE table_schema = 'main' AND …`** из фактического 3.2-кода (`core.py` уже фильтрует по схеме; directaiq-форма — БЕЗ фильтра). Обогащение **только добавляет** колонку `semantics` — НЕ убирать фильтр схемы (иначе одноимённый объект в `temp`/`pg_catalog` задвоит строки → регрессия).
- **НЕ** строить `_annotate_money_column`/`_COST_COLUMN_SEMANTICS`/regex-fallback (риск №1).

### Риск №5 — `--context`: считать COUNT и для view'ов (отличие от directaiq); date-подобная колонка; пустые view (AC #1/#9)

directaiq `_handle_context` **пропускал `COUNT(*)` для VIEW** (`NULL as cnt`), чтобы не словить timeout на тяжёлом парсящем view (`v81_visits_parsed` с regex). **У нас иначе:** AC #1 **требует** row counts для `visits`/`hits` (это и есть главные объекты), а наши view'ы — тонкий `TRY_CAST` над parquet-glob (COUNT дешёв: DuckDB считает по метаданным parquet). **Решение:** `COUNT(*)` считаем и для view'ов.

- **Механика (адаптация directaiq, своя read-only conn):** `_handle_context()` открывает свою `DatabaseManager.connection(read_only=True)` → читает объекты/колонки/типы из `information_schema.columns` (+ `tables` для типа объекта, `table_schema='main'`) → на каждый объект: `SELECT COUNT(*) FROM "{name}"`; найти **первую** date-подобную колонку по `ordinal_position` (тип начинается на `DATE`/`TIMESTAMP` ИЛИ имя == `date`; для `visits`/`hits` это `date` DATE — раньше `date_time` в каталоге) → `SELECT CAST(MIN("{dcol}") AS VARCHAR), CAST(MAX("{dcol}") AS VARCHAR) FROM "{name}"` (квотировать имена `"…"`; **CAST AS VARCHAR** — иначе DATE/TIMESTAMP придут в Python объектами date и в markdown попадёт `datetime.date(...)`, а не `2026-05-20`).
- **Per-object SELECT'ы, НЕ directaiq-UNION ALL:** directaiq собирает COUNT+MIN/MAX одним `UNION ALL`; у нас — 2 служебных `SELECT` на объект. Осознанно: объектов мало (`visits`/`hits`/`load_state`), COUNT по тонкому view дёшев, проще и читаемее. Зафиксировать комментарием «почему» (расхождение с вендоринг-источником — ожидаемо).
- **Пустые view (AC #9):** `COUNT(*)`→0, `MIN`/`MAX` по пустому → `NULL` → `date_range=null`. Никакого деления/`None`-разыменования. _(view пустого источника собран `WHERE false` (2.6) — COUNT=0, MIN/MAX=NULL штатно.)_
- **Сборка вывода:** markdown — на каждый объект `### {name} ({row_count} строк[, {date_min}…{date_max}])`, затем колонки `- {col}: {type} — {semantics или «—»}`. Семантика — из каталога (риск №3) только для `visits`/`hits`.
- **Не через `execute_query`:** `_handle_context` гоняет несколько служебных SELECT'ов (server-controlled, не ввод агента) и сам собирает текст — как directaiq. На `duckdb.Error` в служебном запросе — классифицированная ошибка строкой (сервер жив); на ошибке каталога — AC #7. _(directaiq на сбое COUNT деградировал в `N/A` — допустимо, но для «одного оператора» проще понятная ошибка; деградацию оставить опционально.)_
- **Каталог недоступен (AC #7):** `load_catalog()` → `ValueError` → ловится → `«Каталог схемы недоступен: …»` строкой; НЕ собирать контекст с пустой семантикой.

### Риск №6 — `--context` и `format`; читаем, не пишем

`--context` возвращает **markdown-сводку независимо от `format`** (как directaiq `_handle_context`; параметр `format` для неё не осмыслен — это курированный текст). `--schema TABLE` уважает `format` (идёт через `execute_query`). Канал — **только чтение**: `_handle_context`/`_handle_schema` открывают `read_only=True`, `.writer.lock` не берётся, `gdau.duckdb` не мутируется. `--context` файлов не пишет (в отличие от `--export` 3.2).

### Риск №7 — мусорные dev-репо артефакты при тестах

Как 3.1/3.2/`test_views.py`: тесты против `tmp_path` + `monkeypatch.setenv(DATA_ROOT_ENV, …)`; `gdau.duckdb` создаётся write-conn'ом/`views.create_views` в фикстуре, MCP читает read-only. Каталог в тесте — мини-фикстура CSV через инъекцию пути в `load_catalog(path=…)` (catalog.py принимает `path`-шов), чтобы AC #7 (битый каталог) и AC #8 (рассинхрон) тестировались детерминированно, не завися от реального `schema-catalog.csv`. `.env`/`*.parquet`/`*.duckdb` в dev-репо не создавать.

---

## Tasks / Subtasks

- [ ] **Task 0 — Предусловие: 3.1 И 3.2 реализованы и влиты; прочитать фактический код (риск №2)**
  - [ ] 3.2 уже реализована и доступна в рабочем дереве (`review` на момент создания 3.3): роутинг `handle_query`, `_handle_schema` plain, `_validate_table_name`/`_check_table_exists`, `readOnlyHint=False`, аудит. **Ветвить 3.3 от ветки/коммита с кодом 3.2** (после её code-review+merge — от обновлённого `main`). Если на твоём дереве сервисного слоя 3.2 НЕТ — сначала получить его (3.3 расширять нечего).
  - [ ] Прочитать фактические `scripts/mcp/tools/core.py` + `scripts/mcp/gdau_mcp_server.py` ПОСЛЕ 3.2: форму `handle_query`-роутинга, сигнатуру `_handle_schema`, `_validate_table_name`, блок констант, `__all__`. Контракт ниже сверить с фактом (план 3.2 мог уточниться на ревью).
  - [ ] Прочитать `scripts/utils/catalog.py`: `load_catalog(path=None)`, `Catalog.fields_for(source)`, `CatalogField.description`, `VALID_SOURCES` (это источник семантики).
- [ ] **Task 1 — `_handle_context` + роутинг `--context` (`scripts/mcp/tools/core.py` UPDATE; AC #1/#2/#7/#8/#9)**
  - [ ] Обновить шапку-пометку вендоринга: 3.3 принёс `_handle_context` (механика information_schema/COUNT/MIN-MAX/markdown адаптирована) + семантику колонок **из каталога**; `trimmed:` теперь **закрыт** — `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/`_GENERIC_MONEY_COL_RE`/`process_sql_placeholders`/`get_config`/goal-плейсхолдеры/`config_manager` **не переносятся вовсе** (см. риск №1).
  - [ ] Импорт `from scripts.utils.catalog import load_catalog, VALID_SOURCES` (наш модуль, не config_manager — ast-тест проходит).
  - [ ] **`Catalog.descriptions(source) -> dict[str, str]` (`scripts/utils/catalog.py` UPDATE, риск №3 — решение зафиксировано):** зеркало `duckdb_types`: `return {f.storage_name: f.description for f in self.fields_for(source)}`; русский докстринг (как у соседей); +тест в `tests/test_catalog.py`. Аддитивный чистый аксессор — существующие методы/валидацию каталога НЕ трогать.
  - [ ] **`_handle_context() -> str`** (новое): своя `DatabaseManager.connection(read_only=True)`; объекты/колонки/типы из `information_schema` (`table_schema='main'`); per-object `COUNT(*)` (и для view'ов — риск №5) + `MIN`/`MAX` по date-подобной колонке (тип `DATE`/`TIMESTAMP` или имя `date`; квотировать `"…"`); семантика из каталога по источнику (риск №3, `dict.get` → AC #8); сборка markdown (`### {obj} ({N} строк[, dmin…dmax])` + `- col: type — semantics`). **БЕЗ** секций Money/Goal/Config. Каталог-ошибка → AC #7 (ловить `ValueError` от `load_catalog` → строка). Пустые → AC #9 (0/`NULL`).
  - [ ] **`_handle_context` ловит все ошибки в строку (риск №6 — паритет с `execute_query`):** функция зовётся **напрямую** из `handle_query` (НЕ через `execute_query`) → голое исключение порвёт MCP-сессию. Обернуть: `except RuntimeError → str(exc)` (до создания БД `DatabaseManager` бросает «… gdau-logs update» — дружелюбный текст, паритет AC #8), `except ValueError → str(exc)` (**покрывает И битый/недоступный каталог `load_catalog` AC #7, И битый/незаданный `GDAU_DATA_ROOT` из `paths.get_storage_root` — это один класс**; не делать `except` только под каталог), `except duckdb.Error → _format_sql_error(exc, "--context")`, `except Exception → "**Error:** …"`.
  - [ ] **`handle_query` (UPDATE):** добавить `if cleaned == "--context": return _handle_context()` в роутинг 3.2 (по `cleaned`, ПЕРЕД fall-through `execute_query`). Точное равенство. `_handle_context` **приватна** — НЕ расширять `__all__` (как весь сервисный слой 3.2: `_handle_schema`/`_validate_table_name` не экспортированы); тестировать через `core.handle_query("--context", …)`.
- [ ] **Task 2 — Обогащение `_handle_schema` семантикой каталога (`core.py` UPDATE; AC #2/#7/#8)**
  - [ ] **Аддитивно** дополнить существующий `_handle_schema` (3.2 на диске) колонкой `semantics` — НЕ переписывать целиком: загрузить каталог (ошибка → AC #7); `desc = catalog.descriptions(table_name)` если `table_name in VALID_SOURCES`, иначе `{}`; собрать `CASE WHEN column_name='{col}' THEN '{escaped_desc}' … ELSE NULL END AS semantics` (экранировать `'`→`''`; нет ключа/пустое описание → ветку пропустить; WARNING на несопоставленные — AC #8); итог `SELECT column_name, data_type, {semantics_expr} AS semantics FROM information_schema.columns WHERE table_schema = 'main' AND table_name='{name}' ORDER BY ordinal_position` → `execute_query(...)`. ⚠️ **СОХРАНИТЬ фильтр `table_schema = 'main'`** (он есть в 3.2-коде — не потерять, КРИТ риск регрессии). **НЕ** `_annotate_money_column`/`_COST_COLUMN_SEMANTICS`/regex (риск №1/№4).
  - [ ] Существование таблицы (AC #4 3.2) + квотирование имени — **переиспользовать из 3.2**, не дублировать.
- [ ] **Task 3 — Снятие directaiq-специфики = ASSERT + сервер docstring/Field (`gdau_mcp_server.py` UPDATE; AC #3/#4/#5/#6)**
  - [ ] **Ничего не удалять** (риск №1): `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/`_GENERIC_MONEY_COL_RE`/`process_sql_placeholders`/`get_config`/`config_manager`/`common.py` в репо отсутствуют. Работа — закрепить отсутствие тестами (Task 5).
  - [ ] **`Field`/docstring инструмента (UPDATE):** добавить в рекламу `--context` (авто-контекст: таблицы/типы/row counts/диапазоны дат + семантика колонок из каталога) и упомянуть, что `--schema TABLE` теперь несёт `semantics`. **НЕ** упоминать Direct/НДС/goal-плейсхолдеры/`{{…}}`/`t10_*`/`t18_*`.
  - [ ] **НЕ** менять `readOnlyHint` (после 3.2 = `False`, канал пишет файлы экспорта) и **НЕ** трогать `_save_audit_log` (3.2). 3.3 — только дополнение docstring/`Field`.
- [ ] **Task 4 — Спека `docs/mcp-query.md` (UPDATE; часть DoD)**
  - [ ] Дополнить (3 вопроса project-context): **что делает** — `--context` (обзор рабочего слоя: объекты, колонки/типы, сколько строк, за какие даты) + семантика колонок «что означает поле» из каталога в `--context`/`--schema TABLE`; **зачем** — ориентироваться без ручных подсказок, без неприменимой Direct/НДС-семантики; **контракт** — семантика согласована с каталогом схемы (SSOT), каталог битый → понятная ошибка, лок писателя по-прежнему не берётся. Снять из раздела «Границы» пункт про 3.3 (Epic 3 закрыт).
- [ ] **Task 5 — Тесты (`tests/test_mcp_core.py` + `tests/test_gdau_mcp_server.py` UPDATE)**
  - [ ] Фикстура: переиспользовать/расширить `views_db` 3.2 (`tmp_path` + `monkeypatch.setenv(DATA_ROOT_ENV, …)`; view'ы `visits`/`hits` через `create_views` поверх tmp-партиции; уже несёт таблицу `Mixed_Case`). **Добавить `load_state`** в фикстуру `--context` через `scripts.utils.load_state.ensure_load_state_table(conn)` — иначе AC #1 (`load_state` в выводе) и AC #8 (`load_state`-колонки → unknown) непроверяемы (в `views_db` его НЕТ). Каталог для AC #7/#8 — мини-CSV через `load_catalog(path=…)`-инъекцию (риск №7).
  - [ ] **AC #1:** `--context` → присутствуют секции `visits`/`hits`/`load_state` (проверять по **вхождению**/`>=`, НЕ точным набором объектов — в фикстуре есть и `Mixed_Case`), колонки с типами, `row_count`, диапазон дат (заполненный для непустого источника). `MIN`/`MAX` даты — строкой (CAST AS VARCHAR / isoformat), не repr объекта date.
  - [ ] **AC #2:** семантика колонки в `--context` и `--schema visits` совпадает с `description` каталога (напр. `visit_id` → его описание); `--schema visits` несёт колонку `semantics`.
  - [ ] **AC #2 (КРИТ — обновить сломанный тест 3.2):** `test_schema_single_table_columns_without_semantics` (3.2, `test_mcp_core.py`) жёстко требует `columns == ["column_name","data_type"]` и `set(row.keys()) == {"column_name","data_type"}` → обогащение `semantics` его **ломает**. Это **ожидаемая смена контракта 3.2→3.3, НЕ регрессия**: переименовать тест (убрать `without_semantics`), ждать `columns == ["column_name","data_type","semantics"]`, проверять `semantics` для `visit_id` == описание каталога. _(Это единственный тест 3.2, который 3.3 правомерно меняет; остальные — зелёные без изменений.)_
  - [ ] **AC #3/#4/#6 (guard отсутствия):** в `scripts/mcp/tools/core.py` НЕТ строк `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/`_GENERIC_MONEY_COL_RE`/`process_sql_placeholders`/`get_config`/`{{PRIMARY_GOAL_ID}}`/`{{DATE_30D}}`; ast: `scripts/mcp/**` не импортирует `config_manager`/`auth_manager`/`directaiq`/`scripts.mcp.utils.common` (как 3.1, по import-узлам, не подстрокой); сервер импортируется/регистрирует `duckdb_query` без `config_manager` (нет `ImportError`).
  - [ ] **AC #5 (регресс):** `--tables`/`--schema`/`--schema TABLE`/`--sample`/`--export` (3.2) и произвольный SQL (3.1) работают как прежде; `duckdb_query(query, format, limit)` сигнатура цела.
  - [ ] **AC #7:** битый каталог (несуществующий путь / `load_catalog` бросает) → `--context` и `--schema visits` отдают понятную ошибку строкой (сервер жив), НЕ полу-контекст.
  - [ ] **AC #8:** объект без записей в каталоге (`load_state`) → колонки с пустой/«unknown» семантикой (без `KeyError`); колонка view, которой нет в каталоге → то же + WARNING.
  - [ ] **AC #9:** пустые view'ы (нет партиций) → `--context` даёт `row_count=0` и пустой/`null` диапазон дат, без падения.
  - [ ] **Риск №6 (до данных):** `--context` на хранилище без `gdau.duckdb` → дружелюбная подсказка про `gdau-logs update` строкой (не сырой `RuntimeError`/трейсбек), сервер жив.
  - [ ] **`tests/test_catalog.py`** (UPDATE): `Catalog.descriptions(source)` → `{storage_name: description}` источника (порядок не важен); невалидный `source` → `ValueError` (наследуется из `fields_for`). Существующую валидацию каталога не трогать.
  - [ ] Существующие тесты 3.1/3.2 (guard/clamp/timeout/retry/роутинг/авто-экспорт/аудит) — оставить зелёными; **единственное правомерное изменение** — `test_schema_single_table_columns_without_semantics` (см. выше, смена контракта). Остальные добавление `--context`/semantics не ломает.
- [ ] **Гейты перед сдачей**
  - [ ] `uv run mypy scripts` → зелено (`strict=true`; новые функции типизированы; `fetchone()`/`fetchall()` → guard `None`; матрица CI ubuntu+windows, локально доп. `--platform linux`).
  - [ ] `uv run pytest` (offline) → зелено; маркер `live` не вводится (MCP-контекст в Logs API не ходит).
  - [ ] `uv.lock`/`pyproject.toml` не менялись (`mcp`/`duckdb`/`pydantic`/`python-dotenv` уже есть; `re`/`json` — stdlib; каталог — наш `catalog.py`).
  - [ ] Чек-лист «Definition of Done» пройден; `docs/mcp-query.md` обновлён.

## Dev Notes

### Рекомендуемый контракт 3.3 (поверх 3.1/3.2)

| Имя | Сигнатура | Смысл | Где |
|---|---|---|---|
| `handle_query` | `(query, output_format="json", limit=…) -> str` | **UPDATE 3.2**: + ветка `cleaned == "--context"` перед `execute_query` | `tools/core.py` |
| `_handle_context` | `() -> str` | **новое 3.3**: своя read-only conn → объекты/колонки/типы + COUNT + MIN/MAX(date) + семантика каталога → markdown; БЕЗ Money/Goal/Config | `tools/core.py` |
| `_handle_schema` | `(table, fmt, limit) -> str` | **UPDATE 3.2**: + колонка `semantics` (CASE из `description` каталога), вместо plain | `tools/core.py` |
| `duckdb_query` | `(query, format, limit) -> str` | **UPDATE 3.2**: docstring/`Field` + `--context` и `semantics`; `readOnlyHint`/аудит НЕ трогать | `gdau_mcp_server.py` |
| `Catalog.descriptions` | `(source) -> dict[str, str]` | **новое 3.3 (аддитив)**: `{storage_name: description}` источника — зеркало `duckdb_types`; источник семантики (SSOT) | `scripts/utils/catalog.py` |

**Карта примитивов, которые зовём:**
- `scripts.utils.catalog.load_catalog(path=None)` → `Catalog`; `Catalog.descriptions(source) -> dict[str, str]` (**добавляем в 3.3**, зеркало `duckdb_types` — источник семантики); `Catalog.fields_for(source)`/`CatalogField.description`; `VALID_SOURCES`. **Битый каталог → `ValueError`** (AC #7). _(catalog.py докстринг: «`fields_for` — MCP-контекст (3.3) для семантики колонок».)_
- `DatabaseManager.connection(read_only=True)` (`database_manager.py:39`) — `_handle_context` (information_schema + COUNT/MIN-MAX); read-only, лок не берётся; до создания БД → `RuntimeError` (наследуется — но `--context` обычно зовут после данных).
- `views.create_views(conn, …)` (`views.py:117`) — **только тестовая фикстура**.
- **3.1/3.2 (переиспользовать, не дублировать):** `_validate_table_name`/existence-check 3.2, `execute_query`/`_format_sql_error`/форматтеры 3.1, роутинг `handle_query` 3.2.
- **НЕ зовём:** `config_manager`/`get_config`/`process_sql_placeholders`/`scripts.mcp.utils.common` (нет в репо → 3.3 их и не вводит); `read_metrica_credentials`/`MetricaClient`/`p81`/`parquet_store`/`writer_lock` (путь записи Epic 1/2).

### Что НЕ переносим из directaiq (риск №1 — закрепить отсутствие, не вендорить)

| Из directaiq `core.py`/server | Статус в gdau | Действие 3.3 |
|---|---|---|
| `_COST_COLUMN_SEMANTICS` (dict `(table,col)→VAT-текст`) | никогда не вендорился | ASSERT отсутствия; замена = `description` каталога |
| `_annotate_money_column` / `_GENERIC_MONEY_COL_RE` / `_MONEY_COL_TYPES` (regex `(cost|.*_revenue)`) | никогда не вендорился | ASSERT отсутствия (AC #3) |
| `process_sql_placeholders` / `get_config` (`{{PRIMARY_GOAL_ID}}`/`{{DATE_30D}}`/`{{GOAL_COLUMNS}}`/…) | никогда не вендорился | ASSERT отсутствия (AC #4) |
| `scripts/mcp/utils/common.py` (`get_config`/`get_mcp_output_dir` directaiq) | не существует | НЕ заводить; аудит 3.2 берёт `get_mcp_output_dir` из `paths.py` |
| `## Money / units` / `## Goal Columns` / `## Config` секции `_handle_context` | — | НЕ переносить в наш `_handle_context` |
| `config_manager`-импорт | нигде в `scripts/**` (вырезан с Epic 1) | ASSERT: сервер стартует без него (AC #6) |

### Паттерны (соблюдать — снижают цикл ревью)
- `from __future__ import annotations` первой строкой; русские docstrings/комментарии (модульный обязателен), английские идентификаторы; type hints везде, `mypy --strict`, без `Any`-дыр; абсолютные импорты от корня; `logger = logging.getLogger(__name__)` (диагностика — `logging`; WARNING на несопоставленную семантику AC #8 — через logger).
- **Вендоренный код — обновлённая шапка-пометка** «vendored from directaiq @ <ref>, seam: …, trimmed: …»: для 3.3 зафиксировать, что Direct/НДС/goal/`config_manager` НЕ переносятся принципиально (не «вычищены», а не вводились), а семантика — из нашего каталога (развязка шва FR-18).
- **Комментарии «почему», не «что»** — особенно: почему COUNT считается и для view'ов (отличие от directaiq, риск №5); почему семантика из каталога, а не из денег/НДС (геймдев, риск №1/№3); почему `--context` собирает текст сам, мимо `execute_query` (server-controlled SQL).
- **Read-only — инвариант** (project-context «MCP=только чтение»): `_handle_context`/`_handle_schema` — `read_only=True`, лок не берётся, БД не мутируется, файлов `--context` не пишет.
- **Каталог = SSOT** (project-context): семантика берётся ТОЛЬКО из каталога; не хардкодить описания в `core.py`. Не тащить `config_manager`/тяжёлые зависимости; новых зависимостей не добавлять.

### Границы 3.3 (не выходить)
- **Трогаем (UPDATE):** `scripts/mcp/tools/core.py` (`_handle_context` новое, роутинг `--context`, обогащение `_handle_schema`), `scripts/mcp/gdau_mcp_server.py` (docstring/`Field`), `scripts/utils/catalog.py` (**аддитивный** `Catalog.descriptions(source)` — зеркало `duckdb_types`, существующее не трогать; риск №3), `docs/mcp-query.md`, `tests/test_mcp_core.py`, `tests/test_gdau_mcp_server.py`, `tests/test_catalog.py`.
- **Не трогаем:** `paths.py` (новых путей нет — `--context` ничего не пишет), `execute_query`/guard/clamp/timeout/retry 3.1, авто-экспорт/`_export_query`/`_save_audit_log`/`readOnlyHint` 3.2, код Epic 1/2 (клиент/оркестратор/запись/view'ы/лок — только читаем). `.mcp.json` — не трогаем.
- **Не** реализуем заново 3.1/3.2 — встаём поверх.

### Project Structure Notes
- Раскладка architecture.md:461-463: `scripts/mcp/gdau_mcp_server.py` + `scripts/mcp/tools/core.py` (`:463` — «ядро duckdb_query; **семантика колонок из каталога**»). Запуск — `.mcp.json` (`uv run python -m scripts.mcp.gdau_mcp_server`).
- Тесты — плоские `tests/test_<area>.py`; `conftest.py` нет (`tmp_path`/`monkeypatch` напрямую). `test_catalog.py` существует (паттерн мини-фикстуры каталога — посмотреть). Маркер `live` не для MCP-чтения.
- Каталог `development-docs/schema-catalog.csv` — артефакт dev-репо (в хранилище приходит симлинком), путешествует с кодом; `DEFAULT_CATALOG_PATH` резолвится от модуля (catalog.py), не от cwd. `gdau.duckdb`/`*.parquet`/`.env`/файлы экспорта — артефакты хранилища (`GDAU_DATA_ROOT`), в dev-репо не создаются/не коммитятся.
- Не переводить на src-layout, не переименовывать пакет `scripts` (hatchling `packages=["scripts"]`).

### Live-smoke / DoD
- **Live неприменим** (как 3.1/3.2/2.1/2.6): `--context`/`--schema` **не дёргают внешний Logs API** — читают локальный `gdau.duckdb` + каталог CSV. Мандат live-smoke (project-context) касается контракта внешнего API; здесь его нет. Достаточно offline против временного DuckDB + мини-каталога.
- **Ручной smoke (опционально, не тест):** против `G:\gdau-smoke` поднять сервер и проверить `--context` (есть `visits`/`hits` с row counts и диапазоном дат, семантика колонок из каталога) и `--schema visits` (колонка `semantics` = описания каталога). Описать в `docs/mcp-query.md`.

### Эмпирические факты (база 3.1, DuckDB 1.5.3 — грунт под `--context`)
- `read_only=True` блокирует запись в `gdau.duckdb` (`CREATE/INSERT/DROP` → `InvalidInputException`), но `COUNT(*)`/`MIN`/`MAX` по view'ам — обычные read-операции, проходят на read-only conn. `--context` ничего не пишет.
- Пустой источник: `build_view_ddl` (2.6, `has_partitions=False`) собирает view `CAST(NULL …) WHERE false` → `COUNT(*)`=0, `MIN`/`MAX`=`NULL` (AC #9), без обращения к parquet (ложного `IOException` не будет).
- Каталог уже несёт `description` на каждое поле (`visits` + `hits`); сейчас все заполнены, но `catalog.py` допускает пустое `description` → код обязан пережить пустое/несопоставленное (AC #8).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story 3.3] (строки 414-430) — 9 AC: `--context` (таблицы/типы/row counts/диапазоны дат); семантика из каталога (FR-16), замена `_COST_COLUMN_SEMANTICS`; нейтрализация regex-fallback; снятие goal-плейсхолдеров/`config_manager`; сохранение интерфейса; edge (остаточный config_manager, каталог недоступен, рассинхрон view↔каталог, пустые view). [#Epic 3] (371-373) — 3.3 закрывает Epic 3.
- [Source: prd.md#FR-18] (строки 268-275) — контекст/схема: таблицы/view'ы, типы, row counts, диапазоны дат; семантика согласована с каталогом (FR-16); Direct/НДС/goal убраны/заменены; интерфейс сохраняется (лёгкая доработка). [#FR-16] — каталог = SSOT для DDL view (FR-7) и семантики MCP (FR-18).
- [Source: architecture.md] — :49, :235-238 (MCP=канал чтения, лёгкая доработка: заменить `_COST_COLUMN_SEMANTICS` семантикой каталога + нейтрализовать regex `(cost|.*_revenue)`; убрать goal-плейсхолдеры + завязку на `config_manager`; интерфейс + сервис-команды сохраняются); :463 (`tools/core.py` — семантика колонок из каталога); :601 (OQ#4 residual — финальный список правок `core.py` снятия `config_manager`/плейсхолдеров = эта история).
- [Source: _bmad-output/implementation-artifacts/3-1-вендоринг-mcp-сервера-и-инструмент-duckdb-query.md] — фактический код 3.1 (`done`/влита): `handle_query`/`execute_query`/`_reject_if_not_readonly`/`_clamp_limit`/`_format_sql_error`/форматтеры; вариант A — тонкий read-канал, контекст/семантика → 3.3; что НЕ вендорилось (config_manager/`_COST_COLUMN_SEMANTICS`/goal-плейсхолдеры).
- [Source: _bmad-output/implementation-artifacts/3-2-сервисные-команды-mcp-и-авто-экспорт-аудит.md] — **база, на которую встаёт 3.3** (⚠️ риск №2 — должна быть реализована ПЕРВОЙ): роутинг `handle_query`, `_handle_schema` plain, `_validate_table_name`+existence-check, `readOnlyHint=False`, аудит; явно отложил в 3.3: `--context`/`_handle_context`, `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/regex-fallback, goal-плейсхолдеры/`config_manager`, semantics-часть `_handle_schema`.
- [Source: scripts/mcp/tools/core.py] — фактический код 3.1 (импорт только `DatabaseManager`; `handle_query` без роутинга; нет config_manager/семантики).
- [Source: scripts/utils/catalog.py] — `load_catalog(path=None)` (ValueError на missing/битый — AC #7), `Catalog.fields_for(source)`, `CatalogField.description` (семантика — AC #2/#8), `VALID_SOURCES`; докстринг помечает `fields_for` как MCP-контекст 3.3.
- [Source: development-docs/schema-catalog.csv] — `source,storage_name,metrica_field,type,description`; `description` — человекочитаемая семантика поля (напр. `visit_id` → «Идентификатор визита…»); `visits` и `hits` оба имеют `date`/`date_time`.
- [Source: scripts/utils/database_manager.py:39] — `DatabaseManager.connection(read_only=True)`; RuntimeError до создания БД, `finally`-close, лок не берёт.
- [Source: scripts/utils/views.py:117] — `create_views(conn, …)` для тестовой фикстуры; пустой источник → view `WHERE false` (AC #9).
- [Source: G:\git\directaiq (фактически D:\git\directaiq)\scripts\mcp\tools\core.py] — вендоринг-механика: `_handle_context` (331-474, **адаптируем** information_schema + UNION/COUNT/MIN-MAX + markdown; **НЕ** берём `## Money/Goal/Config`), `_handle_schema` semantics (482-518, **механику CASE берём, источник — каталог**), роутинг `--context` (`== "--context"`, 538-539). **НЕ берём:** `_COST_COLUMN_SEMANTICS` (26-39), `_annotate_money_column`/`_GENERIC_MONEY_COL_RE` (43-57), `process_sql_placeholders` (68-116), `common.get_config` (config_manager).
- [Source: _bmad-output/project-context.md] — каналы (MCP=только чтение), каталог=SSOT для семантики MCP, вендоринг с шапкой+развязка швов, не тащить `config_manager`, docs/<component>.md как DoD, тесты по import-узлам не подстрокой.
- [Memory] [[mcp-env-delivery]] (Claude Code не грузит `.env`), [[dotenv-usecwd-gotcha]], [[gdau-smoke-live-storage]] (`G:\gdau-smoke` для ручного smoke), [[gdau-env-contract]] (`GDAU_DATA_ROOT`), [[directaiq-vendor-source]] (источник вендоринга), [[parallel-epic3-epic4-worktrees]] (стык `.mcp.json`→4.3).

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Definition of Done

1. `scripts/mcp/tools/core.py` (UPDATE поверх 3.2): `handle_query` роутит `--context` (+ существующие `--tables`/`--schema [TABLE]`/`--sample`/`--export` 3.2), иначе `execute_query`; `_handle_context` (новое) отдаёт markdown-сводку рабочего слоя — объекты/колонки/типы + row counts + диапазоны дат + семантика колонок **из каталога**; `_handle_schema` обогащён колонкой `semantics` из каталога. **Без** `_COST_COLUMN_SEMANTICS`/`_annotate_money_column`/regex-fallback/`process_sql_placeholders`/`get_config`/goal-плейсхолдеров. (AC #1/#2/#3/#4/#7/#8/#9)
2. `scripts/mcp/gdau_mcp_server.py` (UPDATE): docstring/`Field` рекламируют `--context` и колонку `semantics`; **НЕ** упоминают Direct/НДС/goal. `readOnlyHint`/`_save_audit_log` (3.2) не тронуты. Сервер поднимается без `config_manager` (нет `ImportError`). (AC #5/#6)
3. Семантика колонок — из каталога схемы (FR-16) через **`Catalog.descriptions(source)`** (аддитивный аксессор в `catalog.py`, зеркало `duckdb_types`), согласована с каталогом; «замена `_COST_COLUMN_SEMANTICS`». Direct/НДС/goal/`config_manager`-специфика отсутствует (никогда не вендорилась) — закреплено guard/ast-тестами. (AC #2/#3/#4/#6)
4. Read-only к БД сохранён: `_handle_context`/`_handle_schema` — `read_only=True`, `.writer.lock` не берётся, `gdau.duckdb` не мутируется, `--context` файлов не пишет. (риск №6)
5. Толерантность: каталог недоступен/битый → понятная ошибка строкой, сервер жив (AC #7); рассинхрон view↔каталог → пустая/«unknown» семантика + WARNING, без `KeyError` (AC #8); пустые view → `row_count=0`/`date_range=null` (AC #9).
6. Интерфейс сохранён: `duckdb_query(query, format, limit)` + сервис-команды 3.2 работают; регресс-тесты 3.1/3.2 зелёные. (AC #5)
7. `docs/mcp-query.md` обновлён (`--context` + семантика каталога; Epic 3 закрыт). (project-context: компонент без актуальной спеки не «готов»)
8. Тесты (UPDATE 3.1/3.2 + новые): `--context` (объекты/типы/row counts/даты/семантика) / `--schema TABLE` с `semantics` из каталога / guard отсутствия Direct-НДС-goal-config_manager (строковый + ast) / сервер без `config_manager` / каталог-битый → ошибка / рассинхрон → unknown+WARNING / пустые view → 0/null / регресс 3.1/3.2. (AC #1–#9)
9. Гейты зелёные: `mypy scripts` (`strict=true`; CI ubuntu+windows, локально доп. `--platform linux`), `pytest` (offline); `uv.lock`/`pyproject.toml` не менялись. Live неприменим (MCP-контекст в Logs API не ходит).
10. **Зависимость порядка учтена (риск №2):** 3.1 **и** 3.2 реализованы/влиты ДО старта 3.3 (3.3 расширяет роутинг 3.2 и `_handle_schema` 3.2); ветка 3.3 от обновлённого `main`; меняемые места не сломали тесты 3.1/3.2.
