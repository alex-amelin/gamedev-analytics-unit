# Story 2.4: Мета-таблица `load_state` + реконсиляция мета×факт

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want чекпойнт `load_state` с реконсиляцией против факта партиций,
so that «что загружено» было правдой о данных, а не слепой записью.

**Контекст эпика.** Четвёртая история Epic 2 «Приём данных и безопасное обновление хранилища». Фундамент уже стоит: 2.1 (`done`) дала `paths.py` + `database_manager.py` (контекст-менеджер соединения write/read-only), 2.2 (`done`) — `parquet_store.write_partition(...) -> int` (атомарная запись дня, возвращает число записанных строк), 2.3 (`ready-for-dev`) — жёсткую сверку строк источник↔партиция. Теперь 2.4 кладёт **чекпойнт состояния**: модуль `scripts/utils/load_state.py` с (а) мета-таблицей `load_state` в `gdau.duckdb` и (б) **реконсиляцией мета×факт на старте обновления** — сверкой записанного в таблицу против реальных Parquet-файлов, где **источник истины — факт партиции**, а мета приводится к нему. Покрывает **FR-12**; опорная часть **NFR-1** «не сломать базу» (crash-recovery + реконсиляция). В цепочке протокола идемпотентного дня 2.4 — **точка коммита дня**: `download → собрать → write_partition (2.2) → сверка (2.3) → **load_state (2.4)** → clean`. Реконсиляция — отдельный момент: она бежит **на старте** обновления (до загрузки), выверяя журнал против диска.

**Кто это потребляет (проектируй API под них).**
- **init (4.3)** создаёт таблицу при разворачивании БД — зовёт `ensure_load_state_table(conn)`.
- **Оркестратор p81 (2.7)** пишет чекпойнт: `mark_loaded(conn, source, date, row_count)` **после** rename+сверки (точка коммита), и зовёт `reconcile(conn)` на старте обновления.
- **Инкремент (2.8, FR-9)** читает результат `reconcile` — множество подтверждённо-загруженных дней — чтобы **пропускать загруженные**; перезалив/hot-window — тоже его забота, не 2.4.

Проектируй API под **инъекцию соединения** (как тесты БД 2.1): открытие/закрытие `gdau.duckdb` делает `DatabaseManager.connection(...)` у вызывающего, в функции `load_state` передаётся **готовый `conn`**. Так модуль чисто тестируется на временной БД + временных партициях, без сети и без своей логики открытия БД, а запись идёт под `.writer.lock` вызывающего (2.5) — сам лок здесь не берётся.

**Это НЕ вендоринг — новый модуль.** directaiq-`DatabaseManager` нёс систему миграций (`schema_migrations`/`check_schema_version`) и таблицы Директа — всё это сознательно вырезано в 2.1 ([[directaiq-reference]], NFR-6). `load_state` — выделенный новый модуль (project-context: один модуль = одна ответственность); дерево тестов архитектуры называет `test_load_state.py` (architecture.md:483) — `load_state.py` его естественное зеркало. Прямого аналога в directaiq нет — пиши с нуля по контракту ниже.

### Главные риски / решения (читать до кода)

1. **Источник истины — ФАКТ партиции, а не запись в таблице (AC #2, суть FR-12).** Реконсиляция доверяет **файлам на диске**, а не строкам меты: мета может врать (крэш посреди записи, ручное вмешательство, рассинхрон). На старте по каждому дню реальный файл партиции и его реальный `count()` — авторитетны; мета **приводится к ним** (мета ≠ факт → правят мету, не наоборот). Это разворот «слепой записи»: журнал не назначает правду, он её отражает.

2. **«День загружен» = конъюнкция трёх условий, не одно (AC #3, критично).** День засчитывается загруженным **только** при всех трёх: (1) файл партиции существует И (2) `мета.status == 'loaded'` И (3) `мета.row_count == фактический count() партиции`. Любое из трёх ложно → день **НЕ** загружен (под перезалив). Условие (3) — это и есть «мета согласована»: число строк в чекпойнте совпадает с реально лежащим в файле. Гейт ловит частичную/оборванную запись, оставившую мету впереди факта (мета говорит «5», а в партиции 4).

3. **Битая/нечитаемая партиция при `count()` НЕ валит всё обновление (AC #5, критично, анти-цель NFR-1).** `reconcile` считает строки через `read_parquet`; битый/нечитаемый файл → `duckdb.Error`. Ловим **per-day** → день помечается незагруженным под перезалив, `reconcile` **продолжает остальные дни и возвращается нормально**. Никогда не выпускать исключение наружу из-за одного битого файла — иначе один сбойный день блокирует обновление **всей** базы (это противоположность «не сломать базу»: лучше перелить один день, чем встать целиком). Лог WARNING с путём.

4. **`status ∈ {loading, loaded, failed}`; всё кроме `loaded` = незагружен (AC #6).** Крэш посреди записи мог оставить `status='loading'` (двухфазная отметка p81: «начал» → «закоммитил»); `'failed'` — явный провал. `reconcile` трактует `loading`/`failed` как **незагруженный** день — **даже если файл партиции существует и `count` совпал** (status-гейт строгий: засчитывается только `'loaded'`). Это защита от «полу-закоммиченного» дня: партиция могла лечь, но сверка/коммит не завершились — день под перезалив.

5. **Осиротевший `*.parquet.tmp` — НЕ партиция (граница с 2.2).** При перечислении файлов партиций считаем только `*.parquet`, **исключая** `*.parquet.tmp` (stale temp от прошлого крэша — забота `parquet_store` 2.2, не факт загруженного дня). Glob `*.parquet` по суффиксу не матчит `2026-05-20.parquet.tmp`, но это зафиксировать тестом, чтобы рефактор не сломал границу незаметно.

6. **2.4 НЕ перезаливает и НЕ оркеструет — только учёт + правда (границы).** `reconcile` **вычисляет** правду и **приводит мету к факту**; **решение** «какие дни лить» (skip загруженных / hot-window) — инкремент 2.8; сам цикл загрузки дня — p81 2.7; запись партиции — 2.2; сверка — 2.3. `mark_*` — примитивы записи чекпойнта, их зовёт p81 (как `verify_row_count` зовётся p81 в 2.3). **`row_count` — `BIGINT`**, НЕ `HUGEINT` (HUGEINT обоснован только для ID-полей `visit_id`/`client_id`/`watch_id` > 2^63; счётчик строк дня влезает в `BIGINT` с гигантским запасом — не применять HUGEINT механически).

7. **`conn` инъектируется — модуль БД сам не открывает (шов, тестируемость).** Все функции берут `duckdb`-соединение параметром; открытие/закрытие — `DatabaseManager.connection(...)` (2.1) у вызывающего (init/p81). `load_state` **НЕ** импортирует `database_manager` и **НЕ** зовёт `paths.get_db_path()` для открытия — он работает с переданным `conn`. Для перечисления и счёта партиций использует `paths.get_raw_source_dir`/`get_raw_partition_path` + `read_parquet` на том же `conn`. `load_state` **НЕ** импортирует `parquet_store` (нулевая сцепка по коду; считает он чтением, а не записью).

## Acceptance Criteria

1. **Given** БД, **When** инициализируется схема, **Then** есть таблица `load_state` (`source, date, row_count, loaded_at, status`).
2. **Given** старт обновления, **When** реконсилируется состояние, **Then** по каждому дню сверяются мета × факт партиции; источник истины — факт партиции.
3. **Given** день засчитывается загруженным, **When** проверяются условия, **Then** только при всех трёх: партиция есть + сверка сошлась (2.3) + мета согласована.
4. **Given** расхождение, **When** реконсиляция, **Then** день → незагружен (под перезалив), мета приводится к факту.
5. **Given** партиция нечитаема/битая, **When** реконсиляция делает `count()`, **Then** день помечается незагруженным под перезалив, а не валит всё обновление исключением. _[edge-case: битая партиция при реконсиляции]_
6. **Given** `status` ∈ {loading, loaded, failed} и крэш посреди записи оставил `loading`, **When** реконсиляция, **Then** `loading`/`failed` трактуются как незагруженный день. _[edge-case: статус незавершённого дня]_

## Tasks / Subtasks

- [x] **Task 1 — `scripts/utils/load_state.py`: таблица + отметки + реконсиляция (AC: #1–#6)**
  - [x] `from __future__ import annotations` первой строкой. Русский модульный docstring: роль (чекпойнт загруженных дней `load_state` в `gdau.duckdb` + реконсиляция мета×факт на старте; **факт партиции — источник истины**, мета приводится к нему; день загружен ⟺ партиция есть + `status='loaded'` + `row_count == факт`). Явно отметить границы: открытие/закрытие БД — `DatabaseManager` 2.1 (`conn` инъектируется), запись партиции — 2.2, сверка строк — 2.3, `.writer.lock` — 2.5, view/`TRY_CAST` — 2.6, оркестрация/перезалив/hot-window — 2.7/2.8. Импорты: `import logging`, `from collections.abc import Iterable` (по необходимости), `import duckdb` (тип соединения + ловля `duckdb.Error`), `from scripts.utils.paths import get_raw_partition_path, get_raw_source_dir`, `from scripts.utils.catalog import VALID_SOURCES`. **НЕ** импортировать `database_manager`/`parquet_store` (риск №7: `conn` инъектируется, считаем чтением). `logger = logging.getLogger(__name__)`. `__all__` с публичными именами.
  - [x] **Словарь статусов + DDL (AC #1, #6):**
    `STATUS_LOADING = "loading"`, `STATUS_LOADED = "loaded"`, `STATUS_FAILED = "failed"` (модульные константы — единый словарь, не магические строки). Константа DDL:
    `load_state(source VARCHAR NOT NULL, date DATE NOT NULL, row_count BIGINT, loaded_at TIMESTAMP, status VARCHAR NOT NULL, PRIMARY KEY (source, date))`. `date` — тип `DATE` (DuckDB сам кастит строку `'YYYY-MM-DD'`; нужно для диапазонных запросов инкремента/hot-window 2.8). `row_count` — `BIGINT` (риск №6 — НЕ HUGEINT). PK `(source, date)` нужен для UPSERT `ON CONFLICT`.
  - [x] **`ensure_load_state_table(conn: duckdb.DuckDBPyConnection) -> None` (AC #1):**
    исполняет DDL (`CREATE TABLE IF NOT EXISTS`) — **идемпотентно**; зовут init (4.3) при разворачивании БД и защитно p81 (2.7) перед записью. Без побочной логики.
  - [x] **Отметки чекпойнта (точка коммита дня; AC #6):**
    - `mark_loaded(conn, source, date, row_count) -> None` — UPSERT `ON CONFLICT (source, date) DO UPDATE`: `status='loaded'`, `loaded_at = current_timestamp`, `row_count = <переданное>`. Валидировать `source ∈ VALID_SOURCES` fail-loud и `row_count >= 0` (`ValueError`). Зовётся p81 **после** rename+сверки.
    - `mark_loading(conn, source, date) -> None` — UPSERT `status='loading'`, `loaded_at = NULL` (двухфазный старт p81; ради него `reconcile` и обязан трактовать `loading` как незагружен, AC #6).
    - `mark_failed(conn, source, date) -> None` — UPSERT `status='failed'` (полнота словаря AC #6; защитная отметка явного провала). _(Минимально необходимы `mark_loaded` + `mark_loading`; `mark_failed` — опционально, но дёшево и закрывает словарь.)_
  - [x] **Счёт строк факта партиции (AC #5, риск №3):**
    `count_partition_rows(conn, source, date) -> int | None` — реальное число строк в файле партиции. Путь — `get_raw_partition_path(source, date)`. Файла нет (`not path.exists()`) → `None` (день фактически отсутствует). Файл есть → `SELECT count(*) FROM read_parquet(?)` с **параметром-путём** (`[str(path)]`), не sql-литералом — путь хранилища не попадает в SQL (ни инъекции, ни кавычек, приём `parquet_store` 2.2). `duckdb.Error` (битый/нечитаемый Parquet) → лог **WARNING** + `None` (AC #5 — не валить обновление). `.parquet.tmp` сюда не попадает (резолвер даёт `.parquet`).
  - [x] **Реконсиляция мета×факт (AC #2, #3, #4, #5, #6):**
    `reconcile(conn, *, sources: Iterable[str] = VALID_SOURCES) -> frozenset[tuple[str, str]]` — на старте обновления. Алгоритм по каждому `source`:
    1. Собрать **объединение** ключей дней: `{строки меты для source}` ∪ `{stem файлов get_raw_source_dir(source).glob("*.parquet")}` (каталога нет → пусто; **исключить** `*.parquet.tmp` — риск №5). Дата дня — строка `YYYY-MM-DD` (для меты привести `date` → `.isoformat()`).
    2. Для каждого дня: `fact = count_partition_rows(...)`; прочитать `мета.status`, `мета.row_count`.
    3. **День загружен ⟺** `fact is not None` **И** `мета.status == STATUS_LOADED` **И** `мета.row_count == fact` (AC #3). Тогда → во множество результата, мета не трогается.
    4. **Иначе** (любое условие ложно, включая `fact is None` из-за отсутствия/битости файла — AC #5, и `loading`/`failed` — AC #6) → день **незагружен**: привести мету к факту (AC #4) — рекомендуется `DELETE` строки `load_state` этого дня (отсутствие строки = «не загружен», инкремент 2.8 перельёт; альтернатива — снизить статус, но не оставлять ложный `'loaded'`).
    5. Вернуть `frozenset` подтверждённо-загруженных `(source, date)`; лог INFO итог (загружено / исправлено). _(Допустимо вернуть маленький dataclass `ReconcileResult` с `loaded`/`corrected` — финализируй под нужды 2.8; ключевое — наружу множество загруженных дней.)_
  - [x] **НЕ делать:** решение «skip загруженных» / перезалив / hot-window (2.8); цикл загрузки дня и сборку из TSV-частей (2.7); открытие/закрытие БД (2.1 — `conn` инъектируется); запись партиции (2.2); жёсткую сверку источник↔партиция при загрузке (2.3 — это другая сверка, по сырому TSV); захват `.writer.lock` (2.5); `HUGEINT` для `row_count` (риск №6); считать `.parquet.tmp` партицией (риск №5); выпускать исключение наружу из-за одного битого файла (AC #5); импорт `parquet_store`/`database_manager` (риск №7).
- [x] **Task 2 — Спека компонента `docs/ingestion.md` (часть DoD)**
  - [x] **Дополнить** существующий `docs/ingestion.md` (разделы «запись дня в сырьё» от 2.2 и «сверка числа строк» от 2.3) новым разделом **«учёт загруженных дней и реконсиляция»** человеческим языком, без жаргона. Три вопроса простыми словами: **(1) Что делает** — ведёт журнал «какой день какого источника уже загружен и сколько в нём строк», и на старте каждого обновления **сверяет журнал с реальными файлами на диске**; **(2) Зачем** — чтобы «загружено» было **правдой**: если файл дня пропал/побился/запись оборвалась — журнал не должен врать, что день на месте; день, в правдивости которого нет уверенности, помечается «перелить» (лучше лишний раз перелить один день, чем тихо считать неполный день готовым); **(3) Контракт** — день считается загруженным **только** если совпало трое: файл дня есть **+** в журнале статус «загружен» **+** число строк в журнале равно реальному числу строк в файле; **источник истины — файл** (журнал подгоняется под файл, не наоборот); **битый/нечитаемый файл одного дня НЕ роняет всё обновление** — этот день просто помечается «перелить», остальные идут дальше; недописанный день (статус «в процессе»/«сбой») = незагружен. **Явно отметить границы:** запись файла-партиции — соседний раздел (2.2); сверка чисел при загрузке — 2.3; замок одного пишущего — 2.5; типы/представления — 2.6; **решение «что грузить» и перезалив hot-window — 2.7/2.8** (этот раздел лишь говорит правду о состоянии, грузит — оркестратор). Без сигнатур кода; не дублировать `metrica-client.md`/`working-layer.md`.
- [x] **Task 3 — Offline-тесты `tests/test_load_state.py` (AC: #1–#6)**
  - [x] `from __future__ import annotations`; зеркалит `scripts/` → `tests/test_load_state.py`. Временная БД: `duckdb.connect(str(tmp_path / "t.duckdb"))` (или `:memory:`); `GDAU_DATA_ROOT` через `monkeypatch.setenv` (для `paths`); партиции-фикстуры писать **реальным** `parquet_store.write_partition` (он уже есть, 2.2) либо прямым `conn.write_parquet`. **Без сети**; live не нужен. Кросс-платформенно (`tmp_path`, `pathlib`).
  - [x] **AC #1:** `ensure_load_state_table(conn)` создаёт таблицу с колонками `source/date/row_count/loaded_at/status` (проверить через `PRAGMA table_info('load_state')` / `information_schema`); **идемпотентность** — повторный вызов не падает. `mark_loaded(...)` → строка с `status='loaded'`, `row_count`, `loaded_at IS NOT NULL`; повторный `mark_loaded` того же дня — UPSERT (одна строка, обновлена, не дубль).
  - [x] **AC #2/#3 (три условия, факт-истина):** партиция на N строк + `mark_loaded(row_count=N)` → `reconcile` вернул день в множестве загруженных, мета не тронута.
  - [x] **AC #4 (расхождение → незагружен, мета к факту):** (a) мета `loaded, row_count=5`, партиция реально на 4 строки → `reconcile`: дня **нет** в загруженных, мета приведена к факту (строка удалена/снижена, ложного `'loaded'` не осталось); (b) мета `loaded`, партиция **отсутствует** → не загружен, мета исправлена.
  - [x] **AC #5 (битая партиция не валит обновление):** записать **мусорные байты** в `data/raw/visits/{date}.parquet` → `reconcile` **НЕ бросает**, этот день не загружен; рядом — здоровый загруженный день, он **остаётся** в результате (доказывает, что битый файл не сорвал весь проход).
  - [x] **AC #6 (статус незавершённого дня):** `status='loading'` + здоровая **совпавшая по count** партиция → день **НЕ** загружен (строгий status-гейт); то же для `'failed'`. Stale `{date}.parquet.tmp` рядом → **не** считается партицией/загруженным днём (риск №5).
  - [x] **Анти-зависимость (через `ast`, по реальным import-узлам):** нет top-level import `pandas`/`polars`/`numpy`/`pyarrow`, directaiq-инфры `config_manager`/`base_script`, ссылок на вырезанное `schema_migrations`/`register_udfs`. **`duckdb` РАЗРЕШЁН** (в отличие от `row_check` 2.3 — `load_state` штатно работает с БД). Проверять отсутствие импорта `scripts.utils.parquet_store` **в самом `scripts/utils/load_state.py`** (риск №7), даже если тест-фикстура использует `write_partition`. Приём — `tests/test_database_manager.py:202`/`tests/test_parquet_store.py:300`.
  - [x] **Live-тест НЕ нужен** (и не заводить): `load_state` в сеть не ходит — внутренние операции над БД и локальными файлами. Правило opt-in live ([[realapi-smoke-tests]]) — только для внешнего API (Logs API). Зафиксировать в Dev Agent Record, чтобы отсутствие live-набора не сочли упущением (как 2.1/2.2/2.3).
- [x] **Task 4 — Гейты верификации (обязательны перед закрытием)**
  - [x] `uv run mypy scripts` → зелено (strict; `conn: duckdb.DuckDBPyConnection`; полная типизация возвратов, в т.ч. `int | None` и `frozenset[tuple[str, str]]`; без `Any`-дыр).
  - [x] `uv run pytest` → зелено (новый offline-набор + регрессия 1.x/2.1/2.2; live отсеян `addopts="-m 'not live'"`).
  - [x] Новых зависимостей нет (`logging`/`collections.abc` stdlib + уже-зависимый `duckdb`) → **`uv.lock` не меняется**.
  - [x] Прогнать чек-лист «Definition of Done» из Dev Notes.

## Dev Notes

### Рекомендуемый контракт `load_state` (финализируй под p81 2.7 / инкремент 2.8)

| Имя | Сигнатура | Смысл |
|---|---|---|
| `STATUS_LOADING`/`STATUS_LOADED`/`STATUS_FAILED` | `str` | словарь статусов; только `loaded` засчитывается (AC #6) |
| `ensure_load_state_table` | `(conn) -> None` | `CREATE TABLE IF NOT EXISTS load_state`; идемпотентно (AC #1); зовут init 4.3 / p81 2.7 |
| `mark_loading` | `(conn, source, date) -> None` | UPSERT `status='loading'` — старт двухфазной отметки (поддержка AC #6) |
| `mark_loaded` | `(conn, source, date, row_count) -> None` | UPSERT `status='loaded'`, `loaded_at=current_timestamp` — **точка коммита дня** (после rename+сверки) |
| `mark_failed` | `(conn, source, date) -> None` | UPSERT `status='failed'` (опционально; полнота словаря) |
| `count_partition_rows` | `(conn, source, date) -> int \| None` | факт партиции; нет файла → `None`; битый → WARNING+`None` (AC #5) |
| `reconcile` | `(conn, *, sources=VALID_SOURCES) -> frozenset[tuple[str,str]]` | мета×факт на старте; вернуть загруженные дни, привести мету к факту (AC #2–#6) |

### Точная схема таблицы (AC #1)

```sql
CREATE TABLE IF NOT EXISTS load_state (
    source     VARCHAR   NOT NULL,
    date       DATE      NOT NULL,
    row_count  BIGINT,                 -- число строк дня; НЕ HUGEINT (HUGEINT только для ID-полей)
    loaded_at  TIMESTAMP,              -- NULL для loading; current_timestamp при loaded
    status     VARCHAR   NOT NULL,     -- loading | loaded | failed
    PRIMARY KEY (source, date)         -- нужен для ON CONFLICT (UPSERT) и для skip-инкремента 2.8
);
```

### UPSERT в DuckDB (приём `mark_*`)

```sql
INSERT INTO load_state (source, date, row_count, loaded_at, status)
VALUES (?, ?, ?, current_timestamp, 'loaded')
ON CONFLICT (source, date) DO UPDATE SET
    row_count = excluded.row_count,
    loaded_at = excluded.loaded_at,
    status    = excluded.status;
```

`ON CONFLICT` опирается на PK `(source, date)`. `date` передаётся строкой `'YYYY-MM-DD'` — DuckDB кастит в `DATE` сам. Параметры — биндингом (`?`), не конкатенацией.

### Счёт факта партиции (риск №3 — образец)

```
def count_partition_rows(conn, source, date):
    path = get_raw_partition_path(source, date)
    if not path.exists():
        return None                       # дня фактически нет
    try:
        row = conn.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
        return int(row[0])
    except duckdb.Error as exc:            # битый/нечитаемый Parquet
        logger.warning("Партиция %s нечитаема (%s) — день под перезалив", path, exc)
        return None                        # AC #5: НЕ бросать наружу
```

- Путь — **параметром** `read_parquet(?)` (не sql-литерал): корень хранилища не попадает в SQL — ни инъекции, ни проблем с кавычками (риск как в `parquet_store` 2.2).
- `.exists()` отсекает отсутствующий файл до DuckDB; `duckdb.Error` ловит битый существующий (AC #5).

### Реконсиляция (псевдокод; AC #2–#6)

```
loaded = set()
for source in sources:
    meta = { row.date.isoformat(): row for row in SELECT * FROM load_state WHERE source=? }
    parts = { p.stem for p in get_raw_source_dir(source).glob("*.parquet") }   # .tmp исключён
    for date in meta.keys() | parts:
        fact = count_partition_rows(conn, source, date)        # None если нет/битый
        m = meta.get(date)
        if fact is not None and m and m.status == STATUS_LOADED and m.row_count == fact:
            loaded.add((source, date))                         # три условия — AC #3
        else:
            DELETE FROM load_state WHERE source=? AND date=?   # мета → факт (AC #4); loading/failed/битый — сюда (AC #5/#6)
return frozenset(loaded)
```

- `get_raw_source_dir(source)` может не существовать (нет партиций) → пустой `glob` (гард `if dir.exists()`).
- `meta.keys() | parts` — объединение: ловит и «мета есть, файла нет» (ложный `loaded`), и «файл есть, меты нет» (под перезалив).
- Источник истины — `fact` (партиция): мета корректируется к нему, никогда наоборот (риск №1).

### Протокол идемпотентного дня (где 2.4 в цепочке; architecture.md:379–383, 536–538)

`download parts → собрать день (p81) → write_partition (2.2) → сверка expected↔actual (2.3) → **[2.4] load_state: mark_loaded** → clean`.
2.4 владеет **точкой коммита** (`mark_loaded` после rename+сверки — день «загружен» ТОЛЬКО здесь) и **реконсиляцией на старте** (`reconcile` до загрузки). День «загружен» = rename + сверка сошлась + мета согласована (три условия). Реконсиляция: по каждому дню мета × факт партиции, источник истины — факт (architecture.md:382–383; project-context:110).

### Паттерны от историй 1.x/2.1/2.2/2.3 (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` первой строкой; русский модульный docstring (роль компонента + границы с соседними историями); идентификаторы английские.
- Type hints везде, `mypy --strict` по `scripts`, без `Any`-дыр. Абсолютные импорты от корня пакета. `logger = logging.getLogger(__name__)` напрямую — **НЕ** заводить `logging_utils.py`.
- Fail-loud с понятным русским сообщением и контекстом (источник, дата, числа). Но **AC #5 — осознанное исключение из fail-loud**: битая партиция при реконсиляции = WARNING + день под перезалив, НЕ исключение (не валить обновление). Это не «глотание ошибки», а целевое поведение FR-12 (источник истины — факт; битый факт = «нет дня»).
- `conn` инъектируется (шов, как тесты 2.1 гоняют `DatabaseManager` на `tmp_path`); UPSERT через `ON CONFLICT`; `read_parquet(?)` параметром (приём `parquet_store`).
- Тесты: `pytest`, зеркалят `scripts/`; tmp-БД + `monkeypatch.setenv(GDAU_DATA_ROOT)`; **анти-зависимость через `ast`** (по import-узлам, не подстроке), `duckdb` разрешён. Кросс-платформенно (ubuntu + windows).
- Live-набор осознанно отсутствует (нет внешнего API) — зафиксировать, как 2.1/2.2/2.3.

### Границы 2.4 (не выходить)

- Один модуль: `scripts/utils/load_state.py` (+ дополнение `docs/ingestion.md` + тесты `tests/test_load_state.py`). **Не** реализуем: запись Parquet (2.2 — готова), жёсткую сверку источник↔партиция при загрузке (2.3 — готова), открытие/закрытие БД (2.1 — `conn` инъектируется), `.writer.lock` (2.5), view'ы/`TRY_CAST` (2.6), p81-оркестрацию/сборку дня (2.7), решение «skip загруженных»/перезалив/hot-window (2.8), `gdau-logs update` (2.9).
- `load_state` не ходит в сеть, БД сам не открывает (получает `conn`), не берёт локов, не пишет Parquet, не парсит TSV. Только: мета-таблица в `gdau.duckdb` + сверка её против факта партиций.

### Project Structure Notes

- Модуль — `scripts/utils/load_state.py` (выделенный: один модуль = одна ответственность, project-context; architecture.md:452). Дерево тестов архитектуры называет `test_load_state.py` (строка 483) — `load_state.py` его естественное зеркало.
- `scripts/utils/` — регулярный пакет (`__init__.py` из 1.1). Имена snake_case; type hints обязательны (mypy strict). Не переводить на src-layout, не переименовывать пакет `scripts` (ломает резолюцию импортов — hatchling `packages=["scripts"]`).
- Объект DuckDB `load_state` живёт в `gdau.duckdb` рядом с view'ами `visits`/`hits` (architecture.md:322, 497). Имя snake_case.
- `tests/` зеркалит `scripts/`: `tests/test_load_state.py`. Конфиг pytest (`markers`/`addopts`) уже есть (1.3/1.6); `conftest.py` в проекте нет.
- `docs/ingestion.md` — **дополняется** (Task 2): project-context называет `load_state` логическим компонентом приёма (`metrica_client` + p81 + `parquet_store` + `load_state`). Часть DoD: меняется контракт компонента → обновить спеку в том же изменении.
- `uv.lock` не трогаем — stdlib (`logging`/`collections.abc`) + уже-зависимый `duckdb`. Новых зависимостей нет. Не реорганизовывать раскладку.

### Definition of Done — чек-лист самопроверки

1. `scripts/utils/load_state.py`: словарь статусов + `ensure_load_state_table` (idempotent DDL) + `mark_loaded`/`mark_loading`(/`mark_failed`) (UPSERT) + `count_partition_rows` (факт; нет/битый → `None`) + `reconcile` (мета×факт, вернуть загруженные, мету к факту). `conn` инъектируется; НЕ импортирует `parquet_store`/`database_manager`. (AC #1–#6)
2. Таблица `load_state(source, date, row_count, loaded_at, status)`, `date` = `DATE`, `row_count` = `BIGINT` (не HUGEINT), PK `(source, date)`. (AC #1)
3. День загружен ⟺ партиция есть **И** `status='loaded'` **И** `row_count == факт count()` (три условия — конъюнкция). (AC #3)
4. Источник истины — факт партиции; расхождение → день незагружен, мета приведена к факту (ложный `'loaded'` не остаётся). (AC #2, #4)
5. Битая/нечитаемая партиция → WARNING + день под перезалив, `reconcile` **не бросает** и обрабатывает остальные дни. (AC #5)
6. `status` `loading`/`failed` → незагружен даже при совпавшем count; `.parquet.tmp` не считается партицией. (AC #6, риск №5)
7. `docs/ingestion.md` дополнен разделом «учёт загруженных дней и реконсиляция» (3 вопроса простыми словами; границы 2.2/2.3/2.5/2.6/2.7/2.8 названы; источник истины — файл) — DoD компонента. (Task 2)
8. Offline-тесты покрывают AC #1–#6 + анти-зависимость по `ast` (нет pandas/polars/pyarrow/numpy; `duckdb` разрешён; нет импорта `parquet_store` в модуле). Live-набор осознанно отсутствует. (Task 3)
9. `uv run mypy scripts` и `uv run pytest` — зелёные; `uv.lock` не менялся; `data/`-артефактов в dev-репо не создано.
10. Велась в отдельной ветке `story/2.4-load-state` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС (ubuntu + windows). PR в `main`.

### Latest Tech Information

- **DuckDB `ON CONFLICT` (UPSERT):** поддерживается при наличии PRIMARY KEY/UNIQUE (`INSERT … ON CONFLICT (cols) DO UPDATE SET col = excluded.col`). Идеально для `mark_*` (повторная отметка дня обновляет строку, не плодит дубли). `duckdb >=1.5` (локфайл) поддерживает.
- **`read_parquet(?)` параметром:** табличная функция принимает путь биндингом — путь хранилища не уходит в текст SQL (нет инъекции/кавычек). На отсутствующий/битый файл бросает `duckdb.IOException` (подкласс `duckdb.Error`) — ловим, как в `parquet_store.py:156`.
- **`current_timestamp`/`now()`:** стандартный SQL-таймстамп — `loaded_at` ставим в SQL, без Python `datetime` (меньше импортов, время БД консистентно).
- **`date` как `DATE`:** мета-строки возвращаются с `datetime.date`; для сравнения с stem'ами файлов (`'YYYY-MM-DD'`) приводить `.isoformat()`. Тип `DATE` (не VARCHAR) нужен инкременту/hot-window 2.8 для диапазонных условий.
- **Web-ресёрч не требуется:** stdlib + `duckdb` стабильны и зафиксированы локом; внешнего сетевого контракта в истории нет (live-smoke неприменим, как 2.1/2.2/2.3).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.4] (строки 278–291) — user story + 6 AC (усилены edge-case hunter: битая партиция, статус незавершённого дня).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-12] (строка 40) — мета-таблица `load_state` (source, date, row_count, loaded_at, status); реконсиляция против факта партиций; день засчитан при: партиция + сверка + мета.
- [Source: _bmad-output/planning-artifacts/epics.md#NFR-1] (строка 61) — целостность: crash-recovery и реконсиляция мета×факт; требование первого класса.
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2] (строки 230–232) — место 2.4 в упорядоченной цепочке 2.1→2.9; ядро NFR-1.
- [Source: _bmad-output/planning-artifacts/architecture.md#Data Architecture] (строки 212–214) — мета-состояние `load_state` + реконсиляция против факта Parquet-партиции на старте (источник истины — факт партиции).
- [Source: _bmad-output/planning-artifacts/architecture.md#Communication & Process] (строки 379–383) — протокол идемпотентного дня; «реконсиляция на старте: мета × факт, источник истины — факт партиции; мета привести к факту».
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming/Объекты DuckDB] (строки 322–323) — мета-таблица `load_state`, snake_case.
- [Source: _bmad-output/planning-artifacts/architecture.md#Directory Structure] (строки 452, 483, 497) — `load_state.py` [новое]; `test_load_state.py`; `gdau.duckdb` содержит `load_state`.
- [Source: _bmad-output/planning-artifacts/architecture.md#FR→структура] (строка 529) — FR-12 (мета) в `utils/load_state.py`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Integration Points] (строки 536–538) — поток приёма: … → сверка → rename → `load_state`.
- [Source: _bmad-output/project-context.md#Целостность базы] (строки 105–111) — протокол идемпотентного дня; реконсиляция на старте (мета × факт, источник истины — факт); перезалив = перезапись одного файла, без `DROP`.
- [Source: scripts/utils/database_manager.py:39–60] — `DatabaseManager.connection(read_only)` (write/read-only, finally-close); сюда инъектируется `conn` (риск №7).
- [Source: scripts/utils/parquet_store.py:55,163,177] — `write_partition(...) -> int` возвращает число записанных строк = `row_count` для `mark_loaded`; `read_parquet`/duckdb.Error-обёртка как приём счёта.
- [Source: scripts/utils/paths.py:101–118] — `get_raw_partition_path`/`get_raw_source_dir` для пути партиции и перечисления `*.parquet`.
- [Source: scripts/utils/catalog.py:35] — `VALID_SOURCES = ("visits", "hits")`.
- [Source: tests/test_database_manager.py:28–234] — паттерн tmp-БД + `monkeypatch.setenv(GDAU_DATA_ROOT)` + ast-анти-зависимость; зеркало для `test_load_state.py`.
- [Source: tests/test_parquet_store.py:300] — паттерн анти-зависимости через `ast` (import-узлы + запрещённые имена).
- [Source: docs/ingestion.md] — спека компонента приёма (разделы «запись дня» 2.2; «сверка строк» 2.3); 2.4 добавляет «учёт загруженных дней и реконсиляция». Образец стиля (3 вопроса + «Границы»).
- [Source: _bmad-output/implementation-artifacts/2-3-жёсткая-сверка-строк.md] — предыдущая история: сверка гейтит шаг `load_state` исключением ДО него; 2.4 пишет `mark_loaded` только когда сверка прошла. Граница 2.3↔2.4.
- [Memory: realapi-smoke-tests] — live применим только к внешнему API → в 2.4 не нужен (внутренние БД-операции). [[simplicity-first]] — реконсиляция как простой проход «мета vs файлы», без тяжёлого фреймворка миграций. [[structure-mirror-directaiq]] — `load_state.py` в `utils/`, форма узнаваема, инфра directaiq (миграции) вырезана.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Claude Opus 4.7, 1M context) — bmad-dev-story workflow.

### Debug Log References

- `uv run mypy scripts` → `Success: no issues found in 16 source files` (strict).
- `uv run pytest tests/test_load_state.py -v` → 14 passed.
- `uv run pytest` (полный) → 237 passed, 3 deselected (live отсеян `addopts="-m 'not live'"`).
- Один self-fix в ходе реализации: ранний черновик передавал `current_timestamp` строкой-параметром (DuckDB не скастил бы её в TIMESTAMP) — заменено на SQL-выражение `current_timestamp` прямо в `VALUES` (две формы UPSERT: `_UPSERT_LOADED_SQL` / `_UPSERT_PENDING_SQL`).

### Completion Notes List

- **Task 1 (AC #1–#6):** `scripts/utils/load_state.py` — словарь статусов `STATUS_LOADING/LOADED/FAILED`; `ensure_load_state_table` (идемпотентный `CREATE TABLE IF NOT EXISTS`); `mark_loaded` (UPSERT `status='loaded'` + `current_timestamp`, валидация `source ∈ VALID_SOURCES` и `row_count >= 0` fail-loud) / `mark_loading` / `mark_failed` (UPSERT `ON CONFLICT`, сброс `row_count`/`loaded_at` в NULL); `count_partition_rows` (`read_parquet(?)` параметром; нет файла → `None`; `duckdb.Error` → WARNING + `None`, не валит проход); `reconcile` (объединение `мета ∪ файлы`, `.tmp` исключён; день загружен ⟺ три условия — файл + `status='loaded'` + `row_count==факт`; иначе ложная мета удаляется → факт; возвращает `frozenset[(source,date)]`). `conn` инъектируется; НЕ импортирует `parquet_store`/`database_manager` (риск №7). `row_count` = `BIGINT` (не HUGEINT, риск №6).
- **Task 2:** `docs/ingestion.md` дополнен разделом «Учёт загруженных дней и реконсиляция» (3 вопроса простыми словами; источник истины — файл; границы 2.1/2.2/2.3/2.5/2.6/2.7/2.8 названы); раздел «Границы» актуализирован (2.4 → реализован).
- **Task 3:** `tests/test_load_state.py` — 14 offline-тестов: AC #1 (создание схемы + идемпотентность + UPSERT без дублей + fail-loud отметок), AC #2/#3 (подтверждение при трёх условиях), AC #4 (мета впереди факта + отсутствие файла → исправление), AC #5 (битая партиция не валит проход; здоровый сосед уцелел; `count_partition_rows` варианты), AC #6 (параметризовано `loading`/`failed` → незагружен при совпавшем count; stale `.tmp` ≠ партиция), анти-зависимость через `ast` (`duckdb` разрешён; нет pandas/polars/numpy/pyarrow; нет импорта `parquet_store`/`database_manager`).
- **Task 4 (гейты):** mypy strict зелёный; pytest зелёный (237 + 14 новых); `uv.lock` не менялся (stdlib `logging`/`collections.abc`/`typing` + уже-зависимый `duckdb`); `data/`-артефактов в dev-репо не создано.
- **Live-набор осознанно отсутствует** (как 2.1/2.2/2.3): `load_state` в сеть не ходит — операции над БД и локальными файлами. Opt-in live ([[realapi-smoke-tests]]) — только для внешнего Logs API.

### File List

- `scripts/utils/load_state.py` — **новый**: мета-таблица `load_state` + отметки + `count_partition_rows` + `reconcile`.
- `tests/test_load_state.py` — **новый**: offline-набор (AC #1–#6 + ast-анти-зависимость).
- `docs/ingestion.md` — **изменён**: добавлен раздел «Учёт загруженных дней и реконсиляция»; актуализированы шапка и «Границы».
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — **изменён**: статус истории 2-4 → in-progress → review.

## Change Log

- 2026-05-24 — Story 2.4 создана (create-story): мета-таблица `load_state` + реконсиляция мета×факт. Выделенный модуль `scripts/utils/load_state.py` (`conn` инъектируется — БД сам не открывает; НЕ импортирует `parquet_store`/`database_manager`); таблица `load_state(source, date DATE, row_count BIGINT, loaded_at TIMESTAMP, status)` PK `(source,date)`; `ensure_load_state_table`/`mark_loading`/`mark_loaded`/`mark_failed` (UPSERT `ON CONFLICT`) + `count_partition_rows` (факт через `read_parquet(?)`; нет/битый → `None`) + `reconcile` (мета×факт на старте; источник истины — факт партиции; день загружен ⟺ партиция + `status='loaded'` + `row_count==факт`; расхождение/битый/`loading`/`failed` → незагружен, мета к факту; битая партиция НЕ валит обновление). Дополнение `docs/ingestion.md`; offline-набор `tests/test_load_state.py`; live неприменим. Статус → ready-for-dev.
- 2026-05-24 — Story 2.4 реализована (dev-story): `scripts/utils/load_state.py` (словарь статусов + `ensure_load_state_table` идемпотентно + `mark_loaded`/`mark_loading`/`mark_failed` UPSERT `ON CONFLICT` + `count_partition_rows` факт/нет/битый→`None` без падения + `reconcile` мета×факт, три условия, мета→факт, `frozenset` загруженных). `docs/ingestion.md` дополнен разделом «Учёт загруженных дней и реконсиляция» + актуализированы «Границы». `tests/test_load_state.py` — 14 offline-тестов (AC #1–#6 + ast-анти-зависимость, `duckdb` разрешён). Гейты зелёные: mypy strict (16 файлов), pytest 237 passed + 3 deselected (live); `uv.lock` не менялся; `data/` в dev-репо не создано. Live осознанно отсутствует (нет внешнего API). Статус → review.
