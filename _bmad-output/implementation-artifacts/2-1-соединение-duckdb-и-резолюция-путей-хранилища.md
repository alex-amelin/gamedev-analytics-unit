# Story 2.1: Соединение DuckDB и резолюция путей хранилища

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want резолюцию путей per-game хранилища и контекст-менеджер соединения DuckDB,
so that инструменты единообразно открывают БД и знают, где лежат партиции, при нулевом серверном процессе.

**Контекст эпика.** Первая история Epic 2 «Приём данных и безопасное обновление хранилища» — **фундамент всего пути записи**. Epic 1 (`done`) дал канал к Logs API (клиент 1.3, env-ридер 1.2, clamp 1.4, каталог 1.5, CLI 1.6), но **никто ещё не трогает хранилище игры**: 1.6 явно вынес `paths.py`/резолюцию `GDAU_DATA_ROOT` и любую запись данных за свою границу («это 2.1 / Epic 2»). Эта история кладёт два примитива, на которые встанут **все** последующие истории Epic 2 и далее: `scripts/utils/paths.py` (где в хранилище лежат партиции/БД/лок) и `scripts/utils/database_manager.py` (как открыть встроенный DuckDB в режиме write/read-only с гарантированным закрытием). Покрывает фундамент FR-8 (ноль серверов, переносимость); прямых данных ещё не пишет — это 2.2 (Parquet) и далее.

**Кто это потребляет (проектируй API под них).** `paths.py` → `parquet_store` (2.2, путь партиции дня), `writer_lock` (2.5, путь `.writer.lock`), `load_state` (2.4) и `views.py` (2.6, каталог `data/raw/{source}`), оркестратор p81 (2.7), MCP-чтение (3.1), init (4.3). `database_manager` → `load_state` (2.4), `views.py` (2.6), p81 (2.7, write-conn под локом), MCP (3.1, **read-only**-conn), init (4.3, создаёт `gdau.duckdb` + view'ы). Это **самый фундаментальный** модуль эпика — форма его функций станет контрактом для дюжины потребителей. Делай минимально, но «правильно с первого раза».

**Это «вендоринг», но НЕ построчный перенос (как 1.6 с CLI).** В directaiq есть прямые прообразы — `scripts/utils/database_manager.py` и `scripts/utils/paths.py`. Карта соответствия архитектуры (строки 422–423) помечает их «соединение DuckDB (**упрощено**)» и «как есть». **«Как есть» здесь вводит в заблуждение:** оба directaiq-модуля тяжело завязаны на инфру, которую мы сознательно НЕ тащим (миграции схемы, UDF-макросы Директа, `config_manager`, `activate.sh`, fallback old/new-структуры, `setup_paths` с `sys.path`-хаками). Берём **только узнаваемую форму вызова** (`with DatabaseManager.connection(...) as conn:` и набор `get_*`-резолверов), а тело пишем минимальным под нашу модель Parquet+встроенный DuckDB. Это та же логика, что в 1.6: «та же форма (глаз Шефа натренирован, [[structure-mirror-directaiq]]), но не та же обвязка (NFR-6, [[directaiq-reference]])».

### Главные риски / расхождения с directaiq (читать до кода)

1. **`paths.py`: fail-loud, БЕЗ авто-создания каталогов (AC #1, #5 — критично).** directaiq-`paths.py` при резолюции **создаёт** хранилище (`external_root.mkdir(parents=True, exist_ok=True)` в `_ensure_external_storage_initialized`, и `get_db_path` mkdir-ит `data/duckdb/`). У нас это **запрещено**: `GDAU_DATA_ROOT` не задан / указывает на несуществующий путь → **`ValueError` fail-loud**, и **ни одного каталога не создаётся** (особенно в dev-репо — инвариант project-context «в dev-репо данные не пишутся»). Резолверы `get_*` — **чистые функции** (никаких side-effect/mkdir). Единственный легальный mkdir во всей истории — в `database_manager` write-режиме (см. риск #3), и только под уже провалидированным существующим корнем хранилища.

2. **`paths.py` НЕ загружает `.env` и НЕ дублирует env_reader (шов с 1.2).** directaiq-`paths.py` тащит `_load_env_with_fallback` (зовёт `load_dotenv`). У нас загрузку `.env` **уже** делает `env_reader` (1.2, `_load_env`). `paths.py` только **резолвит пути из переменной окружения** `GDAU_DATA_ROOT`, ничего не грузит. Имя переменной — общий контракт: **переиспользуй константу** `from scripts.utils.env_reader import DATA_ROOT_ENV` (= `"GDAU_DATA_ROOT"`), не вводи второй литерал (рассинхрон имени = тихий баг). **Осознанная асимметрия с env_reader:** env_reader трактует отсутствие `GDAU_DATA_ROOT` как **не-фатал** (креды могут прийти прямо в процесс-окружение, режим `uv --env-file`); `paths.py` трактует его отсутствие как **жёсткий fail** (без корня хранилища некуда писать/читать данные). Это не противоречие — разные зоны ответственности; зафиксируй в docstring.

3. **`database_manager`: минимальный контекст-менеджер, вся инфра directaiq вырезана (AC #2, #4, #6).** directaiq-`DatabaseManager` несёт: `register_udfs` (Laplace/CPA/goal/cost-макросы — **семантика Директа, не геймдев**), `check_schema_version`/`get_pending_schema_migrations` (**система миграций**, у нас её нет), `REQUIRED_TABLES`/`TABLE_METADATA_DDL` (`table_metadata`/`query_metrics` — **таблицы Директа**; наша мета — `load_state`, заводится в 2.4, **не здесь**), `get_schema_version`/`needs_migration`/`has_required_tables`/`validate_migration_status` (**migration-tooling**), legacy `get_connection` с отсылками к `activate.sh`. **Всё это — DROP** (NFR-6). Оставляем ровно: `connection(read_only=False)` — `@staticmethod @contextmanager`, который резолвит путь через `paths.get_db_path()`, открывает встроенный DuckDB и **гарантированно закрывает** в `finally` (AC #4). Сообщения об ошибках ссылаются на `gdau-init`/`gdau-logs update`, **не** на `activate.sh`.

4. **read-only до первой выгрузки → понятная ошибка, не «голый» IOException (AC #6).** В read-only режиме при отсутствующем `gdau.duckdb` DuckDB сам бросит низкоуровневый `IOException` («database does not exist»). У нас: **до** `duckdb.connect` проверить `if read_only and not db_path.exists()` → `RuntimeError("БД не инициализирована: <путь> — запусти gdau-init или gdau-logs update")`. Так оператор/агент видит причину, а не сырой трейсбек движка.

5. **write-режим СОЗДАЁТ `gdau.duckdb` (и его родителя) — это легально (AC #2, #3).** Встроенный файловый DuckDB: `duckdb.connect(path, read_only=False)` создаёт файл при отсутствии. В write-режиме перед connect — `db_path.parent.mkdir(parents=True, exist_ok=True)` (каталог `data/duckdb/` **внутри уже провалидированного** корня хранилища — не dev-репо, риск #1 не нарушается). Нормально БД создаёт init (4.3), но write-conn обязан уметь поднять её сам (так работает embedded-движок и так тестируется 2.1). read-only **никогда** не создаёт (риск #4).

## Acceptance Criteria

1. **Given** `GDAU_DATA_ROOT` хранилища, **When** вызывается `paths.py`, **Then** резолвятся `data/raw/{source}/{YYYY-MM-DD}.parquet`, `data/duckdb/gdau.duckdb`, `.writer.lock` относительно корня хранилища.
2. **Given** `database_manager`, **When** открывается соединение в режиме write/read_only, **Then** возвращается соответствующее DuckDB-соединение к `gdau.duckdb`.
3. **Given** нет серверного процесса, **When** открывается БД, **Then** это файловый встроенный DuckDB, без отдельного сервера (FR-8).
4. **Given** соединение, **When** блок контекст-менеджера завершается, **Then** соединение корректно закрывается (нет утечки/висящего хэндла).
5. **Given** `GDAU_DATA_ROOT` не задан или указывает на несуществующий путь, **When** резолвится путь, **Then** fail-loud с понятным сообщением, без создания мусорных каталогов в dev-репо. _[edge-case: мусорная резолюция]_
6. **Given** режим read_only и `gdau.duckdb` ещё не создан (до init / первый запуск), **When** открывается соединение, **Then** понятная ошибка (БД не инициализирована), а не «голое» DuckDB IOException. _[edge-case: чтение до init]_

## Tasks / Subtasks

- [ ] **Task 1 — `scripts/utils/paths.py`: чистые fail-loud резолверы путей хранилища (AC: #1, #5)**
  - [ ] `from __future__ import annotations` первой строкой. Русский модульный docstring: роль (единственная точка резолюции путей per-game хранилища от `GDAU_DATA_ROOT`; чистые функции без side-effect; фундамент Epic 2/3/4). Импорты: stdlib `os`, `from pathlib import Path`; `from scripts.utils.env_reader import DATA_ROOT_ENV` (переиспользовать имя переменной — риск #2). `logger = logging.getLogger(__name__)` если логируешь (опционально — резолверы тихие). **Без** `dotenv`/`load_dotenv` (грузит env_reader, риск #2), **без** `sys.path`-хаков/`setup_paths` (entry points + hatchling уже резолвят импорты — 1.1), **без** fallback old/new-структур и **без** `mkdir` (риск #1).
  - [ ] `get_storage_root() -> Path` — прочитать `os.environ.get(DATA_ROOT_ENV)`; пусто/None → `ValueError` (понятно: переменная не задана, подсказать про `gdau-init`/запуск из хранилища). `Path(value).resolve()`; **если не `.is_dir()` → `ValueError`** (корень не существует — AC #5). **Никакого `mkdir`.** Это закрывает «мусорную резолюцию»: при отсутствующем/битом корне падаем ДО любого построения путей, каталоги не создаются.
  - [ ] `get_db_path() -> Path` → `get_storage_root() / "data" / "duckdb" / "gdau.duckdb"` (чистая резолюция, без mkdir).
  - [ ] `get_raw_partition_path(source: str, date: str) -> Path` → `get_storage_root() / "data" / "raw" / source / f"{date}.parquet"`. `date` — уже отформатированная строка `YYYY-MM-DD` (форматирование/валидация дат — `dates.py` 1.4, здесь не дублировать). Рекомендуется валидировать `source ∈ {"visits","hits"}` fail-loud (переиспользовать `from scripts.utils.catalog import VALID_SOURCES`) — мусорный источник не должен молча резолвиться в путь.
  - [ ] `get_raw_source_dir(source: str) -> Path` → `get_storage_root() / "data" / "raw" / source` — каталог источника (понадобится `views.py` 2.6 для glob `*.parquet`; тривиальный компаньон, заводим сейчас).
  - [ ] `get_writer_lock_path() -> Path` → `get_storage_root() / ".writer.lock"`. **Только путь** — захват/освобождение лока это story 2.5, не здесь.
  - [ ] `__all__` со списком публичных функций (как в `dates.py`/`catalog.py`).
- [ ] **Task 2 — `scripts/utils/database_manager.py`: контекст-менеджер соединения (AC: #2, #3, #4, #6)**
  - [ ] `from __future__ import annotations` первой строкой. Русский docstring: роль (единственная точка открытия встроенного DuckDB `gdau.duckdb`; write/read-only; гарантированное закрытие; ноль серверов FR-8). Явно отметить: **форма directaiq (`DatabaseManager.connection`) сохранена для узнаваемости, но это не построчный вендоринг** — вся инфра directaiq (миграции/UDF-макросы/таблицы Директа/`config_manager`/`activate.sh`) удалена (NFR-6, риск #3). Импорты: `contextlib`, `from collections.abc import Iterator`, `from pathlib import Path` (если нужен), `import duckdb`, `from scripts.utils.paths import get_db_path`. `logger = logging.getLogger(__name__)`.
  - [ ] Класс `DatabaseManager` с `@staticmethod @contextlib.contextmanager def connection(read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:` (форма directaiq; см. Latest Tech про аннотацию генератора под mypy strict). **Без** `db_path`-параметра/`check_schema_version`/`register_udfs` — путь всегда из `paths.get_db_path()` (единый резолвер), миграций/UDF нет.
  - [ ] Тело: `db_path = get_db_path()` (внутри — `get_storage_root()`, который fail-loud-ит при битом корне, AC #5 наследуется). Затем:
    - **read-only-гейт (AC #6):** `if read_only and not db_path.exists(): raise RuntimeError(f"БД не инициализирована: {db_path} — запусти gdau-init или gdau-logs update")` — до `duckdb.connect`, чтобы не выпускать «голый» IOException движка (риск #4).
    - **write-режим создаёт родителя (AC #2, риск #5):** `if not read_only: db_path.parent.mkdir(parents=True, exist_ok=True)` (внутри провалидированного корня хранилища — не dev-репо).
    - открыть: `conn = duckdb.connect(str(db_path), read_only=read_only)` (файловый embedded — AC #3, ноль серверов). Обернуть открытие в `try/except duckdb.Error as exc: raise RuntimeError(...) from exc` — чтобы и неожиданные ошибки connect выходили понятным сообщением, не сырым трейсбеком.
    - `yield conn` внутри `try`; в `finally` — `with contextlib.suppress(Exception): conn.close()` (AC #4: гарантированное закрытие даже при исключении в теле `with`; suppress на close — закрытие уже-битого хэндла не должно маскировать исходную ошибку).
  - [ ] **НЕ заводить** `load_state`/`table_metadata`/любые таблицы и DDL — схема это 2.4/2.6/init (4.3). `database_manager` только открывает соединение, не знает про объекты БД.
- [ ] **Task 3 — Спека компонента `docs/working-layer.md` (часть DoD)**
  - [ ] Завести `docs/working-layer.md` человеческим языком (project-context: `working-layer.md` — логический компонент «рабочий слой: view'ы + database_manager + типизация»; `paths` — мелкий хелпер, документируется внутри родственной спеки). Сейчас компонент описывает **фундамент**: резолюцию путей хранилища + открытие БД. Три вопроса простыми словами: **(1) Что делает** — по корню рабочего пространства игры (`GDAU_DATA_ROOT`) знает, где лежат сырьё (`data/raw/{источник}/{дата}.parquet`), сама база (`data/duckdb/gdau.duckdb`) и файл-замок записи (`.writer.lock`); и умеет открыть базу на запись или только-чтение, гарантированно закрывая её после; **(2) Зачем** — чтобы все инструменты единообразно находили рабочее пространство и открывали одну и ту же базу, при нулевом серверном процессе (просто файлы — копируется между Windows и Linux); **(3) Контракт** — вход: переменная окружения с корнем хранилища; выход: пути и открытое соединение; обещания: нет корня/корня нет на диске → сразу понятная остановка (ничего не создаёт «мимо»), чтение несуществующей базы → понятное «база не готова» (а не сырая ошибка движка), база всегда закрывается. **Явно отметить границы:** типизированные представления (`view'ы`) и парсинг типов — story 2.6 (этот файл дополнится); захват `.writer.lock` — 2.5; запись партиций — 2.2; мета-таблица — 2.4. Без сигнатур кода.
- [ ] **Task 4 — Offline-тесты (AC: #1–#6)**
  - [ ] `from __future__ import annotations`; зеркалит `scripts/` → `tests/test_paths.py` + `tests/test_database_manager.py`. Кросс-платформенно: только `tmp_path`/`pathlib`, без хардкода разделителей/абсолютных путей (CI гоняет ubuntu + windows). Без сети (модули сетей не знают). **`monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))`** задаёт корень хранилища; `monkeypatch.delenv(..., raising=False)` — снимает.
  - [ ] **`test_paths.py`:**
    - **AC #1:** при `GDAU_DATA_ROOT=tmp_path` — `get_db_path() == tmp_path/"data"/"duckdb"/"gdau.duckdb"`; `get_raw_partition_path("visits","2026-05-20") == tmp_path/"data"/"raw"/"visits"/"2026-05-20.parquet"`; `get_writer_lock_path() == tmp_path/".writer.lock"`; `get_raw_source_dir("hits")` корректен. Сравнивать через `Path`-равенство (не строки), чтобы тест прошёл на обеих ОС.
    - **AC #5 (не задан):** `monkeypatch.delenv(DATA_ROOT_ENV)` → `pytest.raises(ValueError)` на `get_storage_root()`/`get_db_path()`; **и проверить, что ни один каталог не создан** (напр. `data/` в репо/cwd не появился — assert на отсутствие side-effect).
    - **AC #5 (несуществующий):** `GDAU_DATA_ROOT = tmp_path/"nope"` (не создавать) → `ValueError`; каталог `nope` после вызова **не существует** (резолвер ничего не mkdir-ит).
    - (опц., если ввёл валидацию источника) `get_raw_partition_path("sessions", ...)` → `ValueError`.
  - [ ] **`test_database_manager.py`:**
    - **AC #2/#3 (write создаёт и работает):** `GDAU_DATA_ROOT=tmp_path`; `with DatabaseManager.connection() as conn: conn.execute("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1)")` или `SELECT 1`; после — `get_db_path().is_file()` True (реальный файл на диске = embedded, ноль серверов).
    - **AC #2 (read-only читает записанное):** сначала write создаёт/пишет, затем `with DatabaseManager.connection(read_only=True) as conn: conn.execute("SELECT ...").fetchall()` отдаёт данные.
    - **AC #4 (закрытие/нет утечки):** сохранить ссылку на `conn` из `with`; после выхода — `pytest.raises(Exception)` (`duckdb`-ошибка соединения) на `conn.execute("SELECT 1")` → доказывает, что закрыт. Плюс: открыть write, закрыть, затем снова открыть write/read-only **успешно** — отсутствие висящего эксклюзивного лока DuckDB подтверждает чистое закрытие (Windows особенно чувствителен).
    - **AC #6 (read-only до init):** `GDAU_DATA_ROOT=tmp_path` (корень есть), `gdau.duckdb` **нет** → `pytest.raises(RuntimeError)` с сообщением про «не инициализирована» (а не сырой `duckdb.IOException`); файл БД после так и **не создан** (read-only не создаёт).
  - [ ] **Анти-зависимость (через `ast`, не подстроку — docstring/комментарии содержат `directaiq`/`config_manager`/`pandas`):** распарсить `ast` обоих модулей, проверить отсутствие `Import`/`ImportFrom` (top-level имя `name.split(".")[0]`) на `pandas`/`polars`/`numpy` и на directaiq-инфру `config_manager`/`base_script`/`auth_manager`; для `database_manager` дополнительно убедиться, что нет узлов-ссылок на `register_udfs`/`schema_migrations`/`migrations` (вырезанная инфра, риск #3). Приём — из `tests/test_catalog.py`/`test_logs_api_cli.py`.
  - [ ] **Live-тест НЕ нужен** (и не заводить): 2.1 не ходит в сеть, DuckDB локален. Правило opt-in live ([[realapi-smoke-tests]]) применяется только к компонентам **внешнего API** (Logs API). Зафиксировать это в Dev Agent Record, чтобы отсутствие live-набора не сочли упущением.
- [ ] **Task 5 — Гейты верификации (обязательны перед закрытием)**
  - [ ] `uv run mypy scripts` → зелено (strict; полная типизация; генератор `connection` аннотирован `Iterator[duckdb.DuckDBPyConnection]`). Новых зависимостей нет (`duckdb` уже пин `>=1.5,<1.6`) → **`uv.lock` не меняется**.
  - [ ] `uv run pytest` → зелено (новый offline-набор + регрессия 1.1–1.6; live отсеян `addopts="-m 'not live'"`).
  - [ ] Прогнать чек-лист «Definition of Done» из Dev Notes.

## Dev Notes

### Контракт вендоринга (риск #3 — что берём из directaiq, что вырезаем; источник: `../directaiq/scripts/utils/`)

| directaiq | Наш 2.1 | Почему |
|---|---|---|
| `DatabaseManager.connection(db_path, read_only, check_schema_version)` contextmanager | `DatabaseManager.connection(read_only=False)` — та же форма, без `db_path`/`check_schema_version` | путь из `paths.get_db_path()` (единый резолвер); миграций нет |
| `register_udfs` (Laplace/CPA/`parse_goals`/`goal_price`/…) | **DROP** | семантика Директа/целей, не геймдев (NFR-6) |
| `check_schema_version`/`needs_migration`/`get_schema_version`/`migrations.runner` | **DROP** | системы миграций у нас нет |
| `REQUIRED_TABLES`/`TABLE_METADATA_DDL`/`table_metadata`/`validate_migration_status`/`has_required_tables` | **DROP** | таблицы Директа; наша мета — `load_state` (story 2.4), не здесь |
| legacy `get_connection` + сообщения `source activate.sh` | **DROP**; сообщения → `gdau-init`/`gdau-logs update` | `activate.sh` нет (`uv run`, кросс-платформа) |
| `paths._ensure_external_storage_initialized` + `mkdir(exist_ok=True)` на корне | **DROP**; нет корня/корня нет → `ValueError`, **без** mkdir | AC #5, инвариант «в dev-репо данные не пишутся» |
| `paths._load_env_with_fallback` (`load_dotenv`) | **DROP** — `.env` грузит `env_reader` (1.2) | один путь загрузки `.env`, без дублей (риск #2) |
| `paths.setup_paths` (`sys.path.insert`) | **DROP** | импорты резолвят entry points + hatchling (1.1) |
| `paths.get_*` с fallback old/new-структур + mkdir | чистые `get_*` без side-effect, одна раскладка | простота; одна модель Parquet |
| `DIRECTAIQ_DATA_ROOT` / `yandex_direct.duckdb` | `GDAU_DATA_ROOT` (константа из `env_reader`) / `gdau.duckdb` | наш контракт хранилища |

**Итог:** оба модуля выходят **крошечными**. `paths.py` — 5–6 чистых резолверов + один fail-loud `get_storage_root`. `database_manager.py` — один класс с одним `connection`-методом. Если получается много кода — значит притащил вырезанное.

### Раскладка хранилища (источник: architecture.md строки 488–504, project-context «Data & Domain»)

```
../{game}/                       # корень = GDAU_DATA_ROOT (per-game хранилище)
├── .env                         # креды (читает env_reader 1.2)
├── .writer.lock                 # лок писателя (резолвит paths; захватывает 2.5)
└── data/
    ├── raw/{visits,hits}/{YYYY-MM-DD}.parquet   # сырьё, партиция = день источника (пишет 2.2)
    └── duckdb/gdau.duckdb        # view'ы (2.6) + load_state (2.4); открывает database_manager
```

- storage-имена `snake_case`; `source ∈ {visits, hits}`. Один файл = один день одного источника.
- В dev-репо данные **не пишутся** — всё под `GDAU_DATA_ROOT`. Корень создаёт init (4.3); 2.1 его только **резолвит и валидирует** (fail-loud, не создаёт).

### Шов с env_reader (1.2) — общая переменная, разные политики (риск #2)

- `env_reader.DATA_ROOT_ENV == "GDAU_DATA_ROOT"` — **переиспользовать** (`from scripts.utils.env_reader import DATA_ROOT_ENV`), не вводить второй литерал. Цикла импорта нет: `env_reader` не импортирует `paths`.
- **env_reader:** отсутствие `GDAU_DATA_ROOT` → **не-фатал** (креды могут прийти из процесс-окружения; `_load_env` просто пропускает загрузку файла).
- **paths:** отсутствие/несуществование `GDAU_DATA_ROOT` → **жёсткий fail** (без корня хранилища данные негде брать/писать).
- Это осознанная асимметрия (зоны ответственности: креды vs данные) — задокументировать в docstring `paths.py`, чтобы будущий читатель не «починил» её под одну политику.

### DuckDB-специфика (DuckDB `>=1.5,<1.6`, embedded)

- `duckdb.connect(str(path), read_only=False)` — **создаёт** файл БД при отсутствии (нужен существующий родитель → mkdir в write-режиме). `read_only=True` на несуществующем файле → `duckdb.IOException` → перехватываем превентивной проверкой `exists()` (AC #6, риск #4).
- Встроенный движок = **ноль серверов** (AC #3): просто файл `.duckdb` на диске, открывается в процессе. `:memory:` не используем.
- **Блокировка файла:** write-conn держит эксклюзивный лок DuckDB на файле БД до `close()`; несколько read-only-conn сосуществуют. Это **внутренний лок движка**, НЕ наш `.writer.lock` (story 2.5) — последний охватывает всё хранилище (включая запись Parquet, которую DuckDB не лочит). Не путать; 2.1 ни тот, ни другой лок не захватывает (write-conn берёт DuckDB-лок неявно). На Windows незакрытый write-conn заблокирует повторное открытие — тест AC #4 (открыть→закрыть→открыть снова) это ловит.
- `path.resolve()` в `get_storage_root` проходит сквозь симлинки (в per-game хранилище `scripts`/`development-docs` приходят симлинками; сам `GDAU_DATA_ROOT` указывает на реальный корень).

### Паттерны от историй 1.x (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` первой строкой; русский модульный docstring (роль компонента); идентификаторы — английские.
- Type hints везде, `mypy --strict` по `scripts`, без `Any`-дыр. Абсолютные импорты от корня пакета (`from scripts.utils.X import Y`).
- Fail-loud `ValueError`/`RuntimeError` с понятным русским сообщением и контекстом (путь, имя переменной) — **никогда** «голый» трейсбек наружу. Логирование — stdlib `logging` (`logger = logging.getLogger(__name__)`); диагностика в stderr; секреты не логировать (здесь секретов нет, но `GDAU_DATA_ROOT` — путь, не секрет — печатать можно).
- Тесты: `tmp_path`/`monkeypatch`/`pytest.raises`, зеркалят `scripts/`; **анти-зависимость через `ast`** (не подстроку — docstring содержит `directaiq`). Кросс-платформенно (ubuntu + windows).
- **НЕ** заводить `logging_utils.py` (directaiq-обёртка `get_logger`) — существующие модули используют `logging.getLogger(__name__)` напрямую; так же здесь.

### Границы 2.1 (не выходить)

- Только два примитива: `paths.py` (резолверы) + `database_manager.py` (соединение). **Не** реализуем: запись Parquet (2.2), сверку строк (2.3), `load_state`/реконсиляцию (2.4), `.writer.lock`-захват (2.5), view'ы/`TRY_CAST` (2.6), p81-оркестрацию (2.7), инкремент/hot-window (2.8), `gdau-logs update` (2.9), MCP-чтение (3.1), init/симлинки (Epic 4).
- `paths.py` — **чистые** функции, ноль side-effect; единственный mkdir во всей истории — родитель БД в write-режиме `database_manager`.
- `database_manager` не знает про объекты БД (таблицы/view'ы) — только открыть/закрыть соединение.

### Project Structure Notes

- Модули — `scripts/utils/paths.py` и `scripts/utils/database_manager.py` ровно по дереву архитектуры (строки 448, 454) и карте соответствия directaiq (строки 422–423). `scripts/utils/` — регулярный пакет (`__init__.py` из 1.1).
- Имена snake_case (модули/функции); класс `DatabaseManager` CapWords; type hints обязательны (mypy strict). Не переводить на src-layout, не переименовывать пакет `scripts` (ломает резолюцию импортов — hatchling `packages=["scripts"]`).
- `tests/` зеркалит `scripts/`: `tests/test_paths.py`, `tests/test_database_manager.py`. Конфиг pytest (`[tool.pytest.ini_options]` с `markers`/`addopts`) уже есть (1.3); `conftest.py` в проекте нет — тесты используют `tmp_path`/`monkeypatch` напрямую.
- `docs/working-layer.md` — **заводится** (Task 3): project-context называет его логическим компонентом. Часть DoD.
- `uv.lock` не трогаем — stdlib (`os`/`pathlib`/`contextlib`/`logging`) + уже пинятый `duckdb`. Не реорганизовывать раскладку.

### Definition of Done — чек-лист самопроверки

1. `scripts/utils/paths.py` — чистые резолверы `get_storage_root`/`get_db_path`/`get_raw_partition_path`/`get_raw_source_dir`/`get_writer_lock_path`; `from __future__ import annotations`; русский docstring; `DATA_ROOT_ENV` переиспользован из `env_reader`; **ноль mkdir/side-effect**; нет/несуществует корень → `ValueError` fail-loud. (AC #1, #5)
2. `scripts/utils/database_manager.py` — `DatabaseManager.connection(read_only=False)` contextmanager; путь из `paths.get_db_path()`; write создаёт родителя+файл, read-only до init → `RuntimeError` «не инициализирована» (не IOException); `finally`-закрытие; **без** миграций/UDF/таблиц/`config_manager`/`activate.sh`. (AC #2, #3, #4, #6)
3. Встроенный файловый DuckDB, ноль серверов (FR-8); соединение всегда закрывается. (AC #3, #4)
4. `docs/working-layer.md` заведён (3 вопроса простыми словами; границы 2.2/2.4/2.5/2.6 названы) — DoD компонента. (Task 3)
5. Offline-тесты покрывают AC #1–#6 (резолюция путей; fail-loud без mkdir; write создаёт+читает; закрытие/нет утечки; read-only до init → понятная ошибка; анти-зависимость по `ast`). Live-набор осознанно отсутствует (нет внешнего API). (Task 4)
6. `uv run mypy scripts` и `uv run pytest` — зелёные; `uv.lock` не менялся (новых зависимостей нет). (Task 5)
7. Велась в отдельной ветке `story/2.1-duckdb-paths` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС (ubuntu + windows). PR в `main`.

### Latest Tech Information

- **DuckDB Python (`>=1.5,<1.6`):** `duckdb.connect(database: str, read_only: bool = False, config: dict = {})`. `read_only=True` на отсутствующем файле → исключение (`duckdb.IOException`) — перехватываем превентивной `exists()`-проверкой. write-режим создаёт файл (родитель должен существовать). `DuckDBPyConnection.close()` идемпотентен/безопасен под `contextlib.suppress`.
- **mypy strict + `@contextlib.contextmanager`:** аннотируй **внутренний генератор** как `Iterator[duckdb.DuckDBPyConnection]` (`from collections.abc import Iterator`) — `@contextmanager` сам превращает его в `ContextManager[...]` для вызывающего. (directaiq писал `Generator[duckdb.DuckDBPyConnection]`; под strict надёжнее `Iterator[...]`.) `duckdb` несёт type-стабы — `DuckDBPyConnection` типизирован, `Any`-дыр не нужно.
- **Web-ресёрч не требуется:** API DuckDB/stdlib стабильны и зафиксированы локом; внешнего сетевого контракта в истории нет (live-smoke неприменим).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.1] (строки 234–247) — user story + 6 AC (усилены edge-case hunter).
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2] (строки 118–120, 230–232) — роль 2.1: соединение/пути как фундамент пути записи; ядро NFR-1.
- [Source: _bmad-output/planning-artifacts/epics.md#FR-8] (строка 32) — переносимость Win↔Linux, ноль серверов (только Parquet + встроенный DuckDB).
- [Source: _bmad-output/planning-artifacts/architecture.md#directaiq mapping] (строки 422–423) — `database_manager.py` «соединение DuckDB (упрощено)», `paths.py` «как есть» (на деле — сильно урезать, риск #3).
- [Source: _bmad-output/planning-artifacts/architecture.md#Directory Structure] (строки 448, 454, 488–504) — `database_manager.py`/`paths.py` в дереве; раскладка per-game хранилища (`data/raw/{source}/{date}.parquet`, `data/duckdb/gdau.duckdb`, `.writer.lock`).
- [Source: _bmad-output/planning-artifacts/architecture.md#Architectural Boundaries] (строки 510–518) — dev-репо ↔ хранилище; резолюция через `GDAU_DATA_ROOT` + `paths.py`; в dev-репо данные не пишутся; write/read-каналы.
- [Source: _bmad-output/planning-artifacts/architecture.md#Requirements to Structure] (строка 527) — FR-8 → `utils/database_manager.py` + `utils/paths.py` (ноль сервера).
- [Source: _bmad-output/planning-artifacts/architecture.md#Scalability] (строки 257, 266–270) — ноль серверных процессов; view→таблицы обратимы (контекст для 2.6, не 2.1).
- [Source: _bmad-output/project-context.md#Data & Domain] (строки 96–122) — storage-имена snake_case; раскладка `data/raw/...`; граница dev-репо↔хранилище; `GDAU_DATA_ROOT` + `paths.py`; один писатель/чтение без лока (контекст 2.5/3.1).
- [Source: _bmad-output/project-context.md#Документация компонентов] (строки 52–77) — `working-layer.md` = логический компонент (view'ы + database_manager + типизация); мелкие хелперы (`paths`) — внутри родственной спеки; спека как DoD.
- [Source: scripts/utils/env_reader.py:28] — `DATA_ROOT_ENV = "GDAU_DATA_ROOT"` (переиспользовать); `_load_env` уже грузит `.env` из `GDAU_DATA_ROOT/.env` (не дублировать в paths).
- [Source: scripts/utils/catalog.py:35] — `VALID_SOURCES = ("visits","hits")` (для опц. валидации источника в `get_raw_partition_path`); образец fail-loud + docstring «Это НЕ вендоринг».
- [Source: scripts/utils/dates.py] — `parse_date`/`format_date` владеют форматом `YYYY-MM-DD`; `paths` принимает уже-строку, дат не парсит.
- [Source: ../directaiq/scripts/utils/database_manager.py] — форма-образец `DatabaseManager.connection` (contextmanager + `finally`-close). НЕ переносить `register_udfs`/миграции/`REQUIRED_TABLES`/`TABLE_METADATA_DDL`/legacy `get_connection`/`activate.sh`-сообщения.
- [Source: ../directaiq/scripts/utils/paths.py] — форма-образец `get_*`-резолверов. НЕ переносить `_ensure_external_storage_initialized`/`mkdir`-на-корне/`_load_env_with_fallback`/`setup_paths`/fallback old-new-структур.
- [Source: docs/catalog.md] — образец человекочитаемой спеки компонента (3 вопроса + «Границы») для `docs/working-layer.md`.
- [Source: tests/test_catalog.py, tests/test_logs_api_cli.py] — паттерн offline-тестов: `monkeypatch`/`tmp_path`/`pytest.raises`/`capsys`, анти-зависимость через `ast`.
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — defer 1.6: атомарная запись `download` (temp→rename под `.writer.lock`) отнесена к Epic 2 (2.2/2.7) — фон, не задача 2.1.
- [Memory: structure-mirror-directaiq] — держать форму directaiq (узнаваемость). [[directaiq-reference]] — вендорим примитивы/форму, НЕ инфру. [[simplicity-first]] — простота как инвариант (вырезать миграции/UDF/Direct-таблицы). [[cli-tools-ai-native]] — поверхности скриптуемы (контекст для будущих потребителей). [[realapi-smoke-tests]] — live применим только к внешнему API → в 2.1 не нужен.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
