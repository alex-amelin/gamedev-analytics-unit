# Story 1.1: `uv`-каркас, раскладка проекта и CI

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want воспроизводимый `uv`-каркас dev-репо с раскладкой `scripts/`, entry points и CI,
so that все остальные инструменты ложатся на стабильный фундамент со стеком, закреплённым локом.

**Контекст эпика:** Это **первая имплементационная история** проекта (Epic 1 «Каркас юнита и канал Logs API»). Сейчас в репозитории нет ни `pyproject.toml`, ни `scripts/`, ни тестов. Все последующие истории (env-ридер 1.2, вендоринг клиента 1.3, clamp дат 1.4, каталог 1.5, CLI 1.6, весь Epic 2/3/4) опираются на этот каркас. Если раскладка/пакетирование/CI сделаны неправильно — посыпется всё дерево импортов `from scripts.utils... import` и entry points `gdau-logs`/`gdau-init`. **Цель — не «что-то компилируется», а воспроизводимый фундамент, на котором следующие истории не спотыкаются об импорты, версии и переносимость.**

## Acceptance Criteria

1. **Given** инициализируемый репозиторий, **When** выполняется `uv init --package` (src-layout), **Then** создаются `pyproject.toml` + `uv.lock` + `.python-version` (пол `>=3.13`), **And** `uv sync` отрабатывает без ошибок.
2. **Given** pyproject, **When** смотрим зависимости, **Then** запинены ровно `duckdb` (1.5.x), `requests`, `mcp` (≥1.2), `python-dotenv`, `PyYAML`, **And** аналитический стек (pandas/numpy/scipy/numba/prophet/polars) и `tapi-yandex-*` отсутствуют.
3. **Given** раскладку, **When** смотрим дерево, **Then** существуют `scripts/{utils,8x_metrica_logs_api,tools,mcp,init}/`, `tests/`, `templates/`, `development-docs/`, `yandex-docs/metrika-api/` (по карте соответствия архитектуры).
4. **Given** `[project.scripts]`, **When** смотрим entry points, **Then** объявлены `gdau-logs` и `gdau-init` (допустимы заглушки `main`).
5. **Given** раскладку с пакетным `uv` (src-layout), **When** импортируется `scripts.utils.*`, **Then** импорт резолвится (пакетная конфигурация настроена так, что `scripts/` импортируется как в directaiq) — `uv run python -c "import scripts.utils"` проходит. _[edge-case: src-layout vs `from scripts...`]_
6. **Given** CI-конфиг `.github/workflows/tests.yml`, **When** он запускается, **Then** матрица гоняет `uv sync` + `pytest` + `mypy` и на `ubuntu-latest`, и на `windows-latest` (переносимость NFR-2 проверяется на обеих ОС), **And** проходит зелёным на пустом наборе тестов. _[edge-case: Linux-only CI пропустит баги переносимости]_
7. **Given** корневой `.gitignore`, **When** смотрим, **Then** игнорируются `.env`, `data/`, `*.writer.lock`. _[edge-case: секреты/локальные данные в коммит]_

## Tasks / Subtasks

- [x] **Task 1 — Инициализировать `uv`-проект как пакет (AC: #1, #4)**
  - [x] Перед `uv init` убедиться, что активный интерпретатор ≥3.13 (`uv python pin 3.13` поднимет/закрепит нужный Python вне зависимости от того, что стоит локально). _[edge-case #3: пин зависит от локального Python]_
  - [x] В корне репо выполнить `uv init --package --name gamedev-analytics-unit` (репо уже содержит `.git`, `.gitignore`, `_bmad/`, `development-docs/`).
  - [x] **Пост-проверка непустого репо** _[edge-case #8]_: существующий `.gitignore` НЕ перезаписан (см. его текущее содержимое в Dev Notes); `pyproject.toml` создан; сгенерирован `src/gamedev_analytics_unit/` (удалим в Task 3); в корне нет лишних файлов сверх ожидаемых (`README.md`, `.python-version`). Если `uv init` отказался/повёл себя иначе — НЕ форсить, разобраться (возможно, конфликт с существующим файлом).
  - [x] Проверить, что созданы `pyproject.toml`, `.python-version`, `README.md` (lock появляется после `uv sync`/`uv add`).
  - [x] Зафиксировать `requires-python = ">=3.13"` и проверить, что `.python-version` пинит линию ≥3.13 (не ниже пола). _[edge-case #3]_
- [x] **Task 2 — Запинить зависимости v1 (AC: #2)**
  - [x] `uv add "duckdb>=1.5,<1.6" requests "mcp>=1.2" python-dotenv PyYAML` (генерирует/обновляет `uv.lock`).
  - [x] Добавить dev-инструменты: `uv add --dev pytest mypy`.
  - [x] Убедиться, что в `[project.dependencies]` **нет** `pandas/numpy/scipy/numba/prophet/polars/tapi-yandex-*` (анти-список из NFR-6).
- [x] **Task 3 — Перенацелить пакет с `src/` на `scripts/` (AC: #4, #5)** — *ядро истории, см. Dev Notes → «Пакетирование»*
  - [x] Удалить сгенерированный `src/gamedev_analytics_unit/`.
  - [x] **Вычистить хвосты, ссылающиеся на `src/`** _[edge-case #2]_: если `uv init` задал `dynamic = ["version"]` + `[tool.hatch.version] path = "src/.../__init__.py"` — удалить `dynamic`/`[tool.hatch.version]` и поставить static `version = "0.1.0"` (иначе сборка ищет версию в удалённом файле и падает). _Факт: `uv 0.11.7` сгенерировал static `version = "0.1.0"` сразу — `dynamic`-хвоста не было, edge-case #2 не возник._
  - [x] **НЕ удалять `README.md`** _[edge-case #1]_: `pyproject.toml` ссылается на него (`readme = "README.md"`); файл должен остаться, иначе hatchling упадёт на сборке «readme file does not exist». (Либо убрать ключ `readme` из `[project]`, но проще оставить файл.)
  - [x] Прописать `[project.scripts]`: `gdau-logs = "scripts.tools.logs_api_cli:main"`, `gdau-init = "scripts.init.init_project:main"`.
  - [x] Прописать `[tool.hatch.build.targets.wheel] packages = ["scripts"]`, чтобы hatchling паковал `scripts/`, а не пакет по имени проекта. _Расхождение: `uv init` дал backend `uv_build`; заменён на `hatchling.build` по рецепту Dev Notes._
- [x] **Task 4 — Создать раскладку `scripts/` + стаб-модули (AC: #3, #4, #5)**
  - [x] Создать `scripts/{utils,tools,mcp,init}/` с `__init__.py` в каждом (регулярные пакеты → импорт `scripts.utils` резолвится, entry points находят `main`).
  - [x] Создать `scripts/8x_metrica_logs_api/` с `.gitkeep` (НЕ `__init__.py` — имя с цифры не импортируется dotted-путём; см. Dev Notes → «Каталог с цифровым префиксом»).
  - [x] Создать стаб `scripts/tools/logs_api_cli.py` с типизированной `def main() -> None:` (заглушка, реальный CLI — в 1.6).
  - [x] Создать стаб `scripts/init/init_project.py` с типизированной `def main() -> None:` (реальный init — в Epic 4).
  - [x] **Стаб `main()` должен завершаться с кодом 0** _[edge-case #4]_: НЕ `raise NotImplementedError` (даст non-zero и нарушит DoD #4 «запускается без ошибок»). Допустимо `print("gdau-logs: not yet implemented")` + неявный `return` (exit 0). _Проверено: `uv run gdau-logs`/`gdau-init` → exit 0._
- [x] **Task 5 — Создать остальные каталоги дерева (AC: #3)**
  - [x] Создать `tests/`, `templates/`, `yandex-docs/metrika-api/` (с `.gitkeep` где иначе пусто).
  - [x] `development-docs/` уже существует с `schema-catalog.csv` — **НЕ создавать заново, НЕ затирать**.
- [x] **Task 6 — Smoke-тест + зелёный pytest на пустом наборе (AC: #5, #6)**
  - [x] Создать `tests/test_smoke.py`, который `import scripts.utils` (и опц. проверяет резолюцию entry points) — даёт ≥1 собранный тест, чтобы `pytest` не вернул exit code 5 «no tests collected» (= красный CI). См. Dev Notes → «Пустой набор тестов».
  - [x] Локально проверить: `uv run python -c "import scripts.utils"` → exit 0; `uv run pytest` → зелёный; `uv run mypy scripts` → зелёный. _Проверено: 2 passed; mypy Success (7 файлов)._
- [x] **Task 7 — CI matrix ubuntu + windows (AC: #6)**
  - [x] Создать `.github/workflows/tests.yml` с матрицей `[ubuntu-latest, windows-latest]`, шагами `uv sync` + `uv run mypy` + `uv run pytest`. См. Dev Notes → «CI».
  - [x] **Закоммитить `uv.lock`** _[edge-case #7]_: CI использует `uv sync --frozen` и упадёт «lockfile not up to date», если лок не в индексе или рассинхронизирован. Перед push прогнать `uv lock --check` (валидирует, что лок соответствует `pyproject.toml`). _Проверено: `uv lock --check` ✓ и `uv sync --frozen` ✓ локально на Windows. Сам git-коммит истории (вместе с `uv.lock`) выполняется на шаге фиксации — см. Completion Notes._
- [x] **Task 8 — Расширить `.gitignore` (AC: #7)**
  - [x] К существующему `.gitignore` **добавить** `data/` и `*.writer.lock` (`.env` уже игнорируется). НЕ пересоздавать файл; НЕ добавлять blanket `*.csv` (затрёт коммитимые `schema-catalog.csv` и `paths-to-symlink.csv`). См. Dev Notes → «.gitignore». _Проверено `git check-ignore`: data/`*.parquet`, `*.writer.lock`, `.env` игнорируются; CSV-контракты — нет._
- [x] **Task 9 — Финальная проверка всех AC**
  - [x] Прогнать чек-лист соответствия из Dev Notes → «Definition of Done». _Все 7 пунктов DoD зелёные — см. Completion Notes._

### Review Findings

_Code review (adversarial, 3 слоя: Blind Hunter / Edge Case Hunter / Acceptance Auditor) — 2026-05-23._

- [x] [Review][Decision] Артефакты (включая `uv.lock`) не закоммичены — CI `uv sync --frozen` не прогонится, AC #6 фактически не подтверждён до commit+push — Найдено всеми тремя слоями. **РЕШЕНО:** Шеф выбрал «закоммитить сейчас» — реализация и BMAD-артефакты зафиксированы на `main` двумя коммитами (см. Change Log). Финальный «зелёный» CI подтверждается при push.
- [x] [Review][Patch] Нет `.gitattributes` при `core.autocrlf=true` — EOL не закреплён детерминированно для кросс-платформенного репо (NFR-2) [.gitattributes] — **РЕШЕНО:** добавлен `.gitattributes` (`* text=auto eol=lf` + явные текстовые типы + бинарные исключения `*.duckdb`/`*.parquet`).
- [x] [Review][Defer] CI исполняет только editable-путь (uv-editable через repo-root на `sys.path`), сборка wheel hatchling `packages=["scripts"]` в CI не запускается — регресс hatchling-конфига и namespace-загрязнение editable CI не поймает [.github/workflows/tests.yml] — deferred, forward-looking hardening (добавить `uv build`-smoke в CI позже)
- [x] [Review][Defer] `scripts.8x_metrica_logs_api` импортируем через `importlib.import_module` как пустой namespace-модуль (statement-import падает, как и заявлено) [scripts/8x_metrica_logs_api/] — deferred, относится к загрузке оркестратора p81 в истории 2.7
- [x] [Review][Defer] `tests/` без `__init__.py` и без `[tool.pytest.ini_options]` — резолюция `scripts` держится на editable-`.pth`; стоит закрепить `testpaths`/rootdir [tests/] — deferred, опциональное упрочнение тестовой конфигурации

## Dev Notes

> **Главный риск истории — пакетирование.** AC #1 требует `uv init --package`, AC #4 требует entry points `gdau-logs`/`gdau-init`, AC #5 требует импорт `scripts.utils.*`. Эти три требования вместе означают: проект должен быть **установленным пакетом, где импортируемый пакет — это `scripts/`, а не `src/<name>/`**. Раздел «Пакетирование» ниже — точный рецепт. Не импровизировать здесь.

### Текущее состояние репозитория (проверено)

- **Есть:** `.git/`, `.gitignore` (см. ниже), `_bmad/`, `_bmad-output/`, `development-docs/schema-catalog.csv` (116 строк: 1 заголовок + 115 полей; колонки `source,storage_name,metrica_field,type,description`).
- **Нет (создаём в этой истории):** `pyproject.toml`, `uv.lock`, `.python-version`, `scripts/`, `tests/`, `templates/`, `yandex-docs/`, `.github/`, `.mcp.json`.
- **Текущий `.gitignore`** уже игнорирует: `__pycache__/`, `*.py[cod]`, `*.egg-info/`, `.venv/`, `venv/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.env`, `.env.*` (с `!.env.example`), `*.duckdb`, `*.duckdb.wal`, `*.parquet`, `.DS_Store`, `Thumbs.db`. **Не хватает для AC #7:** `data/` и `*.writer.lock`.

### Пакетирование — `scripts/` как импортируемый пакет (AC #1, #4, #5) — КРИТИЧНО

**Почему directaiq нельзя скопировать 1:1.** Референс `directaiq` — **непакетированный** репо: в его `pyproject.toml` нет `[build-system]`, нет `[project.scripts]`, нет конфигурации пакета. Импорты `from scripts.utils... import` там работают через **implicit namespace packages (PEP 420) + `PYTHONPATH=<repo_root>`** (в его CI явно `PYTHONPATH: ${{ github.workspace }}`), а инструменты он запускает как `python scripts/...` / через nushell-обёртки. У него даже нет `scripts/__init__.py`.

**Почему нам нужен настоящий пакет.** Наши AC требуют console-команды `gdau-logs` и `gdau-init` через `[project.scripts]` (NFR-2: кросс-платформенно через `uv`, без bash/nushell-обвязки). Entry points работают только если проект установлен как пакет, в окружении которого резолвится модуль `scripts.tools.logs_api_cli`. Поэтому мы **пакетируем `scripts/`** — это осознанное расхождение с непакетированным directaiq (см. архитектуру → «Conscious Divergences»).

**Рецепт (делать ровно так):**

1. `uv init --package --name gamedev-analytics-unit` — даёт baseline с `build-backend = "hatchling.build"`, `.python-version`, `README.md`, сгенерированный `src/gamedev_analytics_unit/`.
2. **Удалить** `src/gamedev_analytics_unit/` и подчистить ссылки на него в `pyproject.toml`. Два хвоста, которые `uv init` оставляет указывающими на `src/` и которые **уронят `uv sync`** после удаления каталога:
   - `dynamic = ["version"]` + `[tool.hatch.version] path = "src/.../__init__.py"` → заменить на static `version = "0.1.0"`, секцию `[tool.hatch.version]` удалить. _[edge-case #2]_
   - `readme = "README.md"` в `[project]` ссылается на сгенерированный `README.md` → **не удалять `README.md`** (или убрать ключ `readme`). _[edge-case #1]_
3. Привести `pyproject.toml` к виду (ключевые секции):

```toml
[project]
name = "gamedev-analytics-unit"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "duckdb>=1.5,<1.6",
    "requests>=2.31",
    "mcp>=1.2",
    "python-dotenv>=1.0",
    "PyYAML>=6.0",
]

[project.scripts]
gdau-logs = "scripts.tools.logs_api_cli:main"
gdau-init = "scripts.init.init_project:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

# Ключ к AC #5: hatchling должен паковать каталог scripts/, а НЕ пакет по имени проекта.
[tool.hatch.build.targets.wheel]
packages = ["scripts"]

[tool.mypy]
python_version = "3.13"
explicit_package_bases = true
namespace_packages = true        # с explicit_package_bases + вложенными __init__.py нужно, иначе ошибки резолюции базы пакета
strict = true

[dependency-groups]
dev = [
    "pytest>=8",
    "mypy>=1.0",
]
```

4. **`__init__.py`-стратегия.** Сделать `scripts/`, `scripts/utils/`, `scripts/tools/`, `scripts/mcp/`, `scripts/init/` **регулярными пакетами** (пустой `__init__.py` в каждом). Это надёжнее «голого» namespace-варианта directaiq при editable-установке через hatchling и гарантирует, что `import scripts.utils` и резолюция entry points работают одинаково и под `uv run`, и в установленном окружении. (Минимальное расхождение с directaiq — у него `scripts/__init__.py` нет; у нас есть, потому что мы пакетируем.)
5. **Гейты верификации (обязательны перед закрытием истории):**
   - `uv run python -c "import scripts.utils"` → exit 0 (AC #5).
   - `uv run python -c "import scripts.tools.logs_api_cli, scripts.init.init_project"` → exit 0 (entry points резолвятся).
   - `uv run gdau-logs` и `uv run gdau-init` запускаются без ImportError (стаб `main` может просто ничего не делать/печатать usage).

> **Анти-паттерн:** оставить пакет `src/gamedev_analytics_unit/` и тянуть `scripts/` через `sys.path`/`PYTHONPATH` (стиль directaiq). Тогда entry points `gdau-logs` будут указывать на несуществующий в окружении модуль и упадут. Пакуем `scripts`, а не `src`.

### Каталог с цифровым префиксом `8x_metrica_logs_api/` (forward-looking)

`8x_metrica_logs_api` начинается с цифры → **не является валидным Python-идентификатором**, его нельзя `import scripts.8x_metrica_logs_api`. directaiq это терпит, потому что оркестратор там запускается как файл, а не импортируется dotted-путём. Для **этой** истории каталог должен лишь **существовать** (AC #3) — кладём в него `.gitkeep`, **без** `__init__.py`. Сам оркестратор `p81_load_logs.py` появляется в истории 2.7; механизм его загрузки CLI (вероятно `importlib.util.spec_from_file_location` по пути, а не dotted-import) — забота Epic 2, здесь не решаем. Не пытаться сделать его импортируемым в 1.1.

> **Build-гейт (не только import)** _[edge-case #5]_: гейты верификации проверяют, что `import scripts.utils` резолвится, но НЕ что `uv sync` соберёт пакет с цифровым подкаталогом внутри `packages = ["scripts"]`. Если hatchling при сборке отвергнет `8x_metrica_logs_api` как невалидное имя подпакета — НЕ удалять каталог (он нужен по AC #3), а исключить его из автодетекта пакетов и дотянуть как данные:
> ```toml
> [tool.hatch.build.targets.wheel]
> packages = ["scripts"]
> force-include = { "scripts/8x_metrica_logs_api" = "scripts/8x_metrica_logs_api" }
> ```
> Проверять обоими гейтами: `uv sync` проходит сборку **И** `uv run python -c "import scripts.utils"` → exit 0.

### Зависимости — точный список и анти-список (AC #2)

**Пинить (v1):** `duckdb` (1.5.x — `>=1.5,<1.6`; верифицировано stable 1.5.2 на 2026-05-23), `requests`, `mcp` (`>=1.2`; официальный SDK с встроенным FastMCP — НЕ отдельный пакет `fastmcp`), `python-dotenv`, `PyYAML`. Воспроизводимость держит `uv.lock`.

**НЕ тянуть (анти-список, NFR-6 «простота-первой»):** `pandas`, `numpy`, `scipy`, `numba`, `prophet`, `polars`, `tapi-yandex-direct`, `tapi-yandex-metrika`. Это аналитический/Direct-стек directaiq, в v1 он не нужен (Метрика работает через прямые HTTP-запросы в вендоренном `MetricaClient`, который придёт в 1.3 уже без `polars`). Появление любого из них в `[project.dependencies]` = нарушение AC #2.

**Dev-группа:** `pytest`, `mypy`. `uv sync` ставит группу `dev` по умолчанию → `uv run pytest`/`uv run mypy` доступны и локально, и в CI.

### CI — `.github/workflows/tests.yml` (AC #6)

directaiq гоняет CI **только на ubuntu и только pytest** (и через `PYTHONPATH`). Наши AC требуют **матрицу ubuntu+windows и добавляют mypy** — это осознанное усиление: переносимость Win↔Linux (NFR-2) — первоклассное требование, а Linux-only CI пропустит баги путей/rename/симлинков. `PYTHONPATH` нам **не нужен** — пакет `scripts` ставится editable через `uv sync`.

```yaml
name: Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
    runs-on: ${{ matrix.os }}

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5   # при наличии более свежего стабильного мажора — взять его
        with:
          enable-cache: true

      - name: Sync dependencies (frozen)
        run: uv sync --frozen          # uv поднимет нужный Python по .python-version, если его нет

      - name: Type-check
        run: uv run mypy scripts

      - name: Run tests
        run: uv run pytest
```

Замечания:
- `uv sync --frozen` падает, если `uv.lock` рассинхронизирован → защита воспроизводимости. Лок должен быть закоммичен.
- Матрица `fail-fast: false` — чтобы видеть результат обеих ОС, а не обрывать на первой.
- Триггеры на `main` (ветка по умолчанию репо — `main`).

### Пустой набор тестов → зелёный CI (AC #6) — известная ловушка

`pytest` без собранных тестов возвращает **exit code 5** («no tests collected»), что CI трактует как **провал (красный)**. AC #6 явно требует «проходит зелёным на пустом наборе тестов». Решение: создать осмысленный **smoke-тест** `tests/test_smoke.py`, который заодно закрывает AC #5:

```python
def test_scripts_package_importable() -> None:
    import scripts.utils  # noqa: F401
```

Это даёт ≥1 собранный тест → `pytest` возвращает 0. (Альтернатива через `pytest.ini_options` с подавлением exit-5 хуже: маскирует «реально нет тестов» в будущем.)

### `.gitignore` (AC #7)

**Расширить существующий файл**, не пересоздавать. Добавить:

```gitignore
# --- Data / storage artifacts (per-game хранилище, не dev-репо) ---
data/
*.writer.lock
```

`.env` уже игнорируется (`.env` + `.env.*` с исключением `!.env.example`). `*.duckdb`/`*.parquet` уже есть.

> **Анти-паттерн:** копировать из directaiq строку `scripts/**/*.csv` или ставить blanket `*.csv`. У нас коммитятся CSV-артефакты-контракты: `development-docs/schema-catalog.csv` (SSOT каталога, уже в репо) и будущий `templates/paths-to-symlink.csv` (симлинк-контракт). Их игнорировать **нельзя**. Текущие пути обоих файлов вне `scripts/`, так что просто не вводить общий `*.csv`-игнор.

### Раскладка проекта (AC #3) — целевое дерево dev-репо

Структура намеренно повторяет каркас directaiq (тренированная навигация владельца — Шефа). В этой истории создаём **скелет** (каталоги + стабы); наполнение модулей — последующие истории.

```
gamedev-analytics-unit/
├── pyproject.toml  uv.lock  .python-version  .gitignore   # .mcp.json/CLAUDE.md/CHANGELOG.md — позже
├── .github/workflows/tests.yml         # CI (эта история)
├── scripts/
│   ├── __init__.py
│   ├── utils/__init__.py               # наполняется: env_reader(1.2), metrica_client(1.3), dates(1.4), catalog(1.5)…
│   ├── 8x_metrica_logs_api/.gitkeep    # оркестратор p81 — история 2.7 (НЕ __init__.py: цифровой префикс)
│   ├── tools/
│   │   ├── __init__.py
│   │   └── logs_api_cli.py             # СТАБ main() (реальный CLI — 1.6)
│   ├── mcp/__init__.py                 # MCP-сервер — Epic 3
│   └── init/
│       ├── __init__.py
│       └── init_project.py             # СТАБ main() (реальный init — Epic 4)
├── templates/                          # .gitkeep (наполняется в Epic 4)
├── development-docs/                   # УЖЕ ЕСТЬ: schema-catalog.csv — не трогать
└── yandex-docs/metrika-api/            # .gitkeep (справочники Logs API кладёт владелец)
└── tests/
    └── test_smoke.py                   # импорт scripts.utils → зелёный pytest
```

Карта соответствия directaiq → наш проект (для ориентира; реализация — последующие истории): `scripts/utils/metrica_client.py` (вендоринг), `scripts/utils/env_reader.py` (вместо `auth_manager.py`), `scripts/tools/logs_api_cli.py` (та же форма argparse+`_create_parser`), `scripts/mcp/gdau_mcp_server.py`+`tools/core.py`, `scripts/init/init_project.py` (init на Python вместо `nushell/init_project.nu`).

### Project Structure Notes

- **Код под `scripts/`, НЕ `src/`** — обязательное требование владельца (единый «мышечный» каркас с directaiq). Сгенерированный `uv init` каталог `src/` удаляется.
- **`development-docs/` уже существует** с наполненным `schema-catalog.csv` — это SSOT каталога схемы (история 1.5 пишет загрузчик к нему). В 1.1 файл не читается и не модифицируется.
- **Имена snake_case** для будущих модулей; CLI-tool = `{name}_cli.py`; type hints обязательны (mypy strict). Эти конвенции тут лишь фиксируем стабами — реальное применение в 1.2+.
- **Расхождения с directaiq, осознанные:** пакетируем `scripts` (directaiq — нет); CI matrix+mypy (directaiq — ubuntu+pytest); нет `activate.sh`/`toolkit.nu` (заменяет `uv run`); не тащим `queue_cli`/disk-guard/cron/`BaseScript`/`config_manager` (NFR-6).

### Testing Requirements

- `tests/` зеркалят `scripts/` (история наполняет только `test_smoke.py`).
- Запуск: `uv run pytest` (локально и в CI), `uv run mypy scripts`.
- Smoke-тест обязателен (двойная роль: AC #5 + зелёный pytest на пустом наборе, AC #6).
- mypy `strict` + `explicit_package_bases = true` + `namespace_packages = true` (последнее нужно из-за вложенных `__init__.py`, иначе mypy спотыкается на резолюции базы пакета — edge #6). Запуск `uv run mypy scripts`; при упорной ошибке базы — `uv run mypy -p scripts`. Стаб `main()` типизированы (`def main() -> None:`), иначе strict-mypy ругнётся на untyped def.
- **Forward-looking (не блокирует 1.1, заложить на будущее):** когда модули начнут импортировать `duckdb`/`mcp` (истории 1.3+), у них может не быть type-stubs → mypy strict упадёт на `import-untyped`. Тогда добавить в `pyproject.toml` блок overrides (по образцу directaiq):
  ```toml
  [[tool.mypy.overrides]]
  module = ["duckdb", "duckdb.*", "mcp", "mcp.*"]
  ignore_missing_imports = true
  ```
  В 1.1 стабы ничего такого не импортируют, так что можно не добавлять сразу — но знать про эту ловушку.

### Definition of Done — чек-лист самопроверки

1. `uv sync` собирается **без ошибок** (нет хвостов на удалённый `src/`: static `version`, `README.md` на месте — edge #1/#2); `pyproject.toml` + `uv.lock` + `.python-version` (≥3.13, `requires-python = ">=3.13"`) на месте. (AC #1)
2. Зависимости = ровно {duckdb 1.5.x, requests, mcp≥1.2, python-dotenv, PyYAML}; анти-список (pandas/numpy/scipy/numba/prophet/polars/tapi-yandex-*) отсутствует. (AC #2)
3. Дерево содержит `scripts/{utils,8x_metrica_logs_api,tools,mcp,init}/`, `tests/`, `templates/`, `development-docs/`, `yandex-docs/metrika-api/`. (AC #3)
4. `[project.scripts]` объявляет `gdau-logs` и `gdau-init`; `uv run gdau-logs` / `uv run gdau-init` запускаются и **выходят с кодом 0** (стаб не `raise` — edge #4). (AC #4)
5. `uv run python -c "import scripts.utils"` → exit 0 (после успешной сборки `uv sync` — двойной гейт import+build, edge #5). (AC #5)
6. `.github/workflows/tests.yml` — матрица `[ubuntu-latest, windows-latest]`, шаги `uv sync` + `mypy` + `pytest`; `uv.lock` закоммичен и `uv lock --check` зелёный (edge #7); локально `uv run pytest` и `uv run mypy scripts` зелёные (mypy с `namespace_packages` — edge #6). (AC #6)
7. `.gitignore` игнорирует `.env`, `data/`, `*.writer.lock`; коммитимые CSV (`schema-catalog.csv`) НЕ под игнором. (AC #7)

### Latest Tech Information (верифицировано в архитектуре 2026-05-23)

Версии проверены по вебу в день написания архитектуры (совпадает с текущей датой): Python 3.14.4 / 3.13.13; uv 0.11.16; **DuckDB 1.5.2 stable** (1.4.x LTS до сен 2026); **MCP Python SDK 1.27.1** (FastMCP встроен). Пиннинг — через `uv.lock` (точные версии фиксирует он), в `pyproject.toml` — диапазоны-полы. Отдельный live-ресёрч в этой истории избыточен: версии зафиксированы тем же числом, а воспроизводимость обеспечивает лок. Использовать официальный `mcp` SDK (`mcp.server.fastmcp.FastMCP`), НЕ сторонний `fastmcp` 3.x (другая архитектура).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 1.1] — user story и 7 AC (уже усилены прогоном edge-case hunter).
- [Source: _bmad-output/planning-artifacts/epics.md#Additional Requirements] — стартовый шаблон, пин зависимостей v1, анти-список, runtime `>=3.13`, CLI = stdlib argparse, init на Python.
- [Source: _bmad-output/planning-artifacts/architecture.md#Starter Template Evaluation] — выбран `uv`-каркас + selective vendoring; команда `uv init --package`; верифицированные версии.
- [Source: _bmad-output/planning-artifacts/architecture.md#Project Structure & Boundaries] — полное дерево dev-репо, карта соответствия directaiq, entry points (`gdau-logs = scripts.tools.logs_api_cli:main`, `gdau-init = scripts.init.init_project:main`), импорты `from scripts.utils...`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Conscious Divergences from directaiq] — нет activate.sh/toolkit.nu; нет queue/disk-guard/cron/BaseScript/config_manager; init на Python; код под `scripts/`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming Patterns / Implementation Patterns] — snake_case, CLI `_create_parser`, type hints обязательны, CI `pytest`+`mypy`.
- [Source: D:/git/directaiq/pyproject.toml] — референс непакетированного проекта (нет build-system/scripts/package-config); причина, по которой мы пакетируем `scripts`.
- [Source: D:/git/directaiq/.github/workflows/tests.yml] — референс CI (ubuntu-only, pytest-only, `PYTHONPATH`); наш CI усилен (matrix + mypy, без PYTHONPATH).
- [Source: D:/git/directaiq/.gitignore] — референс; НЕ копировать `scripts/**/*.csv` (у нас CSV-контракты коммитятся).
- [Memory: structure-mirror-directaiq] — каркас держим похожим на directaiq; код под `scripts/`, не `src/`.
- [Memory: simplicity-first] — усложнять только по реальной потребности; не тащить тяжёлую инфру directaiq.
- [Memory: cli-tools-ai-native] — возможности как скриптуемые CLI; формат «как directaiq».
- [Memory: directaiq-reference] — `D:\git\directaiq` = источник вендоримых примитивов, не продуктовой логики.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Opus 4.7, 1M context) — Claude Code dev-story workflow.

### Debug Log References

- `uv sync` / `uv sync --frozen` — exit 0 (сборка hatchling + `packages=["scripts"]`, включая `8x_metrica_logs_api/`).
- `uv lock --check` — exit 0 (лок синхронен с `pyproject.toml`).
- `uv run pytest -q` — `2 passed`, exit 0.
- `uv run mypy scripts` — `Success: no issues found in 7 source files`, exit 0.
- `uv run gdau-logs` / `uv run gdau-init` — печатают usage-стаб, exit 0.
- `git check-ignore` — `.env`, `data/*.parquet`, `*.writer.lock` игнорируются; `schema-catalog.csv` — нет.

### Completion Notes List

- Ultimate context engine analysis completed — comprehensive developer guide created.
- **Все 9 задач выполнены, 7 AC удовлетворены, верифицировано локально на Windows 11 + Python 3.13.12 + uv 0.11.7.**
- **Расхождение со story Dev Notes (важно для будущих историй):** `uv 0.11.7` командой `uv init --package` генерирует build-backend `uv_build` (а не `hatchling`, как предполагали Dev Notes) и сразу прописывает **статическую** `version = "0.1.0"`. Следствия: (а) edge-case #2 (`dynamic`-версия на удалённый `src/`) **не возник** — хвоста не было; (б) бэкенд осознанно заменён на `hatchling.build` + `[tool.hatch.build.targets.wheel] packages = ["scripts"]` по рецепту Dev Notes (поведение hatchling с `packages` и цифровым подкаталогом в истории проработано, у `uv_build` — нет).
- **edge-case #5 (build с цифровым подкаталогом):** `uv sync` собрал пакет с `scripts/8x_metrica_logs_api/.gitkeep` без ошибок → fallback `force-include` **не понадобился**. Двойной гейт (build + `import scripts.utils`) пройден.
- **edge-case #6 (mypy резолюция базы пакета):** `uv run mypy scripts` прошёл сразу с `explicit_package_bases = true` + `namespace_packages = true`; запасной `uv run mypy -p scripts` не потребовался.
- **Версии (зафиксированы локом):** duckdb 1.5.3, mcp 1.27.1, requests 2.34.2, python-dotenv 1.2.2, PyYAML 6.0.3; dev — pytest 9.0.3, mypy 2.1.0.
- **CI (AC #6) — граница локальной проверки:** фактический «зелёный» прогон GitHub Actions требует push и здесь **не наблюдался**. Локально на Windows воспроизведены все три шага CI (`uv sync --frozen`, `uv run mypy scripts`, `uv run pytest`) — все exit 0; матрица `[ubuntu-latest, windows-latest]` с `fail-fast: false` прописана. Зелёный на обеих ОС подтвердится после первого push/PR.
- **Git-коммит не выполнялся** (по флоу dev-story и правилу «коммитить только по запросу»). Все файлы, включая `uv.lock`, готовы к коммиту; перед push edge-case #7 закрыт (`uv lock --check` ✓). При желании Шефа — могу закоммитить.
- `README.md` (был создан `uv init` пустым, 0 байт) дополнен минимальным содержимым: метаданные пакета теперь ссылаются на непустой readme.
- О подходе к тестам: история инфраструктурная, поэтому единственный осмысленный тест — smoke (Task 6, `tests/test_smoke.py`), он же закрывает AC #5 и «зелёный pytest на пустом наборе» (AC #6). Реализация шла строго по последовательности задач story (она авторитетна).

### File List

**Добавлены:**
- `pyproject.toml` — манифест пакета (hatchling, `packages=["scripts"]`, entry points `gdau-logs`/`gdau-init`, deps v1, mypy strict)
- `uv.lock` — лок воспроизводимости (47 пакетов)
- `.python-version` — пин `3.13`
- `README.md` — краткое описание + dev-быстрый старт
- `scripts/__init__.py`
- `scripts/utils/__init__.py`
- `scripts/tools/__init__.py`
- `scripts/tools/logs_api_cli.py` — стаб `main()` (gdau-logs)
- `scripts/mcp/__init__.py`
- `scripts/init/__init__.py`
- `scripts/init/init_project.py` — стаб `main()` (gdau-init)
- `scripts/8x_metrica_logs_api/.gitkeep` — каталог-плейсхолдер (без `__init__.py`: цифровой префикс)
- `tests/test_smoke.py` — импорт `scripts.utils` + резолюция entry-point модулей
- `templates/.gitkeep`
- `yandex-docs/metrika-api/.gitkeep`
- `.github/workflows/tests.yml` — CI matrix ubuntu+windows (`uv sync --frozen` + mypy + pytest)

**Изменены:**
- `.gitignore` — добавлены `data/` и `*.writer.lock` (без blanket `*.csv`)

**Удалены:**
- `src/gamedev_analytics_unit/__init__.py` (и каталог `src/`) — сгенерированный `uv init`, перенацелили пакет на `scripts/`

**Затронуты процессом (вне кода):**
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — статус истории `ready-for-dev → in-progress → review`

## Change Log

| Дата | Версия | Описание | Автор |
| --- | --- | --- | --- |
| 2026-05-23 | 0.1.0 | Реализована история 1.1: `uv`-каркас (hatchling, пакет `scripts/`), зависимости v1 + лок, раскладка дерева со стабами `gdau-logs`/`gdau-init`, smoke-тест, CI matrix ubuntu+windows, расширен `.gitignore`. Все 7 AC удовлетворены, верифицировано локально. | Amelia (dev-story) |
| 2026-05-23 | 0.1.0 | Code review (adversarial, 3 слоя): 7 AC подтверждены; применён патч `.gitattributes` (EOL=LF, NFR-2); 3 находки → defer (`deferred-work.md`); 4 отброшены как шум. Реализация и BMAD-артефакты закоммичены на `main`. Статус → done. | Code review |
