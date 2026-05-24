# Story 2.2: Атомарная запись Parquet-партиции дня

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want атомарную запись сырья дня в Parquet,
so that партиция была либо целой, либо отсутствовала — без частичных дней и без CAST.

**Контекст эпика.** Вторая история Epic 2 «Приём данных и безопасное обновление хранилища». История 2.1 (`done`) положила фундамент — `paths.py` (где в хранилище лежат партиции/БД/лок) и `database_manager.py` (как открыть `gdau.duckdb`). Теперь 2.2 кладёт **первый модуль, который реально пишет данные игры на диск**: `scripts/utils/parquet_store.py` — атомарную запись сырья одного дня одного источника в Parquet-партицию. Это нижний слой пути записи: его поверх обернут сверка строк (2.3), мета-таблица `load_state` (2.4), `.writer.lock` (2.5) и оркестратор p81 (2.7), который соберёт день из TSV-частей и позовёт `parquet_store`. Покрывает **FR-6** (сырьевой слой: Parquet, партиции по дню, строками, без CAST) и **FR-14** (атомарная запись дня: `.tmp` → атомарный rename); опорная часть **NFR-1** «не сломать базу». Прямо закрывает defer из 1.6 (атомарная запись `download` через temp→rename отнесена к Epic 2).

**Кто это потребляет (проектируй API под них).** Единственный прямой потребитель — оркестратор **p81 (2.7)**: он скачивает TSV-части (`MetricaClient.download_log_request_part` → `bytes`), парсит их в строки, собирает день и зовёт `parquet_store.write_partition(...)` под `.writer.lock` (2.5). Косвенно от формата партиции зависят: сверка строк (2.3, считает строки записанной партиции), реконсиляция мета×факт (2.4, `count()` по факту партиции), view'ы (2.6, читают `data/raw/{source}/*.parquet` через `read_parquet`/`union_by_name`), MCP-чтение (3.1). Форма `write_partition` — контракт, который примет p81; проектируй чисто (вход = разобранные строки + имена колонок, выход = число записанных строк), чтобы тесты не требовали ни сети, ни TSV-файлов.

**Это НЕ вендоринг — новый модуль.** В отличие от 2.1 (`database_manager`/`paths` имели directaiq-прообразы), `parquet_store.py` помечен в дереве архитектуры как **`[новое]`** (строка 450). У directaiq нет прямого аналога: он писал в таблицы DuckDB с родными `ym:s:*`-именами, а наша модель — отдельные Parquet-файлы по дням со storage-именами `snake_case`. Пиши с нуля по контракту ниже; узнаваемой directaiq-формы здесь копировать нечего.

### Главные риски / решения (читать до кода)

1. **Parquet пишем ТОЛЬКО через DuckDB — pandas/polars/pyarrow запрещены (риск №1, критично).** project-context прямо запрещает аналитический стек (`pandas`/`numpy`/`polars`) и любые новые зависимости. `duckdb` уже пинён (`>=1.5,<1.6`) и умеет писать Parquet сам. **Механизм:** открыть **транзиентное in-memory** соединение `duckdb.connect()` (это НЕ `gdau.duckdb` и НЕ `DatabaseManager` — рабочая база тут ни при чём, см. риск №3), `CREATE TABLE` с колонками `storage_name VARCHAR`, залить строки через `executemany("INSERT … VALUES (?,…)", rows)` (чистые Python-строки, без pandas), затем `connection.table(tmp_table).write_parquet(str(tmp_path))`. **Используй `relation.write_parquet(<python-str-путь>)`, а не `COPY … TO '<путь>'`** — так путь не встраивается в SQL (нет инъекции/проблем с кавычками в пути хранилища). `executemany` с пустым списком + `write_parquet` корректно пишет пустой Parquet **со схемой** (см. риск №5).

2. **Сырьё — строками as-is, единственное преобразование = переименование колонок по каталогу (риск №2, AC #1).** Значения вставляются в VARCHAR-колонки **дословно, как пришли** (TSV-ячейки — строки; массивы вроде `[8273645,8273646]` тоже хранятся строкой — их парсинг в `LIST` это view 2.6, не здесь). **Никакого `CAST`/усечения/дедупа в сырьевом слое** (анти-паттерн project-context). Единственная трансформация: входные колонки приходят с родными именами Метрики (`ym:s:visitID` — так в TSV-заголовке), а в Parquet ложатся под `storage_name` (`visit_id`) по каталогу (1.5). Маппинг `metrica_field → storage_name` бери из `Catalog.fields_for(source)`; неизвестное входное имя (нет в каталоге) → **fail-loud** (поле без записи = дефект).

3. **`parquet_store` НЕ открывает `gdau.duckdb` и НЕ берёт `.writer.lock` (риск №3, границы).** Сырьевые партиции — **самостоятельные файлы** под `data/raw/{source}/`, к рабочей базе `gdau.duckdb` отношения не имеют. Поэтому `parquet_store` **не** использует `DatabaseManager` (это для рабочей базы — view'ы/`load_state`), а поднимает свой одноразовый in-memory DuckDB как «кодировщик Parquet» и закрывает его в `finally`. Замечание «`:memory:` не используем» из 2.1 относилось к **рабочей** базе (она обязана быть файлом) — для транзиентного кодировщика in-memory как раз правильно. `.writer.lock` захватывает p81 (2.7) вокруг вызова — здесь его не трогаем (story 2.5).

4. **Атомарность: `.tmp` в том же каталоге → `os.replace` (риск №4, AC #2, #4, #5).** Пиши `write_parquet` в `{date}.parquet.tmp` **в том же каталоге**, что финальная партиция (та же ФС — иначе rename не атомарен), затем `os.replace(tmp, final)`. **Именно `os.replace`, не `os.rename`** — `os.rename` поверх существующего файла падает на Windows; `os.replace` атомарно перезаписывает и на POSIX, и на Windows (CI гоняет обе ОС). Замена одного файла не трогает другие дни (FR-6/FR-10). Каталог `data/raw/{source}/` создаётся write-стороной перед записью (`mkdir(parents=True, exist_ok=True)` внутри уже провалидированного `get_storage_root()` — не dev-репо; оберни в `try/except OSError → RuntimeError`, как патч ревью 2.1 для `database_manager`). На фейле записи — почисти свой `.tmp` в `finally` (не оставляй частичный temp).

5. **Легитимно пустой день (0 строк) → пустая партиция СО СХЕМОЙ, день загружен (риск №5, AC #7).** API может вернуть валидный день без визитов. Это **не** ошибка и **не** «день отсутствует». `write_partition` всё равно создаёт Parquet-файл с правильными колонками (схему даёт список входных колонок, который p81 передаёт даже при нуле строк — он знает запрошенные поля из каталога). DuckDB: `CREATE TABLE` с колонками + `write_parquet` без вставок = пустой типизированный Parquet. Так сверка (2.3) увидит `0 == 0` (успех), а реконсиляция (2.4) — существующую партицию.

6. **Идемпотентность — на уровне данных, не байт-в-байт (AC #3).** Перезалив того же дня = `os.replace` поверх существующей партиции (ровно один файл, без `DROP`). «Повтор даёт тот же файл» означает **идентичное содержимое/число строк**, а не байт-идентичность: DuckDB может вшить в Parquet метаданные писателя (версию), поэтому **не тестируй байт-равенство** — проверяй идемпотентность чтением строк/`count`. Порядок строк сохраняется как пришёл (сырьё verbatim), повтор того же входа → тот же порядок → то же содержимое.

7. **Дрейф схемы между днями — пиши что пришло, не добивай до полного каталога (FR-6).** `parquet_store` пишет **ровно те колонки, что передал вызывающий** для этого дня, а не весь набор каталога. Если список полей менялся, исторические партиции остаются со своими колонками (FR-6: «смена списка полей не мигрирует исторические партиции на лету»). Расхождение колонок между партициями разрулит `views.py` (2.6) через `union_by_name`. Не навязывай полный каталог-набор и не «мигрируй» старые файлы.

## Acceptance Criteria

1. **Given** строки одного дня одного источника, **When** `parquet_store` пишет день, **Then** данные ложатся в `data/raw/{source}/{YYYY-MM-DD}.parquet` строками как пришли (без CAST/усечения); единственное преобразование — переименование колонок в snake_case по каталогу.
2. **Given** запись дня, **When** она выполняется, **Then** сначала `*.parquet.tmp`, затем атомарная замена в финальную партицию (FR-14).
3. **Given** перезалив существующего дня, **When** `parquet_store` пишет тот же день, **Then** перезаписывается ровно одна партиция, остальные дни не тронуты; повтор даёт тот же файл (идемпотентность, FR-10), без `DROP`.
4. **Given** платформа Windows (где `os.rename` поверх существующего файла падает), **When** выполняется замена, **Then** используется `os.replace` (атомарная перезапись и на POSIX, и на Windows). _[edge-case: Windows rename-over-existing]_
5. **Given** `.tmp` и финальная партиция, **When** пишется день, **Then** `.tmp` создаётся в том же каталоге (та же ФС), иначе rename не атомарен. _[edge-case: cross-FS rename]_
6. **Given** осиротевший `*.parquet.tmp` от прошлого крэша, **When** начинается новая запись дня, **Then** старый `.tmp` удаляется/перезаписывается, не мешая. _[edge-case: stale tmp]_
7. **Given** легитимно пустой день (0 строк от API), **When** `parquet_store` пишет день, **Then** пишется пустая партиция (со схемой) и день помечается загруженным — не трактуется как «отсутствующий»/ошибка. _[edge-case: валидный пустой день]_

## Tasks / Subtasks

- [ ] **Task 1 — `scripts/utils/parquet_store.py`: атомарная запись Parquet-партиции дня (AC: #1–#7)**
  - [ ] `from __future__ import annotations` первой строкой. Русский модульный docstring: роль (единственная точка записи сырья дня в Parquet-партицию; строки as-is, рантайм-преобразование только переименование колонок по каталогу; атомарность temp→rename; Parquet пишется встроенным DuckDB, БЕЗ pandas/polars). Явно отметить границы: `.writer.lock` берёт p81 (2.5/2.7), сверку делает 2.3, мета — 2.4, рабочую базу `gdau.duckdb` модуль НЕ трогает (риск №3). Импорты: stdlib `os`, `logging`, `from collections.abc import Iterable, Sequence`, `import duckdb`, `from scripts.utils.paths import get_raw_partition_path`, `from scripts.utils.catalog import Catalog, load_catalog`. `logger = logging.getLogger(__name__)`. `__all__` с публичной функцией.
  - [ ] Публичная функция (рекомендуемый контракт — финализируй под p81 2.7):
    `def write_partition(source: str, date: str, columns: Sequence[str], rows: Iterable[Sequence[str | None]], *, catalog: Catalog | None = None) -> int:`
    где `columns` — родные имена Метрики (`ym:s:*`/`ym:pv:*`, как в TSV-заголовке), `rows` — разобранные строки дня (TSV-ячейки дословно), `catalog` — инъектируемый шов (тесты дают мини-фикстуру; в проде `load_catalog()`). Возвращает число записанных строк данных (удобно p81/сверке 2.3; сама сверка — 2.3).
  - [ ] **Резолюция пути и переименование (AC #1):** `partition_path = get_raw_partition_path(source, date)` (валидирует `source ∈ {visits,hits}` и наследует fail-loud `get_storage_root` при битом корне). Построить маппинг `metrica_field → storage_name` из `(catalog or load_catalog()).fields_for(source)`. Для каждого имени из `columns` взять `storage_name`; **отсутствие в каталоге → `ValueError`** (поле без записи = дефект). Сохранять **порядок входных колонок**.
  - [ ] **Создать каталог партиции (AC #5, риск №4):** `partition_path.parent.mkdir(parents=True, exist_ok=True)` (внутри провалидированного корня хранилища — не dev-репо), обёрнутый `try/except OSError → RuntimeError` с путём (контракт «на ошибку — понятное сообщение, не сырой OSError», как патч ревью 2.1).
  - [ ] **Запись через DuckDB-кодировщик (AC #1, риск №1):** открыть `conn = duckdb.connect()` (in-memory, **не** `DatabaseManager`, **не** `gdau.duckdb`). `CREATE TABLE` с колонками `"<storage_name>" VARCHAR` (кавычить идентификаторы; имена уже snake_case-валидны из каталога). `conn.executemany(f"INSERT INTO {TMP_TABLE} VALUES ({placeholders})", rows)` — `placeholders = ", ".join(["?"] * len(columns))`; пустой `rows` → executemany не зовётся или зовётся с `[]` (0 вставок). Значения — строки/`None` дословно, **без CAST** (анти-паттерн). Закрыть `conn` в `finally` (`contextlib.suppress` на close).
  - [ ] **Temp→rename (AC #2, #4, #5, #6):** `tmp_path = partition_path.with_suffix(".parquet.tmp")` — **в том же каталоге** (та же ФС). `conn.table(TMP_TABLE).write_parquet(str(tmp_path))` (перезаписывает осиротевший `.tmp` от прошлого крэша — AC #6; путь — python-строкой, не в SQL — риск №1). Закрыть `conn`. Затем `os.replace(str(tmp_path), str(partition_path))` (**`os.replace`, не `os.rename`** — атомарно и на Windows, AC #4). На любом исключении после создания `.tmp` — удалить `.tmp` в `finally` (`os.path.exists` + `os.remove` под `suppress`), чтобы не оставлять частичный temp.
  - [ ] **Пустой день (AC #7):** при `rows == []` всё равно создать таблицу с колонками (схема из `columns`) и `write_parquet` → пустой типизированный Parquet; вернуть `0`. Это валидный загруженный день, не ошибка.
  - [ ] **Лёгкие гарды (рекомендуется, защищают FR-6 «без усечения»):** если ширина строки `len(row) != len(columns)` → fail-loud `ValueError` (не паддить/не резать молча); если `columns` пуст → `ValueError` (нечего писать/нет схемы). Дубль `storage_name` после маппинга → `ValueError` (коллизия колонки).
  - [ ] **НЕ делать:** `CAST`/типизацию (это view 2.6), захват `.writer.lock` (2.5/2.7), запись `load_state` (2.4), сверку строк как часть записи (2.3), `DROP`/удаление партиций, использование `DatabaseManager`/`gdau.duckdb`, добивку до полного каталога (риск №7).
- [ ] **Task 2 — Спека компонента `docs/ingestion.md` (часть DoD)**
  - [ ] Завести `docs/ingestion.md` человеческим языком (project-context: `ingestion.md` — логический компонент «приём: metrica_client + p81 + parquet_store + load_state»). На этом шаге описывает **запись дня в сырьё** (`parquet_store`); зеркалит, как 2.1 засеяла `working-layer.md` фундаментом. Три вопроса простыми словами: **(1) Что делает** — берёт разобранные строки одного дня одного источника и кладёт их в один файл-партицию (`data/raw/<источник>/<дата>.parquet`) **как есть, строками**, только переименовав колонки из «родных» имён Метрики в наши короткие (`snake_case`) по каталогу; **(2) Зачем** — это нижний слой хранения: данные сначала сохраняются дословно, без преобразований, чтобы ничего не потерять; запись сделана так, что файл дня появляется **целиком и сразу** (сначала во временный файл, потом мгновенная подмена) — поэтому сбой посреди записи не оставит «полу-дня»; **(3) Контракт** — вход: имя источника, дата, имена колонок и строки; выход: готовый файл-партиция и число записанных строк; обещания: один файл = один день одного источника, перезапись дня не трогает другие дни, валидный пустой день — это пустой файл со схемой (а не «нет данных»), значения не искажаются и не обрезаются. **Явно отметить границы:** сверка числа строк — 2.3; учёт загруженных дней (`load_state`) — 2.4; замок одного пишущего (`.writer.lock`) — 2.5; типизированные представления (`TRY_CAST`, парсинг массивов) — 2.6; полный цикл за день — оркестратор 2.7. Доступ к самому Logs API уже описан в `docs/metrica-client.md` — не дублировать. Без сигнатур кода.
- [ ] **Task 3 — Offline-тесты `tests/test_parquet_store.py` (AC: #1–#7)**
  - [ ] `from __future__ import annotations`; зеркалит `scripts/` → `tests/test_parquet_store.py`. Кросс-платформенно: только `tmp_path`/`pathlib`, без хардкода разделителей. Без сети (модуль сетей не знает). `monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))` задаёт корень хранилища (импортировать `DATA_ROOT_ENV` из `scripts.utils.env_reader`). Каталог инъектировать мини-`Catalog`: либо `load_catalog(_write_catalog(...))` (паттерн `tests/test_catalog.py`), либо собрать `Catalog(fields=(CatalogField(...),))` напрямую. Чтение записанной партиции для проверки — через `duckdb.connect().execute("SELECT … FROM read_parquet(<path>)")` (DuckDB уже в зависимостях; pandas не нужен).
  - [ ] **AC #1 (строки as-is + rename):** записать день visits с колонками `["ym:s:visitID","ym:s:dateTime","ym:s:watchIDs"]` и 2 строками; прочитать партицию → колонки называются `visit_id`/`date_time`/`watch_ids` (snake_case по каталогу), все типы VARCHAR, значения **дословно** совпадают со входом (массив `[8273645,8273646]` лежит строкой, не распарсен). Путь файла == `get_raw_partition_path("visits","2026-05-20")`.
  - [ ] **AC #2/#5 (temp→rename, та же ФС):** после успешной записи `partition_path.is_file()` True, `*.parquet.tmp` **не существует** (подменён/убран). (Та же-ФС проверяется тем, что tmp лежит в `partition_path.parent` — assert на родителя tmp == родитель финала, без cross-FS.)
  - [ ] **AC #3 (идемпотентность, ровно одна партиция):** записать `visits/2026-05-20` и `visits/2026-05-21`; перезалить `2026-05-20` тем же входом → `2026-05-21` не изменился (mtime/содержимое), а `2026-05-20` читается с тем же числом/содержимым строк. **Не** проверять байт-равенство (DuckDB вшивает метаданные писателя — риск №6); сравнивать прочитанные строки/`count`.
  - [ ] **AC #4 (перезапись существующего):** записать день, затем записать его **снова** (файл уже существует) — без исключения (доказывает `os.replace`, а не `os.rename`; на Windows `os.rename` тут упал бы).
  - [ ] **AC #6 (stale tmp):** заранее создать осиротевший `…/2026-05-20.parquet.tmp` с мусором; запись дня проходит, финальная партиция корректна, `.tmp` после записи отсутствует.
  - [ ] **AC #7 (пустой день):** `rows=[]`, `columns` непуст → файл партиции создан, `read_parquet` даёт `count == 0` и **колонки на месте** (схема есть); функция вернула `0`. День валиден (не ошибка).
  - [ ] **Негативные/гарды:** неизвестная колонка (нет в каталоге) → `pytest.raises(ValueError)`; `source="sessions"` → `ValueError` (через `get_raw_partition_path`); (если ввёл) строка неверной ширины → `ValueError`. Битый корень: `monkeypatch.delenv(DATA_ROOT_ENV)` → `ValueError` (наследуется из `paths`), и **ни одного файла/каталога не создано** (assert на отсутствие side-effect, как в `test_paths.py`).
  - [ ] **Анти-зависимость (через `ast`, не подстроку — docstring упоминает pandas/polars):** распарсить `ast` модуля, проверить отсутствие top-level import (`name.split(".")[0]`) на `pandas`/`polars`/`numpy`/`pyarrow` и на directaiq-инфру `config_manager`/`base_script`. Приём — из `tests/test_database_manager.py:202`/`tests/test_catalog.py`. **Это ключевой тест:** именно он фиксирует, что Parquet пишется DuckDB, а не запрещённым стеком (риск №1).
  - [ ] **Live-тест НЕ нужен** (и не заводить): `parquet_store` не ходит в сеть, DuckDB локален. Правило opt-in live ([[realapi-smoke-tests]]) — только для компонентов внешнего API (Logs API). Зафиксировать в Dev Agent Record, чтобы отсутствие live-набора не сочли упущением.
- [ ] **Task 4 — Гейты верификации (обязательны перед закрытием)**
  - [ ] `uv run mypy scripts` → зелено (strict; полная типизация; `Iterable`/`Sequence` из `collections.abc`; `duckdb` несёт стабы — `Any`-дыр не нужно). Новых зависимостей нет (`duckdb` уже пин) → **`uv.lock` не меняется**.
  - [ ] `uv run pytest` → зелено (новый offline-набор + регрессия 1.x/2.1; live отсеян `addopts="-m 'not live'"`).
  - [ ] Прогнать чек-лист «Definition of Done» из Dev Notes.

## Dev Notes

### Рекомендуемый контракт `parquet_store.write_partition` (финализируй под p81 2.7)

| Параметр | Тип | Смысл |
|---|---|---|
| `source` | `str` | `visits`/`hits` (валидируется через `get_raw_partition_path` → `VALID_SOURCES`) |
| `date` | `str` | уже отформатированная `YYYY-MM-DD` (форматирование/валидация дат — `dates.py` 1.4, здесь не дублируется) |
| `columns` | `Sequence[str]` | родные имена Метрики в порядке TSV-заголовка (`ym:s:visitID`, …); дают и схему, и порядок колонок |
| `rows` | `Iterable[Sequence[str \| None]]` | разобранные строки дня; TSV-ячейки дословно (массивы — строкой); `[]` = пустой день |
| `catalog` | `Catalog \| None` | инъектируемый шов; `None` → `load_catalog()` (прод-путь от модуля) |
| **возврат** | `int` | число записанных строк данных (для p81/сверки 2.3) |

**Почему вход — родные имена + строки, а не storage-имена.** TSV-заголовок Logs API несёт именно `ym:s:*`/`ym:pv:*` (см. `tests/fixtures/logs_visits_sample.tsv`). Переименование в `snake_case` по каталогу — это «единственное преобразование» из AC #1, поэтому оно живёт **в `parquet_store`** (один центр lossless-переименования), а p81 просто отдаёт распарсенный TSV. Схему пустого дня (AC #7) задаёт `columns` — p81 передаёт запрошенный набор полей даже при нуле строк (он знает его из `catalog.metrica_fields(source)`).

### Механизм записи Parquet без pandas/polars (риск №1 — образец потока)

```
conn = duckdb.connect()                      # in-memory, транзиентный кодировщик (НЕ gdau.duckdb)
try:
    cols_ddl = ", ".join(f'"{n}" VARCHAR' for n in storage_names)
    conn.execute(f'CREATE TABLE _raw ({cols_ddl})')
    if rows_list:                            # пустой день → пропускаем вставку, схема уже есть
        ph = ", ".join(["?"] * len(storage_names))
        conn.executemany(f'INSERT INTO _raw VALUES ({ph})', rows_list)
    conn.table("_raw").write_parquet(str(tmp_path))   # python-путь, НЕ COPY '<sql-литерал>'
finally:
    with contextlib.suppress(Exception):
        conn.close()
os.replace(str(tmp_path), str(partition_path))        # атомарно и на Windows
```

- `duckdb.connect()` без аргумента = in-memory; пишет Parquet штатно. **Не** через `DatabaseManager` (та про `gdau.duckdb`).
- `DuckDBPyRelation.write_parquet(file_name)` принимает python-строку пути → нет SQL-инъекции/проблем с кавычками в корне хранилища (предпочесть `COPY … TO`).
- `executemany` глотает `list`/`Iterable` последовательностей строк — чистый stdlib, без pandas/arrow. Объём данных мал (project-context: «единицы–десятки МБ/мес»), построчная вставка приемлема.
- Все колонки VARCHAR → значения хранятся дословно; типизация — забота view (2.6).

### Раскладка и инварианты сырья (источник: architecture.md 306–323, 364–367; project-context «Data & Domain»)

```
{GDAU_DATA_ROOT}/data/raw/{visits|hits}/{YYYY-MM-DD}.parquet   # пишет parquet_store (2.2)
                         /{YYYY-MM-DD}.parquet.tmp             # временный, в том же каталоге → os.replace
```

- Один файл = один день одного источника; `source ∈ {visits, hits}`.
- storage-имена строго `snake_case`; родное `ym:s:*` живёт ТОЛЬКО в каталоге, в Parquet-колонках — `visit_id` и т.п.
- Значения сырья — строками, без CAST/усечения/дедупа. Массивы (`watch_ids` = `[…]`) хранятся строкой, парсятся в `LIST` во view (2.6).
- В dev-репо данные **не пишутся** — всё под `GDAU_DATA_ROOT` (резолвит `paths.py`, fail-loud на битом корне).

### Протокол атомарного дня (где 2.2 в цепочке; источник: architecture.md 379–381, 536–538)

`download parts → собрать день (p81) → [2.2] запись в .tmp → атомарный rename → сверка строк (2.3) → load_state (2.4)`.
2.2 владеет **только** «запись в `.tmp` → атомарный rename». Точка «день загружен» — это запись `load_state` после rename+сверки (2.4/2.7), не выход `write_partition`. Перезалив дня = перезапись одного файла, **никогда** `DROP`.

### Паттерны от историй 1.x/2.1 (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` первой строкой; русский модульный docstring (роль компонента); идентификаторы — английские; docstrings — русские.
- Type hints везде, `mypy --strict` по `scripts`, без `Any`-дыр. Абсолютные импорты от корня пакета (`from scripts.utils.X import Y`).
- Fail-loud `ValueError`/`RuntimeError` с понятным русским сообщением и контекстом (путь, имя колонки/источника) — **никогда** «голый» трейсбек/сырой `OSError`/`duckdb.Error` наружу (см. патчи ревью 2.1: обёртка `mkdir`-`OSError` → `RuntimeError`). Логирование — stdlib `logging` (`logger = logging.getLogger(__name__)`); путь — не секрет, печатать можно.
- Тесты: `tmp_path`/`monkeypatch`/`pytest.raises`, зеркалят `scripts/`; **анти-зависимость через `ast`** (не подстроку — docstring содержит `pandas`/`polars`). Кросс-платформенно (ubuntu + windows).
- **НЕ** заводить `logging_utils.py` — модули используют `logging.getLogger(__name__)` напрямую.

### Границы 2.2 (не выходить)

- Только один модуль: `scripts/utils/parquet_store.py` (+ его спека `docs/ingestion.md` + тесты). **Не** реализуем: сверку строк (2.3), `load_state`/реконсиляцию (2.4), `.writer.lock`-захват (2.5), view'ы/`TRY_CAST`/парсинг массивов (2.6), TSV-парсинг и p81-оркестрацию (2.7), инкремент/hot-window (2.8), `gdau-logs update` (2.9).
- `parquet_store` не ходит в сеть, не открывает `gdau.duckdb`, не берёт локов, не типизирует данные. Только: разобранные строки + имена колонок → атомарная Parquet-партиция со storage-именами.

### Project Structure Notes

- Модуль — `scripts/utils/parquet_store.py` ровно по дереву архитектуры (строка 450, помечен `[новое]`). `scripts/utils/` — регулярный пакет (`__init__.py` из 1.1).
- Имена snake_case (модуль/функции); type hints обязательны (mypy strict). Не переводить на src-layout, не переименовывать пакет `scripts` (ломает резолюцию импортов — hatchling `packages=["scripts"]`).
- `tests/` зеркалит `scripts/`: `tests/test_parquet_store.py`. Конфиг pytest (`markers`/`addopts`) уже есть (1.3/1.6); `conftest.py` в проекте нет — тесты используют `tmp_path`/`monkeypatch` напрямую. Архитектурное дерево упоминает `test_parquet_atomic.py` (строка 483) как ориентир — назови `test_parquet_store.py` (зеркалит модуль; имя теста — не контракт).
- `docs/ingestion.md` — **заводится** (Task 2): project-context называет его логическим компонентом приёма. Часть DoD.
- `uv.lock` не трогаем — stdlib (`os`/`contextlib`/`logging`) + уже пинятый `duckdb`. Новых зависимостей нет. Не реорганизовывать раскладку.

### Definition of Done — чек-лист самопроверки

1. `scripts/utils/parquet_store.py` — `write_partition(...)`: строки as-is в VARCHAR, переименование колонок по каталогу (неизвестная → fail-loud), путь из `get_raw_partition_path`, запись через **in-memory DuckDB** (НЕ pandas/polars, НЕ `gdau.duckdb`), temp в том же каталоге → `os.replace`. (AC #1, #2, #4, #5)
2. Перезалив = одна партиция через `os.replace`, без `DROP`; повтор идемпотентен по содержимому; другие дни нетронуты. (AC #3)
3. Осиротевший `.tmp` перезаписывается/убирается; на фейле записи `.tmp` не остаётся. (AC #6)
4. Пустой день (0 строк) → пустая партиция со схемой, возврат `0`, день валиден. (AC #7)
5. `docs/ingestion.md` заведён (3 вопроса простыми словами; границы 2.3/2.4/2.5/2.6/2.7 названы; ссылка на `metrica-client.md` без дублей) — DoD компонента. (Task 2)
6. Offline-тесты покрывают AC #1–#7 + анти-зависимость по `ast` (нет pandas/polars/pyarrow/numpy — Parquet пишет DuckDB). Live-набор осознанно отсутствует (нет внешнего API). (Task 3)
7. `uv run mypy scripts` и `uv run pytest` — зелёные; `uv.lock` не менялся (новых зависимостей нет); `data/`-артефактов в dev-репо не создано.
8. Велась в отдельной ветке `story/2.2-parquet-store` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС (ubuntu + windows). PR в `main`.

### Latest Tech Information

- **DuckDB Python (`>=1.5,<1.6`) как Parquet-писатель:** `duckdb.connect()` (без аргумента) — in-memory соединение. `DuckDBPyRelation.write_parquet(file_name: str, compression="snappy", ...)` пишет Parquet по python-пути (предпочесть `COPY … TO '<sql-литерал>'`, чтобы путь не попадал в SQL). `conn.table("name")` возвращает relation таблицы. `conn.executemany(sql, list_of_sequences)` — массовая вставка чистых Python-данных без pandas/arrow. Пустая таблица + `write_parquet` пишет валидный Parquet со схемой (нужно для AC #7).
- **`os.replace(src, dst)` vs `os.rename`:** `os.replace` атомарно перезаписывает существующий `dst` на **обеих** платформах; `os.rename` поверх существующего файла бросает `FileExistsError`/`PermissionError` на Windows. Атомарность гарантирована только в пределах одной ФС → `.tmp` обязан лежать в каталоге назначения (AC #5).
- **Parquet не байт-детерминирован:** DuckDB вшивает метаданные писателя (версию) → один и тот же вход может дать не-идентичные байты при идентичном содержимом. Идемпотентность (AC #3) проверять по строкам/`count`, не по байтам/хэшу файла.
- **Web-ресёрч не требуется:** API DuckDB/stdlib стабильны и зафиксированы локом; внешнего сетевого контракта в истории нет (live-smoke неприменим).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.2] (строки 249–263) — user story + 7 AC (усилены edge-case hunter).
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2] (строки 118–120, 230–232) — роль 2.2 в упорядоченной цепочке 2.1→2.9; ядро NFR-1.
- [Source: _bmad-output/planning-artifacts/epics.md#FR-6,FR-14] (строки 30, 42) — Parquet-сырьё по дням строками; атомарная запись `.tmp`→rename; партиция самодостаточна.
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming/Format Patterns] (строки 306–323, 364–367) — storage snake_case; значения сырья строками без CAST; партиция `data/raw/{source}/{date}.parquet`; temp→rename; массивы строкой→`LIST` во view.
- [Source: _bmad-output/planning-artifacts/architecture.md#Протокол идемпотентного дня] (строки 379–381) — download→собрать→`.tmp`→сверка→rename→`load_state`; перезалив = один файл, без `DROP`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Directory Structure] (строка 450) — `utils/parquet_store.py` `[новое]`: «запись дня temp→rename; data/raw/{source}/{date}.parquet».
- [Source: _bmad-output/planning-artifacts/architecture.md#Requirements to Structure] (строки 526, 528–529) — FR-6 → `parquet_store.py`; FR-10/14 → `parquet_store.py` (temp→rename).
- [Source: _bmad-output/planning-artifacts/architecture.md#Integration Points] (строки 536–538) — поток приёма: p81 под `.writer.lock` → `parquet_store` пишет `.tmp` → сверка → rename → `load_state`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Boundaries] (строки 510–518) — данные только под `GDAU_DATA_ROOT`; запись (p81) = `.writer.lock` + атомарный Parquet; в dev-репо данные не пишутся.
- [Source: _bmad-output/project-context.md#Data & Domain] (строки 96–111) — сырьё строками, lossless-переименование по каталогу, раскладка `data/raw`, temp→rename, перезалив одного файла, «в dev-репо данные не пишутся».
- [Source: _bmad-output/project-context.md#Anti-patterns] (строки 173–189) — НЕ: CAST в сырье, `DROP` ради перезалива, `pandas`/`polars`, угадывание типов, запись данных в dev-репо.
- [Source: _bmad-output/project-context.md#Документация компонентов] (строки 52–77, особ. 60) — `ingestion.md` = логический компонент приёма (включает `parquet_store`); спека как DoD.
- [Source: scripts/utils/paths.py:101] — `get_raw_partition_path(source, date)` (резолвер пути партиции, валидирует source, наследует fail-loud корня). Также `get_raw_source_dir` (для views 2.6).
- [Source: scripts/utils/catalog.py:158,166] — `Catalog.fields_for(source)`/`metrica_fields(source)`: маппинг `metrica_field`↔`storage_name`; `load_catalog(path=...)` — инъектируемый шов; `VALID_SOURCES`.
- [Source: scripts/utils/metrica_client.py:298] — `download_log_request_part(request_id, part) -> bytes` (сырой TSV; парсинг в строки — p81 2.7, не здесь).
- [Source: scripts/utils/database_manager.py] — образец: контекст-менеджер с `finally`-close, обёртка `OSError`/`duckdb.Error` → `RuntimeError`. NB: `parquet_store` его НЕ использует (`gdau.duckdb` ≠ сырьевые партиции).
- [Source: tests/fixtures/logs_visits_sample.tsv] — формат TSV: заголовок `ym:s:visitID\tym:s:dateTime\tym:s:watchIDs`, массив как строка `[8273645,8273646]`. Основа для строк-фикстур теста.
- [Source: tests/test_database_manager.py:202, tests/test_catalog.py] — паттерн offline-тестов: `monkeypatch`/`tmp_path`/`pytest.raises`, анти-зависимость через `ast` (import-узлы + запрещённые имена).
- [Source: _bmad-output/implementation-artifacts/2-1-соединение-duckdb-и-резолюция-путей-хранилища.md] — образец качества/структуры истории; патчи ревью (обёртка `OSError`, `is_absolute`-гейт) — учить контракт fail-loud.
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] (строка 27) — defer 1.6: атомарная запись `download` (temp→rename под `.writer.lock`) отнесена к Epic 2 (2.2/2.7) — эта история частично его закрывает (сторона записи Parquet).
- [Source: docs/metrica-client.md, docs/working-layer.md] — образцы человекочитаемой спеки (3 вопроса + «Границы») для `docs/ingestion.md`.
- [Memory: simplicity-first] — простота как инвариант (Parquet пишем имеющимся DuckDB, без нового стека). [[directaiq-reference]] — `parquet_store` НЕ вендоринг (у directaiq нет аналога модели «Parquet по дням»). [[realapi-smoke-tests]] — live применим только к внешнему API → в 2.2 не нужен. [[cli-tools-ai-native]] — поверхность приёма скриптуема (контекст для p81 2.7/`gdau-logs update` 2.9).

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
