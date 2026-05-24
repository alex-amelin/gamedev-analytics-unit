---
project_name: 'gamedev-analytics-unit'
user_name: 'Шеф'
date: '2026-05-23'
sections_completed: ['technology_stack', 'language_specific', 'component_docs', 'data_domain', 'testing', 'code_quality', 'workflow', 'critical_dont_miss']
existing_patterns_found: 9
status: 'complete'
rule_count: 80
optimized_for_llm: true
---

# Project Context for AI Agents

_This file contains critical rules and patterns that AI agents must follow when implementing code in this project. Focus on unobvious details that agents might otherwise miss._

---

## Technology Stack & Versions

**Runtime / сборка:** Python `>=3.13` (`.python-version` → 3.13) · `uv` (+ `uv.lock` — источник истины версий) · сборка `hatchling`.

> `[tool.hatch.build.targets.wheel] packages = ["scripts"]` — пакуется каталог `scripts/`, НЕ пакет по имени проекта. Поэтому `import scripts.utils.*` резолвится и под `uv run`, и в установленном wheel. Не переводить на src-layout, не переименовывать пакет.

**Runtime-зависимости (точные пины — в `uv.lock`):**
- `duckdb >=1.5,<1.6` — встроенный движок рабочего слоя (ноль серверных процессов)
- `mcp >=1.2` — официальный SDK; используем `mcp.server.fastmcp.FastMCP`, НЕ отдельный пакет `fastmcp` 3.x (другая архитектура)
- `requests >=2.31` — единственный HTTP-клиент (Logs API)
- `python-dotenv >=1.0` · `pyyaml >=6.0`

**Dev-зависимости:** `mypy >=1.0` (`strict = true`, `python_version = "3.13"`, `explicit_package_bases`, `namespace_packages`) · `pytest >=8`.

**Запуск — всё через `uv run` (кросс-платформенно, без bash/nushell):**
- `uv sync` (локально) / `uv sync --frozen` (CI) — окружение строго по локу
- `uv run pytest` · `uv run mypy scripts`
- `uv run gdau-logs` (приём Logs API) · `uv run gdau-init` (разворачивание per-game хранилища)

**Не тянуть:** аналитический стек (`pandas`/`numpy`/`scipy`/`polars`/`prophet`), `tapi-yandex-metrika`, отдельный `fastmcp`. Новая зависимость = осознанное решение против принципа «простота-первой».

## Critical Implementation Rules

### Language-Specific Rules (Python)

- **`from __future__ import annotations`** — первой строкой кода в каждом модуле (так в каждом существующем файле; нужно для строковых аннотаций без рантайм-импортов).
- **Type hints обязательны везде** — `mypy --strict` гоняется в CI по `scripts`. Любая функция (включая `main() -> None`) аннотируется полностью; без `Any`-дыр.
- **Импорты — абсолютные, от корня пакета:** `from scripts.utils.metrica_client import MetricaClient`. Не относительные (`from ..utils import`). Каждый каталог под `scripts/` — пакет с `__init__.py`.
- **CLI только на stdlib `argparse`** (ноль зависимостей): CLI-модуль = `{name}_cli.py`, парсер в методе `_create_parser() -> argparse.ArgumentParser` (форма directaiq). Не вводить `Typer`/`Click`.
- **Точка входа:** `def main() -> None`; модуль завершается блоком `if __name__ == "__main__": main()`. Успех → код `0`; любой fail → non-zero + понятное сообщение.
- **Логирование — только stdlib `logging`** (уровни INFO/WARNING/ERROR). Не `print` для диагностики (текущие стабы печатают — это временно, до реальной реализации).
- **Docstrings — на русском**, модульный docstring обязателен; кратко описывает роль модуля (как в существующих файлах). Идентификаторы — на английском.
- **Именование:** модули/функции/переменные — `snake_case`; классы — `CapWords`.

### Документация компонентов (`docs/`)

На каждый **логический компонент** агент заводит и поддерживает спеку `docs/<component>.md`, написанную **человеческим языком, без технического жаргона** — так, чтобы её понял владелец, не читая код.

**Гранулярность — логический компонент, не модуль.** Мелкие хелперы (`dates`, `paths`, `logging_utils`) описываются внутри родственной спеки, а не отдельным файлом. Ориентир состава:

- `catalog.md` — каталог схемы как единый источник истины
- `ingestion.md` — приём: `metrica_client` + оркестратор `p81` + `parquet_store` + `load_state`
- `working-layer.md` — рабочий слой: view'ы + `database_manager` + типизация
- `mcp-query.md` — чтение через MCP `duckdb_query`
- `cli.md` — поверхность `gdau-logs`
- `init-and-storage.md` — init + симлинки + два-репо

**Каждая спека отвечает на три вопроса простыми словами:**

1. **Что делает** — что компонент принимает на вход и что отдаёт.
2. **Зачем нужен** — какую задачу решает, почему он вообще есть.
3. **Контракт с другими** — с кем взаимодействует, что ждёт от них и что обещает им (входы/выходы, формат, инварианты — словами, не сигнатурами кода).

**Правило обновления — часть Definition of Done:**

- Новый компонент → в том же изменении завести `docs/<component>.md`.
- Меняется контракт (входы/выходы, формат, инварианты) → обновить спеку в том же изменении. Код и спека расходиться не должны.
- Проверяется при ревью истории: компонент без актуальной спеки не считается «готовым».

**Граница с другими доками:** `docs/` — человекочитаемые спеки компонентов; `development-docs/` — машинный каталог схемы (SSOT) + генерируемая `data-architecture.md`; `yandex-docs/` — справочники Logs API. Не смешивать.

### Data & Domain Architecture Rules

**Каталог схемы = единственный источник истины (SSOT).**

- Файл `development-docs/schema-catalog.csv`, колонки: `source, storage_name, metrica_field, type, description`.
- Поле без записи в каталоге = дефект. Новое поле → СНАЧАЛА строка в каталоге, потом код.
- Из каталога генерируются DDL working-view и семантика колонок для MCP — не дублировать руками.
- `type` (DuckDB) сидится из официального справочника Logs API маппингом ClickHouse→DuckDB, НЕ угадывается:

  | ClickHouse | DuckDB |
  |---|---|
  | `UInt64` | HUGEINT (UInt64 > 2^63, в BIGINT не влезает) |
  | `UInt32` | BIGINT · `Int64` → BIGINT · `Int32` → INTEGER |
  | `UInt8` (флаг 0/1) | BOOLEAN |
  | `Date` → DATE · `DateTime` → TIMESTAMP · `String` → VARCHAR |
  | `Array(T)` | `LIST<T>` (в CSV пишем как `HUGEINT[]` и т.п.) |

**Именование и слои.**

- storage-имена строго `snake_case` (`visit_id`, `client_id`, `watch_ids`). Родное имя Метрики (`ym:s:visitID`) живёт ТОЛЬКО в каталоге — не в SQL агента, не в таблицах, не в коде.
- **Сырьё (Parquet):** значения — строками, как пришли, без CAST и усечения. Единственное преобразование на входе — lossless-переименование колонок по каталогу.
- Раскладка: `data/raw/{source}/{YYYY-MM-DD}.parquet`, `source ∈ {visits, hits}`. Один файл = один день одного источника.
- **Рабочий слой:** DuckDB-**view'ы** поверх Parquet с `TRY_CAST` по `type` каталога. Битая ячейка → `NULL` + лог (день НЕ падает). CAST в сырьевом слое запрещён.
- Объекты DuckDB: view'ы `visits`, `hits`; мета-таблица `load_state`. Имена snake_case.
- Массивы (`watch_ids` и пр.) приходят в TSV строкой → парсятся в `LIST` во view, не в сырье.

**Целостность базы (NFR №1) — протокол идемпотентного дня.**

- Последовательность: download parts → собрать день → запись в `{date}.parquet.tmp` → **сверка строк** → **атомарный rename** → запись `load_state`. День «загружен» ТОЛЬКО после rename + сверка + мета.
- **Сверка строк источник↔БД = жёсткий fail** (исключение/non-zero), НЕ warning (осознанное отличие от directaiq).
- Перезалив дня = перезапись ОДНОГО файла. Никогда `DROP TABLE` ради перезалива.
- Реконсиляция на старте: по каждому дню сверить мета × факт партиции; источник истины — **факт партиции** (расхождение → день незагружен → перелить).
- **Один писатель:** эксклюзивный `.writer.lock` перед любой записью, fail-fast если занят. Чтение (MCP) лок НЕ берёт.

**Logs API и креды.**

- Единственная точка HTTP к Logs API — `scripts/utils/metrica_client.py`. Retry/rate-limit (≤30 req/s, ≤5000/day, backoff на 429/500/502/503) — ТОЛЬКО оттуда; в оркестраторе заново не реализовывать.
- Цикл: create → poll до `processed` → download parts → clean. `date2` clamp на «вчера по МСК». Формат дат `YYYY-MM-DD` везде; `date_time` → `TIMESTAMP`.
- Креды через `env_reader`: `YANDEX_METRICA_TOKEN` + `YANDEX_METRICA_COUNTER_ID`, без Direct-fallback; инжектятся в `MetricaClient` (развязка шва `AuthManager`). Нет токена/счётчика → понятная ошибка ДО сетевых вызовов.

**Границы и каналы.**

- Каналы агента: **CLI = действия / запись / жизненный цикл Logs API**; **MCP `duckdb_query` = только чтение / анализ**. Вывод обоих: `json|markdown|csv`.
- Граница dev-репо ↔ per-game хранилище: код/каталог/справочники — в dev-репо (приходят симлинками); данные/`.env`/рабочая папка — в хранилище. Резолюция путей через `GDAU_DATA_ROOT` + `paths.py`. **В dev-репо данные не пишутся.**
- Вендоринг: модуль с шапкой `vendored from directaiq @ <ref>, seam: creds injected`; обрезать reporting/`polars`/Direct в клиенте; в MCP снять `config_manager`, goal-плейсхолдеры и НДС-семантику `_COST_COLUMN_SEMANTICS` (заменить семантикой из каталога).
- **Не тащить инфру directaiq:** queue/worker, disk-guard, cron, `BaseScript`, `config_manager`.

### Testing Rules

**Два набора тестов — оба обязательны.**

_Offline (по умолчанию, в CI):_

- `pytest` через `uv run pytest`; файлы `test_*.py`, функции `test_*`; `tests/` зеркалит `scripts/`.
- Не ходят в сеть: HTTP/`MetricaClient` — через фикстуры/моки на мини-TSV из `tests/fixtures/` (в т.ч. битые ячейки).
- `tests/` всегда собирает ≥1 тест (pytest exit code 5 = красный в CI).
- Кросс-платформенность: CI гоняет ubuntu + windows — проходить на обеих. Пути через `pathlib`, без хардкода разделителей/абсолютных путей.
- Покрывать дисциплину целостности, не только happy path: атомарность (temp→rename), сверка-fail, реконсиляция мета×факт, `.writer.lock`, `TRY_CAST` (битая → NULL), парсинг TSV-массивов, hot-window, симлинки init.
- Правило каталога проверяется тестом: все поля покрыты записью в `schema-catalog.csv`, маппинг типов соответствует справочнику.

_Live smoke (против РЕАЛЬНОГО Logs API, opt-in) — обязателен:_

- Отдельный набор дёргает реальный Logs API. **Моки не отражают реальное поведение API** — контракт расходится незаметно (имена/типы полей, формат TSV, статусы асинхронного цикла).
- Маркер `@pytest.mark.live`; по умолчанию отключён (`addopts = "-m 'not live'"`), запуск явно: `uv run pytest -m live`. В стандартном CI не гоняется.
- Креды берёт из `.env` per-game хранилища; кредов нет → `pytest.skip` с понятной причиной (не ложный красный).
- Минимальный: узкое окно в 1 день, малый набор полей — уважать rate-limit (≤5000 req/day) и асинхронный цикл (poll ~30s).
- Проверяет реальный контракт: имена/типы полей из ответа совпадают с каталогом; формат TSV; цикл create→poll→processed→download→clean.
- **Фикстуры offline-набора освежать из реальных ответов live-прогона** — чтобы моки не расходились с API.

- `mypy --strict` нацелен на `scripts` (не на `tests`).

### Code Quality & Structure Rules

- **Структура репо намеренно зеркалит directaiq** (натренированная навигация владельца). Весь код — под `scripts/`. Не реорганизовывать раскладку без явной причины.
- **Раскладка по ответственности:** `scripts/utils/` — примитивы (клиент, `env_reader`, `catalog`, `parquet_store`, `views`, `load_state`, `writer_lock`, `paths`, `dates`, `logging_utils`); `scripts/8x_metrica_logs_api/p81_load_logs.py` — оркестратор; `scripts/tools/` — CLI; `scripts/mcp/` — чтение; `scripts/init/` — init. Один модуль = одна ответственность; не сливать примитивы в «утиль-помойку».
- **Вендоренный код — в выделенном модуле** с шапкой `vendored from directaiq @ <ref>, seam: …`. Развязка — только в обозначенных швах (creds-инжект, семантика из каталога); остальное вендоренное не «причёсывать» вразнобой, чтобы оставалось сравнимым с источником.
- **Комментарии объясняют «почему», не «что».** Неочевидные решения фиксировать прямо у кода (образец — комментарий про `hatchling packages = ["scripts"]` в `pyproject.toml`).
- **Форматтер/линтер в CI не закреплён** (только `mypy` + `pytest`). Держать PEP 8 и стиль существующих файлов; не прогонять ruff/black-переформатирование по всему репо без согласования.
- Новых зависимостей не добавлять без необходимости (см. «не тянуть» в стеке) — простота как инвариант.

### Development Workflow Rules

- **Новая история → новая ветка.** Каждая история ведётся в отдельной ветке от `main` (имя с id истории, напр. `story/1.1-<краткий-слаг>`); напрямую в `main` историю не коммитить. Merge в `main` — только после зелёного CI.
- **Коммиты — Conventional Commits, описание на русском** (как в истории репо: `feat:`, `docs:`, `chore(bmad):`). Привязка к истории BMAD в скобках: `feat: … (story 1.1)`.
- **`uv.lock` коммитится и авторитетен.** Зависимости меняются только через `uv add`/`uv lock`; обновлённый лок — в тот же коммит. CI ставит окружение `uv sync --frozen` — рассинхрон лока валит сборку.
- **CI зелёный обязателен перед merge в `main`** на обеих ОС (ubuntu + windows): `uv sync --frozen` → `mypy scripts` → `pytest`. PR в `main`.
- **Секреты не коммитятся никогда.** `.env`/`.env.*` в `.gitignore` (кроме `.env.example`); реальные креды живут только в `.env` per-game хранилища.
- **Артефакты данных не коммитятся:** `*.parquet`, `*.duckdb`, `*.duckdb.wal`, `data/`, `*.writer.lock` — в `.gitignore` (живут в хранилище, не в dev-репо).
- **Окончания строк — LF** для текстовых типов (`.gitattributes`, NFR переносимости Win↔Linux); бинарь (`*.duckdb`/`*.parquet`) не нормализуется. Не переключать `core.autocrlf` локально вопреки `.gitattributes`.
- **Два репозитория раздельны:** dev-репо (этот) versus per-game хранилище со своим `git init` (создаётся `gdau-init`). Данные не коммитятся ни там, ни там.
- **Работа по историям (BMAD):** планировочные/имплементационные артефакты — в `_bmad-output/`, коммитятся; статус спринта — `_bmad-output/implementation-artifacts/sprint-status.yaml`. Definition of Done истории включает актуальную `docs/<component>.md` (см. выше).

### Critical Don't-Miss Rules (anti-patterns)

**Никогда:**

- Родные имена Метрики в SQL/коде: `SELECT "ym:s:visitID" …`. Только storage snake_case через view.
- CAST в сырьевом слое или молчаливое усечение TSV. Сырьё — строки as-is; типизация только во view через `TRY_CAST` (битая → NULL + лог).
- Сверка строк как `warning`. Расхождение источник↔БД = жёсткий fail (non-zero).
- `DROP TABLE`/удаление партиции ради перезалива дня. Перезалив = перезапись одного `{date}.parquet` (temp→rename).
- Запись в БД/Parquet без захвата `.writer.lock`.
- Реимплементация retry/rate-limit вне вендоренного `MetricaClient`.
- Угадывание типа поля. `type` сидится маппингом ClickHouse→DuckDB из справочника Logs API.
- `BIGINT` для `UInt64`-полей (`visit_id`/`client_id`/`watch_id`) — переполнение; только `HUGEINT`.
- `date2 = today`. Всегда clamp на «вчера по МСК».
- Запись данных в dev-репо; коммит `.env`, `*.parquet`, `*.duckdb`, `data/`.
- Притаскивание инфры directaiq (queue/worker/disk-guard/cron/`BaseScript`/`config_manager`) или тяжёлых зависимостей (`pandas`/`polars`/…).
- Перевод на src-layout / переименование пакета `scripts` — ломает резолюцию импортов (см. `hatchling`).
- Полагаться только на моки для контракта API — обязателен live-smoke (моки расходятся с реальностью).
- Новое поле без записи в каталоге; новый/изменённый компонент без актуальной `docs/<component>.md`.
- Коммит истории напрямую в `main` (новая история → новая ветка).

**Безопасность:** нет токена/счётчика → понятная ошибка ДО сетевых вызовов (fail-loud); креды не логировать.

**Edge-кейсы цикла Logs API:** у poll — верхняя граница ожидания и лимит подряд-ошибок → fail с диагностикой (значения в конфиге), не вечный цикл. Preflight симлинков (Windows Dev Mode) — fail-loud с инструкцией.

---

## Usage Guidelines

**Для AI-агентов:**

- Прочитать этот файл ДО написания кода; следовать правилам дословно.
- В спорной ситуации выбирать более строгий вариант (особенно вокруг целостности базы и каталога-SSOT).
- При сомнении в типе/имени поля — смотреть `development-docs/schema-catalog.csv` и справочники `yandex-docs/`, не угадывать.
- Источники глубже этого файла: `_bmad-output/planning-artifacts/architecture.md` (полные решения), `prd.md` (требования). Этот файл — выжимка неочевидного.

**Для людей (Шеф):**

- Держать файл коротким и про неочевидное; очевидное со временем убирать.
- Обновлять при смене стека/паттернов и когда закрываются OQ (напр. финальный список правок MCP `core.py`).
- Целиться подключить файл из корневого `CLAUDE.md` (пока его нет), чтобы агенты подхватывали правила автоматически.

Last Updated: 2026-05-23
