---
stepsCompleted: [1, 2, 3, 4, 5, 6, 7, 8]
lastStep: 8
status: 'complete'
completedAt: '2026-05-23'
inputDocuments:
  - '_bmad-output/planning-artifacts/briefs/brief-gamedev-analytics-unit-2026-05-23/brief.md'
  - '_bmad-output/planning-artifacts/briefs/brief-gamedev-analytics-unit-2026-05-23/addendum.md'
  - '_bmad-output/planning-artifacts/prds/prd-gamedev-analytics-unit-2026-05-23/prd.md'
  - '_bmad-output/planning-artifacts/prds/prd-gamedev-analytics-unit-2026-05-23/addendum.md'
  - '_bmad-output/planning-artifacts/prds/prd-gamedev-analytics-unit-2026-05-23/reconcile-brief.md'
  - 'D:/git/directaiq (референсный проект — изучен по коду, не как источник продуктовой логики)'
workflowType: 'architecture'
project_name: 'gamedev-analytics-unit'
user_name: 'Шеф'
date: '2026-05-23'
guidingPrinciple: 'Простота, понятность, стабильность. Усложнять только по реальной потребности.'
---

# Architecture Decision Document — Game Dev Analytics Unit

_This document builds collaboratively through step-by-step discovery. Sections are appended as we work through each architectural decision together._

## Project Context Analysis

### Requirements Overview

**Functional Requirements:** 21 FR в 7 группах, UX-спеки нет, эпики ещё не созданы.

- **Приём данных через Logs API (FR-1…FR-5).** Оркестрация полного асинхронного цикла
  (create → poll до `processed` → download parts → clean) поверх вендоренного клиента
  Метрики. Тянется только параметризуемый «заданный список полей» (не «всё»). Источники
  visits + hits (оба в v1). Креды через тонкий env-ридер (без Direct-fallback). Безопасный
  clamp `date2` на «вчера по МСК».
- **Хранение — два слоя (FR-6…FR-8).** Сырьё → Parquet, партиции по дню, строками, без CAST
  (верно источнику). Рабочий слой → DuckDB поверх Parquet с типизацией через `TRY_CAST`
  (битая ячейка → NULL + лог). Ноль серверных процессов; переносимость Win↔Linux копированием
  папки. ID-поля → HUGEINT.
- **Обновление и инкремент (FR-9…FR-11).** Догрузка только отсутствующих дней; идемпотентный
  перезалив одного дня (перезапись одной партиции, без DROP всей таблицы); hot-window перезалива
  последних N=3 дней для доезжающих данных.
- **Консистентность и защита базы (FR-12…FR-15) — NFR первого класса.** Мета-таблица состояния
  (чекпойнт, источник истины — факт партиции); жёсткая сверка строк источник↔БД (fail, не warning);
  атомарная запись дня (temp→rename); дисциплина одного писателя (fail-fast лок). Тяжёлая очередь/
  воркер/disk-guard уровня directaiq — вне v1.
- **Каталог схемы (FR-16).** Собственный машиночитаемый каталог полей — единый источник для DDL
  рабочего слоя и для семантики, отдаваемой агенту через MCP.
- **Доступ агента к данным — MCP (FR-17…FR-18).** Переиспользуемый `duckdb_query(query, format,
  limit)` с сервисными командами (--context/--tables/--schema/--sample/--export); лёгкая доработка
  под нашу схему (замена семантики колонок, снятие Direct/НДС/goal-плейсхолдеров).
- **Двух-репо и инициализация (FR-19…FR-21).** dev-репо (инструменты) ↔ внешнее per-game хранилище
  (БД + конфиги + рабочая папка), связь по декларативному симлинк-контракту; одна init-команда
  разворачивает хранилище; файл описания проекта для агента.

### Non-Functional Requirements

- **Целостность данных («не сломать базу») — доминирующий NFR.** Идемпотентность, атомарность,
  жёсткая сверка, единственный писатель, crash-recovery.
- **Переносимость Win↔Linux + ноль серверных процессов** (только файлы + встроенный движок).
- **Устойчивость к лимитам Logs API** (rate-limit ≤30 req/s, ≤5000 req/day; retry с backoff).
- **Корректность типов:** HUGEINT для visitID/clientID/watchID (> 2^63).
- **Безопасность кредов:** `.env` живёт во внешнем per-game хранилище, не в dev-репо.

### Scale & Complexity

- Primary domain: локальный data-engineering инструментарий (CLI-пайплайн + файловое хранилище
  + MCP-инструмент для агента). Не web/mobile/UI.
- Complexity level: medium (один оператор, нет real-time/мульти-аккаунта/расписаний; но
  нетривиальны асинхронный API-цикл, двухслойное хранение, дисциплина идемпотентности,
  двух-репо с симлинками, вендоринг с развязкой швов).
- Estimated architectural components: ~7–8 (env-ридер + вендоренный Logs API клиент; оркестратор
  загрузки; Parquet raw-слой; DuckDB working-слой; мета-состояние/консистентность; каталог схемы;
  MCP-сервер; init/симлинк-механизм).

### Technical Constraints & Dependencies

- **Logs API:** асинхронный (create→poll→download→clean), `date2 < today`, rate-limit, формат TSV.
- **DuckDB:** single-writer (нативно один писатель) → дисциплина одного писателя обязательна.
- **Стек-референс directaiq:** Python 3.13+, uv, duckdb, requests, mcp (FastMCP), nushell (init).
- **Швы вендоринга (требуют развязки):** `MetricaClient.__init__` зовёт `AuthManager` →
  инжектировать готовые креды; MCP завязан на `config_manager` (goal-плейсхолдеры) и хардкод
  НДС/денег в `_COST_COLUMN_SEMANTICS`; оркестратор `p81_load_logs.py` наследует `BaseScript`.
- **Платформа разработки:** Windows (рабочая машина); хранилище должно жить и на Linux.

### Cross-Cutting Concerns Identified

- **«Заданный список полей» как единый источник истины** — пронизывает выгрузку (FR-2),
  типизацию (FR-7), каталог (FR-16), контекст MCP (FR-18). Рассинхрон = системный дефект.
- **Каталог схемы** — единый источник для DDL рабочего слоя и семантики MCP.
- **Идемпотентность/атомарность** — сквозная дисциплина обновления и защиты базы.
- **Резолюция путей и переносимость** — пронизывает хранение, init, симлинки (Win↔Linux).
- **Креды/окружение** (env-ридер) — общая зависимость клиента и оркестратора.
- **Логирование диагностики** — clamp дат, битые ячейки TRY_CAST, fail сверки.
- **Граница dev-репо ↔ per-game хранилище** — что где живёт, проходит через все компоненты.
- **Дисциплина «не приносить лишнюю инфраструктуру directaiq»** — простота как явный инвариант.

### Noted Intents / Guiding Principles (to formalize in Decisions)

- **CLI-tool — AI-native интерфейс первого класса.** Агент — оператор юнита; возможности
  выставляем как скриптуемые неинтерактивные CLI-команды (text in/out, `--help`), а не прячем
  в библиотечные внутренности. Причина: агенту всегда может понадобиться кастомный/ad-hoc
  запрос к API — он соберёт его из примитивов-команд. Наследуем философию directaiq
  («выгрузка → CLI, анализ → MCP, скрипты без интерактива»).
- **Разделение каналов агента:** CLI = действия / выгрузка / жизненный цикл Logs API
  (пишут, дёргают сеть); MCP `duckdb_query` = чтение / анализ данных.
- **Свой CLI-tool Logs API в формате directaiq, ПОЛНАЯ поверхность:** высокоуровневая команда
  обновления поверх своего Parquet-оркестратора + тонкие подкоманды
  create/status/download/clean/evaluate/list над вендоренным MetricaClient. Формат — как у
  directaiq; поведение `load` — своё (FR-6/7/10/11/13/14).
- **Не переносим тяжёлую обвязку** directaiq (queue_cli / STOP-rule / disk-guard / cron) —
  против принципа простоты; для одного оператора достаточно прямого вызова + fail-fast лока.
- **«Стандартный датасет для ресёрча» = типизированный рабочий слой DuckDB** (visits+hits,
  заданный список полей). Его *определение* (каталог/поля/DDL) — в dev-репо (стандарт на все
  игры), его *данные* — в per-game external storage (изолированы, особняком). Наполняется/
  обновляется CLI-командой; исследуется через MCP. Материализация (view vs таблицы) — OQ#3.

## Starter Template Evaluation

### Primary Technology Domain

Локальный Python-инструментарий для дата-инжиниринга: CLI-пайплайн загрузки (Logs API → Parquet
→ DuckDB) + MCP-сервер для агента. Не web/mobile/UI — типовые web-стартеры (Next.js и пр.) неприменимы.

### Starter Options Considered

1. **Публичный Python-boilerplate** (cookiecutter / шаблоны Typer / oclif-аналоги). Даёт лишь голый
   каркас; не содержит доменных примитивов (клиент Logs API, MCP-сервер, двух-репо init). Низкая
   ценность сверх `uv init`. ОТКЛОНЕНО.
2. **Форк `directaiq` целиком.** Даёт всё сразу, но тащит тяжёлую инфраструктуру (queue/worker,
   disk-guard, мульти-аккаунт, иерархия `BaseScript`, `config_manager`, маркетинг-методология).
   Прямо против принципа простоты v1. ОТКЛОНЕНО.
3. **Чистый каркас на `uv` + точечный вендоринг из `directaiq`.** ВЫБРАНО. Стартуем из пустого
   `uv`-проекта, переносим только согласованные примитивы (MetricaClient с развязкой шва, ядро
   MCP-сервера, паттерн init/симлинков), оркестратор пишем свой. Простота + проверенные примитивы.

### Selected Starter: `uv`-каркас + directaiq как структурный референс (selective vendoring)

**Rationale for Selection:**
Минимальный воспроизводимый Python-каркас (`uv` + `uv.lock`), в который осознанно переносятся только
нужные примитивы directaiq. Совпадает с принципом «простота, понятность, стабильность»: ничего лишнего
из инфраструктуры directaiq не приносится; стек закреплён локом; примитивы — проверенные.

**Initialization Command (ориентир; точная раскладка — step-06):**

```bash
uv init --package gamedev-analytics-unit   # src-layout + entry points под CLI-команды
# далее: uv add duckdb requests "mcp>=1.2" python-dotenv PyYAML
```

**Architectural Decisions Provided by Starter:**

**Language & Runtime:** Python `>=3.13` (текущая серия 3.14; пин пола под совместимость с вендорингом).

**Package/Build Tooling:** `uv` (pyproject.toml + uv.lock). Воспроизводимость = стабильность.

**Dependencies (v1, пинятся через lock):** `duckdb` (1.5.x stable), `requests`, `mcp` (офиц. SDK,
FastMCP встроен), `python-dotenv` (env-ридер), `PyYAML` (каталог схемы). НЕ тянем: `tapi-yandex-*`,
аналитический стек (`pandas/numpy/scipy/numba/prophet/polars`).

> **Проверено по коду directaiq (2026-05-23):** `tapi-yandex-metrika` не используется нигде в `scripts/`
> (directaiq держит её как «к удалению»; Метрика работает через прямые HTTP-запросы `metrica_client.py`).
> `polars` в `metrica_client.py` нужен только методам отчётного API (`get_report*`), которые мы не
> переносим. → при вендоринге обрезаем reporting-методы, клиент остаётся чисто на `requests`.

**MCP Framework:** официальный `mcp` SDK, `mcp.server.fastmcp.FastMCP` (как в вендоримом сервере
directaiq), НЕ отдельный `fastmcp` 3.x (другая архитектура).

**Testing:** `pytest` (как в directaiq), CI на GitHub Actions.

**Code Organization:** dev-репо (инструменты) + per-game external storage. Точная раскладка — step-06.

**Verified Versions (web, 2026-05-23):** Python 3.14.4 / 3.13.13; uv 0.11.16; DuckDB 1.5.2 stable
(1.4.x LTS до сен 2026); MCP Python SDK 1.27.1 (FastMCP встроен).

### Decisions Deferred to Step-04 (Architectural Decisions)

- **Язык init-команды:** nushell (как directaiq) vs Python vs PowerShell — баланс «без лишней
  зависимости» vs переносимость Win↔Linux.
- **CLI-фреймворк:** stdlib `argparse` (ноль зависимостей) vs `Typer`/`Click` (удобнее, но зависимость).
- **Целевой Python:** зафиксировать пол (`>=3.13`) и dev-версию.
- **DuckDB:** stable 1.5.x vs LTS — финализировать пиннинг.

**Note:** Инициализация проекта этой командой (`uv init` + перенос примитивов) должна стать первой
имплементационной историей.

## Core Architectural Decisions

### Decision Priority Analysis

**Critical (блокируют реализацию):**
- Модель хранения: Parquet по дням (сырьё) + DuckDB-view'ы поверх Parquet (рабочий слой).
- Консистентность: мета-таблица состояния + атомарная запись + жёсткая сверка строк + лок одного писателя.
- Оркестратор приёма (per-day) и границы вендоринга (MetricaClient, MCP).
- Двух-репо + симлинк-контракт + язык init.
- Формат каталога схемы; CLI-фреймворк.

**Important (формируют архитектуру):** env-ридер; логирование; пиннинг Python/DuckDB; hot-window N=3.

**Deferred (post-MVP / pre-impl):** агрегатный Reports API; материализованные таблицы; миграция
DuckDB→ClickHouse (OQ#5); тяжёлая инфра (queue/disk-guard/cron/мульти-аккаунт). **Pre-impl
зависимость:** точный список полей visits/hits (OQ#1) — назвать до старта дата-эпиков.

### Data Architecture

- **Сырьевой слой:** Parquet, партиции по дню, строками, без CAST (верно источнику).
- **Рабочий слой:** DuckDB-**view'ы** поверх Parquet с типизацией `TRY_CAST`; ID → HUGEINT
  (visitID/clientID/watchID > 2^63). Решение OQ#3.
- **Единый источник истины — заданный список полей** в CSV-каталоге; из него генерируются
  DDL/определения view и семантика для MCP. Поле без записи в каталоге = дефект.
- **Формат каталога:** CSV (плоский, строка на поле; машинно-парсимо и читаемо в табличном виде).
  Файл: `development-docs/schema-catalog.csv`. Колонки: source, storage_name, metrica_field, type, description.
- **Мета-состояние:** таблица `load_state` (source, date, row_count, loaded_at, status) +
  реконсиляция против факта Parquet-партиции на старте (источник истины — факт партиции).
- **Миграции/DDL:** лёгкие, генерируются из каталога; без тяжёлого фреймворка миграций.

### Authentication & Security (креды)

- Тонкий env-ридер: `YANDEX_METRICA_TOKEN` + `YANDEX_METRICA_COUNTER_ID`, без Direct-fallback;
  креды инжектятся в `MetricaClient` (развязка шва `AuthManager`).
- `.env` живёт в per-game external storage, не в dev-репо и не коммитится.
- Отсутствие токена/счётчика → понятная ошибка ДО сетевых вызовов (fail-loud).

### API & Communication Patterns

- **Вендоринг `MetricaClient`:** HTTP-плумбинг (rate-limit ≤30/s, ≤5000/day; retry backoff на
  429/500/502/503) + **все методы Logs API** + лёгкие info-методы (`get_counter_info`,
  `get_counters`, `get_goals`). **Вырезаем:** агрегатный Stat/Reporting API (+`polars`),
  `upload_offline_conversions`, Direct-специфику. → клиент чисто на `requests`.
- **Logs API цикл:** create → poll до `processed` → download parts → clean; `date2` clamp на
  «вчера по МСК».
- **CLI = канал действий** (stdlib `argparse`, класс с `_create_parser`, как directaiq).
  Полная поверхность Logs API CLI: высокоуровневая команда обновления + подкоманды жизненного
  цикла (create/status/download/clean/evaluate/list) + info-подкоманды. Неинтерактивно, `--help`,
  AI-native.
- **MCP `duckdb_query` = канал чтения** (офиц. `mcp` SDK, FastMCP). Лёгкая доработка: заменить
  `_COST_COLUMN_SEMANTICS` семантикой из нашего каталога + нейтрализовать regex-fallback
  `(cost|.*_revenue)`; убрать goal-плейсхолдеры и завязку на `config_manager`; единый интерфейс
  инструмента и сервисные команды (--context/--tables/--schema/--sample/--export) сохраняются.
- **Обработка ошибок:** сверка строк → жёсткий fail (FR-13); пропуск дней по мета+факту;
  clamp/битые ячейки `TRY_CAST` → лог, не падение.

### Frontend Architecture

- N/A — UI нет. Оператор юнита — агент (Claude Code) через каналы CLI + MCP.

### Infrastructure & Deployment

- **Двух-репо:** dev-репо (инструменты) ↔ per-game external storage (data + config + .env +
  рабочая папка). **Реальные symlinks** по декларативному контракту + **preflight-проверка**
  способности создавать symlink (на Windows — Developer Mode), fail-loud с инструкцией.
- **Init-команда — на Python** (кросс-платформенно Win↔Linux): проверка имени → копирование
  шаблона хранилища → симлинки по контракту → генерация `.env` → подготовка окружения/зависимостей
  (`uv`) → создание DuckDB + view'ы/схема из каталога → `git init`. Имя занято → fail-loud.
- **Один писатель:** файловый лок (`.writer.lock`) на уровне хранилища, fail-fast; чтение (MCP)
  лока не берёт.
- **Атомарность дня:** запись во временный файл → атомарный rename в партицию.
- **Переносимость:** ноль серверных процессов (файлы + встроенный DuckDB); папка копируется
  Win↔Linux.
- **Логирование:** stdlib `logging`. **CI:** GitHub Actions + `pytest`.
- **Не входит:** queue/worker, disk-guard, cron, мульти-аккаунт.

### Scalability & Revisit Triggers

- **Признанный риск (владелец):** реальный объём данных может превысить оценку «единицы–десятки
  МБ/мес».
- **Почему выбор view это не усугубляет** (страховки развязаны с материализацией):
  - партиционирование по дню → partition pruning (запрос за период читает только нужные дни);
  - DuckDB проталкивает фильтры/проекции в чтение Parquet → быстро и на гигабайтах;
  - view → таблицы = `CREATE TABLE AS SELECT`, без изменения сырья/приёма/каталога (не one-way);
  - escape hatch (OQ#5): те же Parquet заливаются в ClickHouse — не переписывание приёма.
- **Встроенный чекпойнт:** замерить вес сырья на первой реальной выгрузке (SM-4); тогда же
  зафиксировать «порог тревоги» (OQ#5).
- **Revisit-триггер:** если у конкретной игры латентность запросов рабочего слоя станет заметной
  ИЛИ размер папки начнёт угрожать переносимости → материализовать view'ы этой игры в таблицы
  (точечно); ClickHouse — только при настоящем «взрыве».

### Decision Impact Analysis

**Implementation Sequence (ориентир):**
1. `uv`-каркас + раскладка проекта (детали — step-06).
2. Вендоринг `MetricaClient` (развязка шва) + env-ридер.
3. YAML-каталог схемы + заданный список полей (зависит от OQ#1).
4. Оркестратор приёма Parquet (per-day: атомарность, сверка строк, мета-состояние, writer-lock).
5. DuckDB-view'ы (генерация из каталога).
6. Инкремент + hot-window (N=3).
7. CLI-tool (`argparse`): update + lifecycle + info.
8. Доработка MCP `duckdb_query`.
9. Init на Python + симлинк-контракт + файл описания проекта.

**Cross-Component Dependencies:**
- Список полей (OQ#1) → каталог → view'ы/DDL → контекст MCP (цепочка единого источника).
- Каталог схемы — SSOT для типизации и семантики.
- Мета-состояние ↔ оркестратор ↔ атомарная запись — тесно связаны (защита базы).
- Init зависит от шаблона хранилища + симлинк-контракта + каталога (DDL).

## Implementation Patterns & Consistency Rules

### Pattern Categories Defined

**Critical Conflict Points Identified:** 9 областей, где AI-агенты могли бы решить по-разному
(имена полей, раскладка партиций, объекты DuckDB, форма каталога, дата/таймзона, правила типизации,
ошибки/коды, Python-конвенции, протокол идемпотентности).

### Naming Patterns

**Поля и колонки (storage):**
- **snake_case ВЕЗДЕ** (Parquet + working-view): `visit_id`, `client_id`, `watch_id`, `is_new_user`,
  `visit_duration`, `page_views`, `start_url`, `referer`, `bounce`, `region_city`, `device_category`,
  `operating_system`, `utm_source`/`utm_medium`/`utm_campaign`/`utm_content`, `watch_ids`, `date`,
  `date_time` и т.д.
- В Parquet **значения — строками, как пришли** (без CAST, без усечения). Единственное преобразование
  на входе — lossless-переименование колонок по каталогу.
- **Каталог хранит для каждого поля:** `metrica_field` (напр. `ym:s:visitID`) · `storage_name`
  (`visit_id`) · `source` (visits|hits) · `working_type` (HUGEINT/DATE/…) · `description`.
- Осознанное отличие от directaiq (держал `ym:s:*` в таблицах) — у нас storage-имена snake_case,
  родное имя Метрики живёт в каталоге.

**Parquet-партиции:** `data/raw/{source}/{YYYY-MM-DD}.parquet` (один файл = один день одного
источника; `source ∈ {visits, hits}`). Запись через temp: `…/{YYYY-MM-DD}.parquet.tmp` → атомарный
rename.

**Объекты DuckDB:** view по имени источника — `visits`, `hits`; мета-таблица — `load_state`.
Имена snake_case.

**Python-код:** модули snake_case; CLI-tool = `{name}_cli.py` + класс с
`_create_parser() -> argparse.ArgumentParser` (как directaiq); функции/переменные snake_case; классы
CapWords; type hints обязательны (mypy, как directaiq).

### Structure Patterns

(детальная раскладка — step-06)
- `tests/` зеркалят структуру `src/`; запуск `pytest`.
- Вендоренный код — в выделенном модуле (напр. `…/metrica/client.py`) с шапкой-пометкой
  «vendored from directaiq @ <ref>, seam: creds injected» и развязанным конструктором.
- Оркестратор приёма, CLI-tool'ы, MCP-сервер, каталог, env-ридер — отдельные модули.

### Format Patterns

**Каталог схемы — источник и сидинг типов:**
- **Источник полей и типов — официальный справочник Logs API Метрики:**
  `…/metrika-api/yandex.ru_dev_metrika_ru_logs_fields_visits.md` и `…_hits.md` (таблица
  Поле · Тип данных · Описание). Это поля **Logs API**, НЕ отчётного API. Файлы кладутся в проект.
- Типы в справочнике — **ClickHouse**; `working_type` каталога = маппинг ClickHouse→DuckDB
  (не угадывается):

  | ClickHouse | DuckDB `working_type` |
  |---|---|
  | `UInt64` | HUGEINT (UInt64 > 2^63, не влезает в BIGINT) |
  | `UInt32` | BIGINT |
  | `Int32` | INTEGER |
  | `Int64` | BIGINT |
  | `UInt8` (флаг 0/1) | BOOLEAN |
  | `Date` | DATE |
  | `DateTime` | TIMESTAMP |
  | `String` | VARCHAR |
  | `Array(T)` | `LIST<T>` |

- **HUGEINT** для `visit_id`/`client_id`/`watch_id` обоснован справочником (`UInt64`).
- **Массивы** (`watch_ids`=Array(UInt64), `goals_id`=Array(UInt32), …) → в TSV приходят строкой,
  во view парсятся в DuckDB `LIST`.

**Даты/время:** формат `YYYY-MM-DD` везде (как Logs API `date1/date2`); таймзона **МСК** для clamp
«вчера». `date_time` → `TIMESTAMP`.
**TSV-парсинг:** разделитель — tab; без молчаливого усечения; пустые/битые значения сохраняются как
есть в сырьё.
**Типизация (working-view, `TRY_CAST`):** по `working_type` каталога; битая ячейка → `NULL` + лог
(день не падает). CAST в сырьевом слое запрещён.
**Каталог:** CSV (`schema-catalog.csv`), строка на поле; машинно-парсимо; единый источник для DDL view и
семантики MCP.
**CLI-вывод:** `json|markdown|csv` (параметр формата; MCP `duckdb_query` — так же).
**Логи:** stdlib `logging`, уровни INFO/WARNING/ERROR; диагностика clamp/битых ячеек/сверки.

### Communication & Process Patterns

**Коды возврата / ошибки:** успех → `0`; любой fail (сверка не сошлась, нет кредов, API failed) →
non-zero + понятное сообщение. **Сверка строк → исключение/non-zero, НЕ warning** (FR-13, осознанное
отличие от directaiq).
**Retry / rate-limit:** только из вендоренного клиента; в оркестраторе **не реализовывать заново**.
**Протокол идемпотентного дня:** download parts → собрать день → запись в `.tmp` → сверка строк →
**атомарный rename** → запись `load_state`. День «загружен» ТОЛЬКО после rename + сверка + мета.
Перезалив дня = перезапись одного файла (без `DROP`).
**Реконсиляция на старте:** по каждому дню сверить мета × факт партиции; расхождение → день
незагружен → перелить, мета привести к факту (источник истины — факт партиции).
**Один писатель:** эксклюзивный `.writer.lock` перед любой записью; занят → fail-fast. Чтение (MCP)
лок не берёт.
**Poll Logs API:** интервал ~30s, верхняя граница ожидания, лимит подряд-ошибок → fail с
диагностикой (значения — в конфиге).

### Enforcement Guidelines

**All AI Agents MUST:**
- Каталог = SSOT: новое поле → сначала запись в каталог (поле без записи = дефект); `working_type`
  сидится из справочника Logs API (маппинг ClickHouse→DuckDB), не угадывается.
- storage-имена строго snake_case; значения сырья — без CAST/усечения.
- Сверка строк = fail (не warning); любая запись — через temp→rename + `.writer.lock`.
- Не реализовывать retry/rate-limit заново; не тащить инфраструктуру directaiq (queue/disk-guard/
  cron/мульти-аккаунт/BaseScript/config_manager).

**Pattern Enforcement:** `pytest` + `mypy` в CI; правило каталога проверяется (все поля заданного
списка покрыты). Обновление конвенции → правка этого раздела + каталога.

### Pattern Examples

**Good:** `SELECT visit_id, date FROM visits WHERE date >= today() - 30` (чистый SQL по view).
**Anti-patterns:**
- `SELECT "ym:s:visitID" FROM …` — родные имена в SQL агента;
- CAST в сырьевом слое; сверка как `warning`; `DROP TABLE` ради перезалива дня;
- реимплементация retry/rate-limit; запись в БД без `.writer.lock`;
- угадывание типов вместо маппинга из справочника Logs API.

## Project Structure & Boundaries

> Структура намеренно повторяет каркас directaiq (тренированная навигация владельца). Код — под
> `scripts/`. Осознанные отличия от directaiq отмечены `[—]` (не тащим) и `[новое]` (наша Parquet-модель).

### directaiq → наш проект (карта соответствия)

| directaiq | наш проект | что меняется |
|---|---|---|
| `scripts/utils/metrica_client.py` | `scripts/utils/metrica_client.py` | вендорим (Logs API + info; requests-only; шов развязан) |
| `scripts/utils/auth_manager.py` | `scripts/utils/env_reader.py` | тонкий ридер вместо AuthManager |
| `scripts/utils/database_manager.py` | `scripts/utils/database_manager.py` | соединение DuckDB (упрощено) |
| `scripts/utils/paths.py` · `logging_utils.py` | те же | как есть |
| `scripts/8x_metrica_logs_api/p81_load_logs.py` | `scripts/8x_metrica_logs_api/p81_load_logs.py` | **своя** оркестрация (Parquet, не DuckDB-CAST) |
| `scripts/tools/logs_api_cli.py` | `scripts/tools/logs_api_cli.py` | та же форма (argparse + `_create_parser`) |
| `scripts/mcp/directaiq_mcp_server.py` + `tools/core.py` | `scripts/mcp/gdau_mcp_server.py` + `tools/core.py` | вендорим + доработка |
| `scripts/nushell/init_project.nu` | `scripts/init/init_project.py` | init на **Python** (не nushell) |
| `templates/external_storage/` | `templates/external_storage/` | тот же паттерн, урезан |
| `templates/paths-to-symlink.csv` | `templates/paths-to-symlink.csv` | **тот же** CSV-контракт |
| `development-docs/data-architecture.md` | `development-docs/schema-catalog.csv` (+ генерируемый `data-architecture.md`) | машинный YAML-каталог = SSOT |
| `yandex-docs/metrika-api/` | `yandex-docs/metrika-api/` | справочники Logs API (visits/hits `.md`) |
| `CLAUDE.md` · `pyproject.toml` · `uv.lock` · `.mcp.json` | те же | корень |
| `activate.sh` · `toolkit.nu` | `[—]` | не нужны: `uv run` кросс-платформенно |
| `queue_cli` · disk-guard · cron · `BaseScript` · `config_manager` | `[—]` | не тащим (простота) |

### Complete Project Directory Structure

**Dev-репо (`gamedev-analytics-unit/`) — инструменты, один на все игры:**

```
gamedev-analytics-unit/
├── CLAUDE.md  pyproject.toml  uv.lock  .python-version  .mcp.json  .gitignore  CHANGELOG.md
├── .github/workflows/tests.yml         # CI: uv + pytest + mypy
├── scripts/
│   ├── utils/
│   │   ├── metrica_client.py           # вендорим: Logs API + info, requests-only, креды инжектятся
│   │   ├── env_reader.py               # YANDEX_METRICA_TOKEN + _COUNTER_ID; без Direct-fallback
│   │   ├── database_manager.py         # контекст-менеджер DuckDB (read_only / write)
│   │   ├── catalog.py                  # загрузка schema-catalog.csv + маппинг ClickHouse→DuckDB
│   │   ├── parquet_store.py   [новое]  # запись дня temp→rename; data/raw/{source}/{date}.parquet
│   │   ├── views.py           [новое]  # DDL view'ов из каталога (TRY_CAST)
│   │   ├── load_state.py      [новое]  # мета-таблица load_state + реконсиляция мета×факт
│   │   ├── writer_lock.py     [новое]  # .writer.lock, fail-fast
│   │   ├── paths.py                    # резолюция путей хранилища (env DATA_ROOT)
│   │   ├── dates.py                    # clamp date2 «вчера по МСК», формат YYYY-MM-DD
│   │   └── logging_utils.py            # stdlib logging
│   ├── 8x_metrica_logs_api/
│   │   └── p81_load_logs.py            # ОРКЕСТРАТОР: create→poll→download→parquet→сверка→meta; hot-window (N=3)
│   ├── tools/
│   │   └── logs_api_cli.py             # argparse: update|create|status|download|clean|evaluate|list|info
│   ├── mcp/
│   │   ├── gdau_mcp_server.py          # вендорим+доработка (FastMCP, офиц. mcp SDK)
│   │   └── tools/core.py               # ядро duckdb_query; семантика колонок из каталога
│   └── init/
│       └── init_project.py             # init-команда (Python): разворачивание per-game хранилища
├── templates/
│   ├── external_storage/               # шаблон хранилища
│   │   ├── .env.example                # YANDEX_METRICA_TOKEN= / YANDEX_METRICA_COUNTER_ID=
│   │   ├── .gitignore                  # игнор .env, data/, .writer.lock
│   │   ├── CLAUDE.md                   # инструкции агенту в рабочем пространстве игры
│   │   └── PROJECT.md                  # описание игры (project context, FR-21) — заполняет владелец
│   └── paths-to-symlink.csv            # декларативный симлинк-контракт (FR-20)
├── development-docs/
│   ├── schema-catalog.csv             # КАТАЛОГ СХЕМЫ — SSOT (metrica_field, storage_name, source,
│   │                                   #   working_type, description). Сидится из yandex-docs/
│   └── data-architecture.md            # человекочитаемая дока (генерируется из каталога)
├── yandex-docs/metrika-api/            # справочники Logs API (кладёт владелец)
│   ├── yandex.ru_dev_metrika_ru_logs_fields_visits.md
│   └── yandex.ru_dev_metrika_ru_logs_fields_hits.md
└── tests/                              # pytest, зеркалят scripts/
    ├── conftest.py · fixtures/         # мини-TSV, битые ячейки, шаблон хранилища
    ├── test_catalog.py · test_type_map.py
    ├── test_parquet_atomic.py · test_load_state.py · test_row_check.py
    ├── test_p81_orchestrator.py · test_hot_window.py · test_views.py
    ├── test_writer_lock.py · test_init_symlinks.py
```

**Per-game внешнее хранилище (`../{game}/`) — создаётся init-командой:**

```
../{game}/
├── .env                                # токен + counter_id (НЕ коммитится)
├── PROJECT.md                          # описание игры (контекст агента)
├── .writer.lock                        # лок одного писателя (во время записи)
├── data/
│   ├── raw/{visits,hits}/{date}.parquet   # сырьё, партиции по дню
│   └── duckdb/gdau.duckdb              # view'ы (поверх ../raw) + load_state
├── scripts            → симлинк → dev-репо/scripts
├── development-docs   → симлинк → dev-репо/development-docs  (каталог схемы)
├── yandex-docs        → симлинк → dev-репо/yandex-docs
├── .mcp.json          → симлинк → dev-репо/.mcp.json
├── pyproject.toml     → симлинк → dev-репо/pyproject.toml
└── .claude/           → симлинк(и) → dev-репо (команды/настройки агента)
```

**Entry points (`pyproject.toml`):** `gdau-logs = scripts.tools.logs_api_cli:main`;
`gdau-init = scripts.init.init_project:main`; MCP — через `.mcp.json` (`python -m scripts.mcp.gdau_mcp_server`).
Импорты в стиле directaiq: `from scripts.utils.metrica_client import MetricaClient`.

### Architectural Boundaries

- **Внешний API:** единственная точка HTTP к Logs API — `scripts/utils/metrica_client.py`.
- **Dev-репо ↔ хранилище:** код/каталог/справочники — в dev-репо (приходят симлинками); данные/`.env`/
  рабочая папка — в хранилище. Резолюция через `DATA_ROOT` + `paths.py`. В dev-репо данные не пишутся.
- **Запись ↔ чтение:** запись (p81) — `.writer.lock` + атомарный Parquet + write-conn; чтение (MCP) —
  read-only, без лока. Каналы: CLI = действия/запись, MCP = чтение/анализ.
- **Данные:** сырьё Parquet (источник истины, строки) ↔ working-view'ы (типизированы); контракт —
  каталог (`schema-catalog.csv`).

### Requirements to Structure Mapping

| FR | Где живёт |
|---|---|
| FR-1…3 (Logs API цикл, поля, visits+hits) | `utils/metrica_client.py`, `8x_metrica_logs_api/p81_load_logs.py`, `schema-catalog.csv` |
| FR-4 креды · FR-5 clamp | `utils/env_reader.py` · `utils/dates.py` |
| FR-6 Parquet · FR-7 view TRY_CAST | `utils/parquet_store.py` · `utils/views.py` + каталог |
| FR-8 переносимость | `utils/database_manager.py`, `utils/paths.py` (ноль сервера) |
| FR-9 инкремент · FR-10 перезалив дня | `p81_load_logs.py` · `utils/parquet_store.py` (temp→rename) |
| FR-11 hot-window · FR-12 мета · FR-13 сверка · FR-14 атомарность · FR-15 лок | `p81_load_logs.py`, `utils/load_state.py`, `utils/parquet_store.py`, `utils/writer_lock.py` |
| FR-16 каталог | `development-docs/schema-catalog.csv`, `utils/catalog.py` |
| FR-17/18 MCP | `scripts/mcp/gdau_mcp_server.py`, `tools/core.py` |
| FR-19 init · FR-20 симлинки · FR-21 контекст | `scripts/init/init_project.py`, `templates/paths-to-symlink.csv`, `templates/external_storage/PROJECT.md` |

### Integration Points & Data Flow

- **Приём (write):** `gdau-logs update --date1 --date2 --source` → p81 берёт `.writer.lock` →
  `MetricaClient` (create→poll→download TSV) → `parquet_store` пишет день в `.tmp` → сверка строк →
  атомарный rename → `load_state`. Hot-window перезаливает N последних дней. View'ы отражают сразу.
- **Запрос (read):** Claude Code → MCP `duckdb_query(sql, format, limit)` → read-only DuckDB → view'ы
  поверх Parquet → результат (json/md/csv; большой → файл-экспорт).
- **Init:** `gdau-init {game}` → проверка имени → копирование шаблона → симлинки по CSV (+ preflight
  Dev Mode) → генерация `.env` → `uv sync` → создание `gdau.duckdb` + view'ы из каталога → `git init`.

### Conscious Divergences from directaiq

- **Нет** `activate.sh`/`toolkit.nu` — окружение через `uv run` (кросс-платформенно, без bash/nushell).
- **Нет** `queue_cli`/disk-guard/cron/`BaseScript`/`config_manager` — простота для одного оператора.
- init на Python (не nushell); storage-имена snake_case; сырьё Parquet (не CSV-в-DuckDB).

## Architecture Validation Results

### Coherence Validation ✅

**Decision Compatibility:** Стек взаимно совместим — Python `>=3.13`, `uv`+lock, DuckDB 1.5.x,
`requests`, офиц. `mcp` SDK (FastMCP, `mcp>=1.2`), `argparse` (stdlib). Версии проверены по вебу
(2026-05-23). Противоречий нет: сырьё-строки + `TRY_CAST` во view, HUGEINT/массивы типизируются во
view, реальные symlinks совместимы с переносимостью (Linux нативно, Windows — Dev Mode + preflight).

**Pattern Consistency:** Паттерны поддерживают решения — snake_case storage-имена, `argparse` +
`_create_parser`, каталог YAML как SSOT для view-DDL и семантики MCP, протокол идемпотентности
(temp→rename + сверка + мета) обслуживает NFR целостности.

**Structure Alignment:** Структура (каркас directaiq) поддерживает все решения: `scripts/utils`
примитивы, `8x_metrica_logs_api/p81` оркестратор, `tools/logs_api_cli.py` CLI, `mcp/` чтение,
`init/` + `paths-to-symlink.csv` двух-репо, `development-docs/schema-catalog.csv` SSOT.

### Requirements Coverage Validation ✅

**Functional Requirements Coverage:** Все 21 FR имеют архитектурный дом (см. таблицу FR→структура).
Источники visits+hits (FR-3) и связь watch_ids↔watch_id — через каталог/типизацию массивов.

**Non-Functional Requirements Coverage:**
- Целостность данных — атомарность (`parquet_store`), сверка-fail (`p81`), лок (`writer_lock`),
  реконсиляция (`load_state`). ✅
- Переносимость Win↔Linux — ноль серверных процессов, копирование папки, `uv run`. ✅
- Лимиты Logs API — rate-limit/retry из вендоренного клиента. ✅
- Корректность типов — HUGEINT для UInt64 (обоснован справочником). ✅
- Безопасность кредов — `.env` в хранилище, не в dev-репо, fail-loud до сети. ✅

### Implementation Readiness Validation ✅

**Decision Completeness:** Критические решения задокументированы с версиями; развилки (init-язык,
CLI-фреймворк, материализация, symlinks, объём API) закрыты владельцем.
**Structure Completeness:** Полное дерево dev-репо и per-game хранилища; границы и точки интеграции
определены; FR→структура полна.
**Pattern Completeness:** 9 точек расхождения покрыты; именование/формат/процесс заданы с примерами
и анти-паттернами.

### Gap Analysis Results

**Critical Gaps:** нет (архитектура не блокирована).

**Important Gaps:**
- **OQ#1 — точный список полей visits/hits не зафиксирован.** Это продуктовый вход (не дефект
  архитектуры): каталог/view/MCP его принимают. **Гейтит данные-эпики** (3→далее). Закрывается по
  `yandex-docs/.../logs_fields_*.md` (фильтр под геймдев, типы из справочника).

**Minor Gaps (impl-time, не блокируют):**
- Точный состав `templates/paths-to-symlink.csv` — финализировать при сборке init.
- Функция парсинга TSV-массивов (`Array(T)` → `LIST`) — деталь реализации view.
- OQ#4 residual: финальный список правок `mcp/tools/core.py` (снятие `config_manager`/плейсхолдеров).
- Windows Developer Mode не проверен на машине — поймает preflight init.
- OQ#5 (порог DuckDB→ClickHouse) — отложен по дизайну (замер на первой выгрузке, SM-4).

### Validation Issues Addressed

OQ#1 классифицирован как pre-impl вход и явно гейтит фазу данных (зафиксировано в Decision Priority
Analysis). Остальные пункты — impl-time детали с понятным владельцем; ни один не меняет архитектурных
решений.

### Architecture Completeness Checklist

**Requirements Analysis**
- [x] Project context thoroughly analyzed
- [x] Scale and complexity assessed
- [x] Technical constraints identified
- [x] Cross-cutting concerns mapped

**Architectural Decisions**
- [x] Critical decisions documented with versions
- [x] Technology stack fully specified
- [x] Integration patterns defined
- [x] Performance considerations addressed

**Implementation Patterns**
- [x] Naming conventions established
- [x] Structure patterns defined
- [x] Communication patterns specified
- [x] Process patterns documented

**Project Structure**
- [x] Complete directory structure defined
- [x] Component boundaries established
- [x] Integration points mapped
- [x] Requirements to structure mapping complete

### Architecture Readiness Assessment

**Overall Status:** READY FOR IMPLEMENTATION — *с оговоркой: данные-эпики начинать после фиксации
списка полей (OQ#1); каркасные эпики (init, вендоринг клиента, оболочка MCP, CLI) можно начинать сразу.*

**Confidence Level:** high — все 16 пунктов чек-листа подтверждены, критических дыр нет; единственный
важный вход (OQ#1) — продуктовый, с понятным источником.

**Key Strengths:**
- Принцип «простота-первой» проведён последовательно (не тащим тяжёлую инфру directaiq).
- Целостность данных как NFR первого класса полностью обеспечена (атомарность/сверка/лок/реконсиляция).
- Единый источник истины (каталог) убирает рассинхрон поле↔тип↔семантика.
- Каркас узнаваем (directaiq) — низкая когнитивная нагрузка при разработке.
- Развязка швов вендоринга выявлена заранее (AuthManager, config_manager, polars/reporting).

**Areas for Future Enhancement:**
- Материализация view→таблицы и/или ClickHouse при росте объёма (revisit-триггеры, OQ#5).
- Аналитические плейбуки/регламенты агента (v2+).
- Тонкая Reports API команда для cross-check (если появится нужда).

### Implementation Handoff

**AI Agent Guidelines:**
- Следовать решениям и паттернам этого документа дословно; storage-имена snake_case; каталог = SSOT.
- Запись — только через `.writer.lock` + temp→rename; сверка строк = fail; retry/rate-limit не
  реализовывать заново.
- Не тащить инфраструктуру directaiq; уважать границу dev-репо ↔ хранилище.

**First Implementation Priority:**
1. `uv`-каркас + раскладка `scripts/` + entry points.
2. Вендоринг `MetricaClient` (развязка шва, обрезка reporting/polars) + `env_reader`.
3. **Зафиксировать OQ#1** (список полей по `yandex-docs`) → наполнить `schema-catalog.csv`.
4. Дальше — оркестратор p81, view'ы, CLI, MCP, init (по Implementation Sequence).
