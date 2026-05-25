# Story 2.6: Рабочий слой — view'ы из каталога с `TRY_CAST`

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want типизированные view'ы DuckDB поверх Parquet, сгенерированные из каталога-SSOT,
so that SQL агента работал с корректными типами (HUGEINT/DATE/массивы), не падая на битой ячейке.

**Контекст эпика.** Шестая история Epic 2 «Приём данных и безопасное обновление хранилища». Сырьевая половина пути записи стоит целиком: 2.1 (`done`) — `paths.py` (в т.ч. **`get_raw_source_dir(source) → {root}/data/raw/{source}`**, уже готов «для views.py 2.6») + `database_manager.py` (контекст-менеджер соединения write/read-only); 2.2 (`done`) — `parquet_store.write_partition` пишет день строками as-is с **переименованием колонок в `snake_case` по каталогу**; 2.3 (`done`) — жёсткая сверка строк; 2.4 (`done`) — `load_state` + реконсиляция; 2.5 (`ready-for-dev`) — `.writer.lock`. Теперь 2.6 строит **читаемую половину** — рабочий слой: модуль `scripts/utils/views.py` генерирует DDL типизированных view'ов `visits`/`hits` поверх Parquet-партиций. Покрывает **FR-7** (рабочий слой DuckDB-view + `TRY_CAST`; ID → HUGEINT; битая ячейка → NULL) и **FR-3** (оба источника visits+hits). Это финальное звено решения OQ#3 «рабочий слой = view'ы, не материализованные таблицы» (architecture.md:191/206/318/430).

**Почему рабочий слой именно view'ы (корень требования).** Сырьё (Parquet) хранится **строками as-is**, без типов (FR-6, инвариант сырьевого слоя — 2.2). Чтобы агент писал осмысленный SQL (`WHERE date >= …`, `SUM(page_views)`, `len(watch_ids)`), нужен **типизированный** доступ. Решение OQ#3 — **view'ы поверх Parquet с `TRY_CAST`**, а НЕ материализованные таблицы:
- партиционирование по дню → partition pruning; DuckDB проталкивает фильтры/проекции в чтение Parquet → быстро даже на гигабайтах (architecture.md:266–270);
- **view отражает текущий Parquet сразу** — перезалив партиции (2.2/2.8) виден без пере-материализации (AC #4);
- view → таблицы = обратимый escape hatch (`CREATE TABLE AS SELECT`), не one-way; страховка развязана с приёмом и каталогом (architecture.md:269).

**Что потребляет рабочий слой (проектируй API под них).**
- **MCP-чтение (3.1)** — главный потребитель: `duckdb_query` гоняет произвольный SQL по view'ам `visits`/`hits` через **read-only** соединение (architecture.md:539). Лок писателя НЕ берёт ([[realapi-smoke-tests]] неприменим; граница 2.5/3.1).
- **MCP-контекст (3.3)** — `--context` отдаёт таблицы/view'ы, типы, row counts, диапазоны дат; **пустые view'ы (нет партиций) → `row_count=0`, `date_range=null`** обрабатываются им штатно (epics.md:430). То есть для 3.3 ценнее **существующий пустой типизированный view**, чем отсутствующий объект (см. риск №3, AC #6).
- **init `gdau-init` (4.3)** — создаёт `gdau.duckdb` и **сразу разворачивает view'ы из каталога** на свежем хранилище без данных (epics.md:477, 490 — «толерантно к нулю партиций, см. 2.6»). Прямой потребитель `create_views(conn)`.
- **p81 (2.7)** — view'ы лениво отражают свежезалитые партиции; пере-создавать view на каждый день НЕ нужно (идемпотентность через `CREATE OR REPLACE`). 2.7 может вызвать `create_views` один раз для гарантии существования объекта.

**Это НЕ вендоринг — новый модуль.** `views.py` помечен в дереве архитектуры как **`[новое]`** (architecture.md:451 «DDL view'ов из каталога (TRY_CAST)»). У directaiq рабочий слой строился иначе (CSV→DuckDB с CAST в самой таблице, `ym:s:*`-имена в таблицах) — наша Parquet-модель и snake_case-имена это **осознанное отличие** (architecture.md:316, 548). Прямого аналога нет — пиши с нуля по контракту ниже. Дерево тестов архитектуры называет `test_views.py` (architecture.md:484) — естественное зеркало.

### Главные риски / решения (читать до кода)

> ✅ **РЕШЕНИЕ ШЕФА (2026-05-24): вариант A — нативный `TRY_CAST(col AS T[])` УТВЕРЖДЁН.** Epic AC #8 буквально требовал «применяется явная функция парсинга массива, а не голый `TRY_CAST`» из предпосылки, что массив приходит **«не литералом DuckDB-list»**. Проверка реального формата (фикстура + эмпирический прогон DuckDB 1.5.3) эту предпосылку **опровергла**: массивы приходят как `[8273645,8273646]` — формат, который нативный `TRY_CAST(col AS HUGEINT[])` парсит корректно (включая пустой `[]` → `[]`), а «явный разбор» парсит **хуже** (пустой `[]` → `[None]`, баг). Шеф утвердил **вариант A**: **AC #8 трактуется по исходу** (массив типизирован в `LIST`, битая ячейка → NULL — как у скаляров), а не по букве. **Реализуй вариант A.** Вариант B (явный `string_split`+`list_transform`) ниже оставлен как разобранная отклонённая альтернатива (трассируемость решения). Контракт (билдер DDL, `create_views`, тесты, границы) от выбора зависит ровно в одном выражении типизации поля.

1. **Механизм типизации массивов: нативный `TRY_CAST(col AS T[])` (вариант A — РЕКОМЕНДОВАН) vs явный разбор `string_split`+`list_transform` (вариант B — буква AC #8).**

   **Вариант A — нативный `TRY_CAST(col AS T[])` (РЕАЛИЗОВАТЬ ЭТОТ при подтверждении Шефа).** Массив и скаляр типизируются **одним и тем же выражением** `TRY_CAST("{storage}" AS {duckdb_type})`, где `duckdb_type` берётся из каталога (для массива это уже `HUGEINT[]`/`BIGINT[]`/`VARCHAR[]`/…). Эмпирически (DuckDB 1.5.3, пин `1.5.x`) нативный cast строки `[v1,v2]` в `T[]` корректно обрабатывает: многоэлементный `[8273645,8273646]`→`[8273645,8273646]`, одиночный `[v]`, **пустой `[]`→`[]`**, `NULL`→`NULL`, мусор→`NULL` (битая ячейка → NULL, **тот же контракт FR-7, что у скаляров**). Билдер DDL получается **единообразным** — ноль спец-кейсов на массивы, что есть [[simplicity-first]].

   **Вариант B — явный `list_transform(string_split(trim(col,'[]'), ','), x -> TRY_CAST(x AS T))` (буква AC #8).** Поэлементный разбор: даёт per-element NULL (одна битая ячейка массива → NULL-элемент, не весь массив NULL). Но: **(а)** эмпирически на пустом `[]` даёт `[None]` вместо `[]` (требует доп-гарда `CASE WHEN col='[]' THEN ... END`); **(б)** на строковых массивах (`parsed_params_key*`, `products_name`, … — `VARCHAR[]`, 30+ полей каталога) наивный `split(',')` **рвёт значения с запятой/скобкой внутри** — а реальный формат экранирования Метрики для строковых массивов **не подтверждён** (`yandex-docs/` молчит, см. defer 2.3 в `deferred-work.md:35`); **(в)** больше движущихся частей, спец-кейс на `[]`-типы в билдере. Сложнее и на наблюдаемых данных **менее** корректен, чем A.

   **Вывод:** A достигает исхода AC #8 (массив → `LIST`, битая → NULL) проще и на наблюдаемых данных корректнее; B буквально следует AC #8, но платит сложностью и хуже на пустом массиве. **Реализуй A до иного решения Шефа.** Контракт (билдер DDL, `create_views`, тесты, границы) от выбора A/B зависит ровно в одной точке — выражении типизации поля.

2. **Путь партиций уходит в DDL view'а СТРОКОВЫМ ЛИТЕРАЛОМ — экранировать кавычки + posix-слеши (критично, кросс-платформенно).** В отличие от `parquet_store` (2.2), где путь шёл python-API `write_parquet(path)` мимо SQL, **определение view обязано встроить путь в текст DDL** (`CREATE VIEW … FROM read_parquet('<glob>')`) — биндинг-параметров в DDL нет. Поэтому: **(а)** одинарную кавычку в пути экранировать удвоением (`'` → `''`) — корень хранилища контролируется, но не выпускать сырой путь в SQL (дисциплина, дух риска №1 из 2.2); **(б)** в glob-строку класть `Path.as_posix()` (прямые слеши) — на Windows `read_parquet('D:\\…\\*.parquet')` с обратными слешами неоднозначен для glob-движка; `as_posix()` (`D:/…/*.parquet`) работает на обеих ОС (CI гоняет ubuntu + windows). Путь резолвить через `get_raw_source_dir(source)` (2.1, fail-loud при битом корне ДО построения DDL).

3. **Пустой источник → пустой ТИПИЗИРОВАННЫЙ view, не отсутствующий объект (AC #6, критично для 3.3/4.3).** `read_parquet('{dir}/*.parquet')` по каталогу без `.parquet`-файлов **бросает ошибку** «No files found» при запросе (view ленивый — ошибка всплывёт у потребителя 3.1/3.3, не при создании). Поэтому при отсутствии партиций источника создавай view **из типизированной проекции NULL'ов без `FROM`**: `CREATE OR REPLACE VIEW {source} AS SELECT CAST(NULL AS {type}) AS "{storage}", … WHERE false` (эмпирически даёт 0 строк с правильными типами колонок). Так init (4.3) разворачивает view'ы на пустом хранилище (epics.md:490), а MCP `--context` (3.3) видит `row_count=0`/`date_range=null` штатно (epics.md:430) — лучше пустого-типизированного view, чем падающего/отсутствующего. **Граница visits×hits независима** (FR-3): один источник пуст, другой полон — каждый строится отдельно, пустой не валит другой. Проверку наличия партиций делать тем же приёмом, что `load_state._partition_dates` (2.4): `get_raw_source_dir(source)` + `.is_dir()` + `glob("*.parquet")` (`.parquet.tmp` не матчится).

4. **Дрейф схемы между партициями → `read_parquet(..., union_by_name => true)` (AC #7).** Новое поле каталога есть только в свежих партициях (FR-6: смена списка полей не мигрирует старые партиции). `union_by_name` объединяет партиции по **именам** колонок: колонка, отсутствующая в старых партициях, → `NULL` в них (вместо ошибки/сдвига). Без флага DuckDB матчит по позиции → дрейф дал бы кашу. **Известная транзиентная граница (НЕ в скоупе AC #7):** если поле добавлено в каталог, но **ни одной** партиции с ним ещё нет (каталог расширили до первого `update`), `TRY_CAST("new_col" …)` упадёт «column not found» при запросе. На практике окно ничтожно: p81 (2.7) выгружает **полный** список полей каталога на каждый день → каждая свежая партиция несёт все текущие колонки; окно живёт лишь между расширением каталога и следующим `update` и закрывается им. AC #7 говорит «есть **только в свежих** партициях» = колонка есть хотя бы где-то → это ровно случай `union_by_name`. Зафиксируй транзиентность в docstring; усложнять COALESCE'ом не нужно ([[simplicity-first]]).

5. **`TRY_CAST` даёт «битую ячейку → NULL», но НЕ per-cell лог (нюанс FR-7, зафиксировать).** FR-7/architecture.md:366 формулируют «битая ячейка → `NULL` + лог». В set-based view `TRY_CAST` реализует **NULL** идиоматично, но **per-cell логирование в SQL-view невозможно** (view — декларативный SELECT, не построчный обработчик). Это осознанная интерпретация: контракт «битая → NULL, день не падает» соблюдён; «+ лог» на уровне view сводится к опциональному **агрегатному** диагностическому запросу (`count(*)` где `TRY_CAST IS NULL AND raw IS NOT NULL`) — НЕ обязателен к реализации в 2.6 (можно отметить как возможное усиление). Не пытайся эмулировать per-cell лог во view (это ломает ленивость/производительность и противоречит «view, не материализация»).

6. **`views.py` — независимый билдер DDL: НЕ открывает `gdau.duckdb`, `conn` инъектируется, НЕ импортирует `database_manager`/`parquet_store`/`load_state`/`writer_lock` (граница, тестируемость; приём 2.4).** Как `load_state` (2.4): соединение **передаётся параметром** (`conn: duckdb.DuckDBPyConnection`), модуль БД сам **не** открывает — открытие/закрытие это `DatabaseManager` (2.1), захват `.writer.lock` вокруг записи DDL — забота вызывающего (init 4.3 / p81 2.7: создание/замена view'а пишет в каталог `gdau.duckdb` → write-conn под локом). Зависимости `views.py`: `catalog` (типы/имена полей — SSOT), `paths.get_raw_source_dir` (glob партиций), `duckdb` (только аннотация `conn`, как в `load_state`), stdlib `logging`/`re`. `duckdb` в анти-зависимости **разрешён** (как 2.4), `pandas`/`polars`/`numpy`/`pyarrow`/directaiq-инфра — запрещены.

7. **Идемпотентность через `CREATE OR REPLACE VIEW` (AC #4).** Каждый запуск пере-определяет view (не падает на существующем, не плодит дублей). View ленив → отражает текущий Parquet **без** материализации: перезалив партиции (2.2/2.8) виден следующим запросом сразу, пере-создавать view не нужно. Имя view = имя источника (`visits`/`hits`, snake_case — architecture.md:322).

8. **Имена колонок — snake_case storage-имена; родные `ym:s:*`/`ym:pv:*` в SQL view НЕ появляются (AC #5, инвариант).** Партиции 2.2 уже хранят колонки в snake_case (переименование по каталогу — забота `parquet_store`). View ссылается на storage-имена (`"visit_id"`, `"date"`, `"watch_ids"`), они же — выход. Родное имя Метрики живёт **только** в каталоге (`metrica_field`), в SQL агента его нет (project-context:98, anti-pattern «`SELECT "ym:s:visitID"`»). Квотируй идентификаторы двойными кавычками (`"date"` — зарезервированное-похожее имя колонки; единообразно для всех).

9. **Чтение партиции во время перезалива (Windows `os.replace`) — граница, НЕ дыра 2.6 (defer 2.2).** `deferred-work.md:31`: на Windows `os.replace` поверх партиции, **открытой читателем**, → `PermissionError`. View сам по себе **ленив и дескриптор НЕ держит** в покое — файл открывается лишь на время активного запроса. Поэтому само создание view (2.6) контакта запись↔чтение не вносит. Реальная конкуренция «читатель держит партицию ⟷ писатель её заменяет» — рантайм-забота **MCP-чтения (3.1)** (там живёт конкурентный read-канал; defer рекомендует ретрай на транзиентной ошибке чтения — epics.md:391 AC 3.1). В 2.6 лишь зафиксируй границу в docstring/Dev Agent Record, чтобы не сочли упущением.

## Acceptance Criteria

1. **Given** каталог (1.5) и партиции, **When** `views.py` генерирует DDL, **Then** создаются view'ы `visits` и `hits` поверх `data/raw/{source}/*.parquet` (оба источника, FR-3).
2. **Given** поле с working_type, **When** строится view, **Then** значение через `TRY_CAST`; битая ячейка → `NULL` + лог, view не падает.
3. **Given** ID-поля, **When** типизируются, **Then** `visit_id`/`client_id`/`watch_id` → HUGEINT.
4. **Given** перезалив партиций, **When** запрос к view, **Then** view отражает текущий Parquet без отдельной материализации (OQ#3 — view'ы).
5. **Given** snake_case storage-имена, **When** агент пишет SQL, **Then** колонки доступны как `visit_id`/`date`/…, родные `ym:s:*` в SQL не используются.
6. **Given** пустой `data/raw` (нет партиций) ИЛИ один источник пуст (есть visits, нет hits), **When** строятся view'ы, **Then** для пустого источника создаётся пустой типизированный view (или skip с уведомлением), не валя другой источник. _[edge-case: пустой источник]_
7. **Given** дрейф схемы (новое поле каталога есть только в свежих партициях, FR-6), **When** view читает партиции, **Then** используется `union_by_name`, отсутствующая в старых партициях колонка → `NULL`. _[edge-case: дрейф схемы между партициями]_
8. **Given** массивное поле приходит в TSV строкой (не литералом DuckDB-list), **When** оно типизируется в `T[]`/`LIST`, **Then** применяется явная функция парсинга массива, а не голый `TRY_CAST`. _[edge-case: парсинг TSV-массива]_

> **Примечание к AC #8 (механизм — РЕШЕНО, вариант A утверждён Шефом 2026-05-24).** Предпосылка AC #8 «массив приходит **не литералом DuckDB-list**» проверена и **не подтверждена**: реальный формат `[8273645,8273646]` нативно парсится `TRY_CAST(col AS T[])`, причём корректнее явного разбора (пустой `[]`: native → `[]`, явный → `[None]`). Шеф утвердил **вариант A** (нативный `TRY_CAST`): AC #8 трактуется **по исходу** — массив типизирован в `LIST`, битая ячейка → NULL (как у скаляров). Вариант B (явный `string_split`+`list_transform`) отклонён (сложнее, баг на `[]`, рвёт строковые массивы). См. риск №1. AC #1–#7 от выбора не зависят. **Окончательное подтверждение формата (особенно экранирование строковых массивов с запятой/скобкой внутри) — за live-smoke оркестратора 2.7; фикстуры offline освежить из реального ответа** ([[realapi-smoke-tests]], defer 2.3 `deferred-work.md:35`).

> **Примечание к AC #2 («+ лог»).** `TRY_CAST` даёт «битая → NULL» идиоматично; per-cell лог в декларативном view невозможен (риск №5). Контракт «битая → NULL, день не падает» соблюдён; per-cell логирование — не цель view (опц. агрегатная диагностика — возможное усиление, не обязательна).

## Tasks / Subtasks

- [ ] **Task 1 — `scripts/utils/views.py`: генерация DDL типизированных view'ов из каталога (AC: #1, #2, #3, #4, #5, #6, #7, #8)**
  - [ ] `from __future__ import annotations` первой строкой. Русский модульный docstring: роль (рабочий слой — типизированные view `visits`/`hits` поверх Parquet-партиций; `TRY_CAST` по типам каталога; битая ячейка → NULL; ID → HUGEINT; массивы → `LIST`; `union_by_name` для дрейфа схемы; `CREATE OR REPLACE` идемпотентно; view ленив → отражает текущий Parquet без материализации, OQ#3). Явно отметить **границы**: типы/имена полей — `catalog` (1.5, SSOT); путь партиций — `paths.get_raw_source_dir` (2.1); открытие/закрытие БД — `DatabaseManager` (2.1, `conn` инъектируется); запись Parquet — 2.2; учёт дней — 2.4; `.writer.lock` вокруг записи DDL — вызывающий (init 4.3 / p81 2.7); чтение/анализ и конкуренция читатель↔писатель — MCP (3.1). Подчеркнуть, что модуль **НЕ** открывает `gdau.duckdb` и **НЕ** импортирует `database_manager`/`parquet_store`/`load_state`/`writer_lock` (риск №6). Импорты: `import logging`, `from collections.abc import Iterable`, `import duckdb` (аннотация `conn`, как `load_state`), `from scripts.utils.catalog import Catalog, load_catalog, VALID_SOURCES`, `from scripts.utils.paths import get_raw_source_dir`. `logger = logging.getLogger(__name__)`. `__all__` с публичными именами.
  - [ ] **Чистый билдер DDL (тестируемый без БД) — `build_view_ddl(source, catalog, *, partition_glob, has_partitions) -> str`** (финализируй сигнатуру под удобство тестов): возвращает строку `CREATE OR REPLACE VIEW`. Это **главный тестируемый шов** — тесты ассертят содержимое DDL без живой БД (есть `TRY_CAST`, `HUGEINT`, `union_by_name`, snake_case-имена, пустой-источник-форма) — как чистые функции `paths`/`catalog`.
    - **Типизированная проекция (общая для обеих веток):** по `catalog.duckdb_types(source)` (отдаёт `dict[storage_name → duckdb_type]` в порядке каталога) на каждое поле — выражение `TRY_CAST("{storage}" AS {duckdb_type}) AS "{storage}"` (**вариант A:** массив и скаляр единообразны, т.к. `duckdb_type` уже `T[]` для массивов — риск №1). Идентификаторы в `"…"` (AC #5, риск №8).
    - **Непустой источник (есть `*.parquet`):** `… FROM read_parquet('{glob}', union_by_name => true)` (риск №4, AC #7). `{glob}` — `get_raw_source_dir(source).as_posix()` + `/*.parquet`, одинарные кавычки экранированы удвоением (риск №2).
    - **Пустой источник (нет партиций, AC #6, риск №3):** проекция из `CAST(NULL AS {duckdb_type}) AS "{storage}"` + `WHERE false` (без `FROM`) — 0 строк, типы колонок корректны. Эмпирически подтверждено (DuckDB 1.5.3).
  - [ ] **Исполнитель — `create_views(conn, *, catalog=None, sources=VALID_SOURCES) -> None`:** на каждый источник определить наличие партиций (`get_raw_source_dir(source).is_dir()` + `glob("*.parquet")` непуст — приём `load_state._partition_dates` 2.4, риск №3; `.parquet.tmp` не матчится), собрать DDL `build_view_ddl(...)` и `conn.execute(ddl)`. `catalog=None` → `load_catalog()` (прод-путь, шов как `parquet_store`). `source` валидируется (`VALID_SOURCES`, fail-loud — переиспользовать приём). Лог INFO на каждый созданный view (имя + источник + пустой/N-партиций). **НЕ** открывать БД, **НЕ** брать лок, **НЕ** делать `mkdir`.
  - [ ] **НЕ делать:** материализовать view в таблицу (OQ#3 — именно view, риск №7); CAST в сырьевом слое (сырьё строки — забота 2.2); per-cell лог битых ячеек во view (риск №5); голый `read_parquet` без `union_by_name` (риск №4); путь в DDL без экранирования/`as_posix` (риск №2); родные `ym:s:*`/`ym:pv:*` в SQL view (риск №8, AC #5); открытие `gdau.duckdb`/импорт `database_manager`/`parquet_store`/`load_state`/`writer_lock` (риск №6); захват `.writer.lock` (забота вызывающего); `mkdir` (резолверы `paths` чистые); спец-кейс на `[]`-типы при варианте A (массив = скаляр, риск №1).
- [ ] **Task 2 — Спека компонента `docs/working-layer.md` (часть DoD)**
  - [ ] **Дополнить** существующий `docs/working-layer.md` (он сам в шапке обещает: «Типизированные представления данных и разбор типов добавятся к этому же файлу в истории 2.6») новым разделом **«типизированные представления»** человеческим языком, без жаргона и сигнатур кода. Три вопроса простыми словами: **(1) Что делает** — поверх сырых дневных файлов (где всё лежит **текстом**, как пришло) строит **типизированные представления** `visits` и `hits`: числа становятся числами, даты — датами, списки (например, просмотры визита) — списками; если значение в сырье «битое» и не приводится к нужному типу, на его месте оказывается **пусто** (NULL), а представление при этом **не ломается** и остальные строки читаются; **(2) Зачем** — сырьё намеренно хранится дословно (чтобы ничего не потерять и не исказить на входе), но анализировать удобно типы; представление даёт агенту **готовый типизированный взгляд** на те же файлы, ничего не копируя и не дублируя — поэтому свежезалитые данные видны **сразу**, без пересборки; **(3) Контракт** — представления строятся **из каталога** (он решает, какие поля и какого типа); колонки названы понятными короткими именами (`visit_id`, `date`, `watch_ids`), родные технические имена Метрики в запросах не нужны; если данных по источнику ещё нет, представление всё равно создаётся — **пустым, но с правильными типами** (чтобы инструменты-навигаторы его видели). **Явно отметить границы:** сами файлы пишет приём (2.2), порядок/учёт дней — мета и оркестратор (2.4/2.7), а **читают** представления — анализ через MCP (3.1/3.3, представления — только чтение, замок не берут). Не дублировать `ingestion.md`/`catalog.md`.
- [ ] **Task 3 — Offline-тесты `tests/test_views.py` (AC: #1–#8)**
  - [ ] `from __future__ import annotations`; зеркалит `scripts/` → `tests/test_views.py`. **Без сети, без внешнего API.** Кросс-платформенно (`tmp_path`/`pathlib`; CI гоняет ubuntu + windows — `union_by_name`/glob/`as_posix` обязаны работать на обеих). Каталог инъектируется мини-`Catalog` (как `test_parquet_store.py` собирает `Catalog`/`CatalogField` напрямую — без чтения большого CSV) ИЛИ мини-фикстурой CSV через `load_catalog(path=...)`. БД — транзиентный `duckdb.connect()` (in-memory) ИЛИ `monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))` для прод-пути партиций; `duckdb` импортировать в тесте можно (как `test_load_state.py`).
  - [ ] **Чистый билдер DDL (без БД):** `build_view_ddl("visits", catalog, …)` для непустого источника содержит `CREATE OR REPLACE VIEW visits`, `TRY_CAST`, `union_by_name`, типы каталога (`HUGEINT`, `DATE`, `HUGEINT[]`), квотированные snake_case-имена (`"visit_id"`, `"watch_ids"`), и **не содержит** `ym:s:` (AC #5). Для пустого источника — `WHERE false` и `CAST(NULL AS …)` без `read_parquet` (AC #6).
  - [ ] **AC #1 + #3 + #5 (интеграция, типы):** в `tmp_path` положить мини-партиции `data/raw/visits/2026-05-20.parquet` и `data/raw/hits/…` со snake_case-колонками **VARCHAR** (значения строками as-is, как пишет 2.2 — можно записать прямой `duckdb …write_parquet`, чтобы не импортировать `parquet_store` в модуль; в тесте `write_partition` использовать допустимо как хелпер). `create_views(conn)` → `SELECT visit_id, watch_ids, date FROM visits` отдаёт **HUGEINT/`LIST`/DATE** (проверить `typeof`/значения); `visit_id` за пределами BIGINT (напр. `17298374650000000001`) не переполняется (HUGEINT, NFR-4); родное `ym:s:*` в запросе не используется.
  - [ ] **AC #2 (битая ячейка → NULL, view не падает):** партиция с битым значением в типизируемой колонке (напр. `page_views='abc'` для INTEGER, `date='not-a-date'`) → `SELECT` проходит, битая ячейка = `NULL`, соседние валидные строки целы (view не падает).
  - [ ] **AC #8 (массив → `LIST`):** `watch_ids='[8273645,8273646]'` → `len(watch_ids)=2`, элементы HUGEINT; пустой `watch_ids='[]'` → пустой `LIST` (**не `[NULL]`** — закрепляет преимущество варианта A над B); битый `watch_ids='garbage'` → `NULL`. _(При выборе варианта B Шефом — переписать ожидания под поэлементный разбор + гард `[]`.)_
  - [ ] **AC #4 (перезалив отражается без материализации):** записать партицию дня → `create_views` → запрос даёт N строк; **перезаписать** ту же партицию (другое содержимое/число строк, приём 2.2 `os.replace`) → **тот же** view (без пере-создания) отражает новые данные. Доказывает ленивость view.
  - [ ] **AC #7 (дрейф схемы, `union_by_name`):** две партиции visits с **разным набором колонок** (старая без нового поля, новая с ним) → запрос нового поля даёт `NULL` для строк старой партиции, реальное значение для новой; обе партиции читаются (нет ошибки позиционного матча).
  - [ ] **AC #6 (пустой источник):** (a) каталог источника без `.parquet` → `create_views` создаёт **пустой типизированный** view (`SELECT count(*)=0`, но `SELECT … LIMIT 0` даёт правильные типы колонок); (b) **visits есть, hits пуст** → `visits` отдаёт строки, `hits` — пустой типизированный view; пустой источник **не валит** построение другого (граница FR-3). Дополнительно: осиротевший `{date}.parquet.tmp` в каталоге источника **не** считается партицией (источник с одним `.tmp` → пустой view).
  - [ ] **Битый корень (наследование fail-loud):** `monkeypatch.delenv(DATA_ROOT_ENV)` + `create_views(conn)` → `ValueError` (из `get_storage_root`/`get_raw_source_dir`) — наследуется из `paths` (2.1), без побочных эффектов.
  - [ ] **Анти-зависимость (через `ast`, по реальным import-узлам — приём `test_parquet_store.py:387`):** в `scripts/utils/views.py` нет top-level import `pandas`/`polars`/`numpy`/`pyarrow`, directaiq-инфры `config_manager`/`base_script`; **дополнительно (риск №6):** нет импорта `scripts.utils.database_manager`/`scripts.utils.parquet_store`/`scripts.utils.load_state`/`scripts.utils.writer_lock`. `duckdb` **разрешён** (как 2.4 — нужен для аннотации `conn`).
  - [ ] **Live-тест НЕ нужен** (и не заводить): `views.py` в сеть не ходит — генерирует DDL и читает локальный Parquet. Правило opt-in live ([[realapi-smoke-tests]]) — только для внешнего Logs API. Зафиксировать в Dev Agent Record (как 2.1/2.2/2.3/2.4/2.5). **Отдельно отметить:** окончательное подтверждение формата TSV-массива (особенно экранирование строковых массивов) — за live-smoke оркестратора 2.7; фикстуры освежить из реального ответа.
- [ ] **Task 4 — Гейты верификации (обязательны перед закрытием)**
  - [ ] `uv run mypy scripts` → зелено (strict; `conn: duckdb.DuckDBPyConnection`; `Iterable[str]`; без `Any`-дыр).
  - [ ] `uv run pytest` → зелено (новый offline-набор + регрессия 1.x/2.1–2.5; live отсеян `addopts="-m 'not live'"`). На обеих ОС (glob/`union_by_name`/`as_posix`).
  - [ ] Новых зависимостей нет (`duckdb` уже в стеке; `logging`/`re`/`collections.abc` — stdlib) → **`uv.lock` не меняется**.
  - [ ] Прогнать чек-лист «Definition of Done» из Dev Notes.

## Dev Notes

### Рекомендуемый контракт `views.py` (вариант A; финализируй под init 4.3 / p81 2.7)

| Имя | Сигнатура | Смысл |
|---|---|---|
| `build_view_ddl` | `(source: str, catalog: Catalog, *, partition_glob: str, has_partitions: bool) -> str` | **чистая** генерация `CREATE OR REPLACE VIEW`-DDL (тестируема без БД); типизированная проекция из каталога |
| `create_views` | `(conn: duckdb.DuckDBPyConnection, *, catalog: Catalog \| None = None, sources: Iterable[str] = VALID_SOURCES) -> None` | определить наличие партиций, собрать DDL, `conn.execute`; `None` → `load_catalog()` |

**Использование (init 4.3 / p81 2.7):**
```python
with writer_lock():                          # 2.5 — запись DDL = write в gdau.duckdb
    with DatabaseManager.connection() as conn:   # 2.1 — write-conn
        ensure_load_state_table(conn)        # 2.4
        create_views(conn)                   # 2.6 — view'ы visits/hits из каталога
# MCP-чтение (3.1) потом: DatabaseManager.connection(read_only=True), без лока
```

### Форма DDL (вариант A — образец, проверено на DuckDB 1.5.3 / пин 1.5.x; риски №2/№3/№4)

**Непустой источник:**
```sql
CREATE OR REPLACE VIEW visits AS
SELECT
  TRY_CAST("visit_id"  AS HUGEINT)      AS "visit_id",
  TRY_CAST("watch_ids" AS HUGEINT[])    AS "watch_ids",   -- массив = тот же TRY_CAST (вар. A)
  TRY_CAST("date"      AS DATE)         AS "date",
  TRY_CAST("page_views" AS INTEGER)     AS "page_views"
  -- … все поля источника из каталога, в порядке каталога …
FROM read_parquet('{root}/data/raw/visits/*.parquet', union_by_name => true);
```
**Пустой источник (нет партиций) — AC #6:**
```sql
CREATE OR REPLACE VIEW hits AS
SELECT
  CAST(NULL AS HUGEINT)   AS "watch_id",
  CAST(NULL AS BIGINT[])  AS "goals_id"
  -- … типизированные NULL-колонки из каталога …
WHERE false;   -- 0 строк, корректные типы; read_parquet не зовётся
```
- `{root}/data/raw/visits` берётся `get_raw_source_dir("visits").as_posix()`; одинарные кавычки в пути → `''` (риск №2).
- `union_by_name => true` — дрейф схемы (AC #7); колонка, отсутствующая в части партиций → `NULL`.
- `TRY_CAST` строки `[v1,v2]`/`[v]`/`[]`/`NULL`/мусор в `T[]`: `[v1,v2]`/`[v]`/`[]`/`NULL`/`NULL` (эмпирически verified — риск №1).

### Протокол рабочего слоя (где 2.6 в архитектуре; architecture.md:191/206/318/430/539)

Сырьё Parquet (строки, 2.2) → **view'ы `TRY_CAST` (2.6, этот шаг)** → MCP read-only SQL (3.1) → контекст/семантика из каталога (3.3). View — **граница типизации**: сырьё дословно ниже, типизированный взгляд выше; контракт — каталог (`schema-catalog.csv`, architecture.md:517). View'ы создаёт init (4.3) на развороте и поддерживает p81 (2.7); читает только MCP (read-only, без лока — architecture.md:515).

### Паттерны от историй 1.x/2.1–2.5 (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` первой строкой; русский модульный docstring (роль + границы); идентификаторы английские, docstrings/комментарии русские.
- Type hints везде, `mypy --strict` по `scripts`, без `Any`-дыр. Абсолютные импорты от корня пакета. `logger = logging.getLogger(__name__)` напрямую — **НЕ** заводить `logging_utils.py`.
- **`conn` инъектируется** (прямой приём `load_state` 2.4: модуль БД сам не открывает, знает только свою логику; `database_manager` НЕ импортируется). `catalog`/путь — шов как `parquet_store` (`catalog=None` → `load_catalog()`).
- **Чистый билдер + тонкий исполнитель** (приём `paths`/`catalog`: чистые функции тестируются без БД/сети; побочка — отдельно). `build_view_ddl` без БД, `create_views` исполняет.
- Fail-loud наследуется из `paths` (битый корень → `ValueError` ДО DDL); `source` валидируется (`VALID_SOURCES`).
- Анти-зависимость через `ast` (import-узлы, не подстрока; приём `test_parquet_store.py:387`); `duckdb` разрешён (как 2.4).
- Live-набор осознанно отсутствует (нет внешнего API) — зафиксировать, как 2.1/2.2/2.3/2.4/2.5.

### Границы 2.6 (не выходить)

- Один модуль: `scripts/utils/views.py` (+ дополнение `docs/working-layer.md` + тесты `tests/test_views.py`). **Не** реализуем: запись Parquet (2.2 — готова), сверку (2.3 — готова), мету/реконсиляцию (2.4 — готова), `.writer.lock` (2.5), p81-оркестрацию и **scope** создания view в цикле приёма (2.7), инкремент/hot-window (2.8), MCP-чтение/`duckdb_query`/семантику колонок/`--context` (3.1/3.3), init-разворачивание (4.3).
- `views.py` не ходит в сеть, **не открывает `gdau.duckdb`** (conn инъектируется), не берёт лок, не пишет Parquet, не парсит TSV, не делает `mkdir`. Только: собрать DDL view'ов из каталога и исполнить на переданном `conn`.
- Конкуренция читатель↔писатель на Windows `os.replace` (defer 2.2) — рантайм-забота 3.1, не 2.6 (риск №9).

### Project Structure Notes

- Модуль — `scripts/utils/views.py` ровно по дереву архитектуры (architecture.md:451, `[новое]` «DDL view'ов из каталога (TRY_CAST)»). `scripts/utils/` — регулярный пакет (`__init__.py` из 1.1). Имена snake_case; type hints обязательны (mypy strict).
- Не переводить на src-layout, не переименовывать пакет `scripts` (ломает резолюцию импортов — hatchling `packages=["scripts"]`).
- `tests/` зеркалит `scripts/`: `tests/test_views.py` (имя совпадает с architecture.md:484). Конфиг pytest (`markers`/`addopts`) уже есть (1.3/1.6); `conftest.py` в проекте нет — тесты используют `tmp_path`/`monkeypatch` напрямую.
- `docs/working-layer.md` — **дополняется** (Task 2): он уже описывает фундамент (2.1) и в шапке обещает раздел типизации в 2.6. Часть DoD (project-context: компонент без актуальной спеки не «готов»).
- `gdau.duckdb`/`*.parquet`/`.writer.lock` — артефакты хранилища (под `GDAU_DATA_ROOT`), в dev-репо не создаются и не коммитятся (`.gitignore`).
- `uv.lock` не трогаем — `duckdb` уже в стеке, остальное stdlib. Новых зависимостей нет. Не реорганизовывать раскладку.

### Definition of Done — чек-лист самопроверки

1. `scripts/utils/views.py`: чистый `build_view_ddl` + исполнитель `create_views(conn, *, catalog=None, sources=VALID_SOURCES)`; `conn` инъектируется, БД сам не открывает; **НЕ** импортирует `database_manager`/`parquet_store`/`load_state`/`writer_lock` (риск №6). (AC #1)
2. Каждое поле — `TRY_CAST` по типу каталога; битая ячейка → `NULL`, view не падает; per-cell лог не эмулируется (риск №5). (AC #2)
3. ID-поля HUGEINT (из каталога — `visit_id`/`client_id`/`watch_id`); значение > 2^63 не переполняется. (AC #3, NFR-4)
4. `CREATE OR REPLACE VIEW`; view ленив → перезалив партиции виден следующим запросом без пере-создания/материализации. (AC #4)
5. Колонки — квотированные snake_case storage-имена; родные `ym:s:*`/`ym:pv:*` в DDL/SQL отсутствуют. (AC #5)
6. Пустой источник → пустой **типизированный** view (`WHERE false` + `CAST(NULL AS type)`); один пустой источник не валит другой; `.parquet.tmp` не считается партицией. (AC #6)
7. Непустой источник — `read_parquet('{glob}', union_by_name => true)`; дрейф схемы → отсутствующая колонка `NULL`; путь — `as_posix()` + экранирование кавычек (риск №2/№4). (AC #7)
8. Массивы типизированы в `LIST` (**вариант A — утверждён Шефом**: нативный `TRY_CAST(col AS T[])`, исход AC #8; пустой `[]` → `[]`, не `[None]`; скаляр=массив одно выражение). (AC #8)
9. `docs/working-layer.md` дополнен разделом «типизированные представления» (3 вопроса простыми словами; границы 2.2/2.4/2.7/3.1 названы; «представления — только чтение, замок не берут») — DoD компонента. (Task 2)
10. Offline-тесты покрывают AC #1–#8 (чистый DDL без БД + интеграция на tmp-партициях) + битый корень + анти-зависимость по `ast` (нет pandas/polars/pyarrow/numpy; нет database_manager/parquet_store/load_state/writer_lock в модуле; duckdb разрешён). Live осознанно отсутствует; формат TSV-массива подтверждается live-smoke 2.7. (Task 3)
11. `uv run mypy scripts` и `uv run pytest` — зелёные на обеих ОС; `uv.lock` не менялся; `data/`-артефактов (`*.parquet`/`*.duckdb`/`.writer.lock`) в dev-репо не создано.
12. Велась в отдельной ветке `story/2.6-views` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС (ubuntu + windows). PR в `main`.

### Latest Tech Information

- **DuckDB VARCHAR→LIST cast (проверено эмпирически, duckdb 1.5.3 / пин `1.5,<1.6`):** `TRY_CAST('[8273645,8273646]' AS HUGEINT[])` → `[8273645, 8273646]`; `'[v]'` → `[v]`; `'[]'` → `[]` (корректный пустой список); `'[1, 2, 3]'` (с пробелами) → `[1,2,3]`; `NULL`/`'garbage'` → `NULL`. То есть нативный cast покрывает наблюдаемый формат массива и **проще + корректнее** поэлементного разбора (вариант A, риск №1).
- **Поэлементный разбор (вариант B) — известный дефект на пустом массиве:** `list_transform(string_split(trim('[]','[]'), ','), x -> TRY_CAST(x AS HUGEINT))` → `[None]` (а не `[]`), т.к. `trim('[]','[]')=''`, `string_split('',',')=['']`. Требует доп-гарда `CASE WHEN col IN (NULL,'[]') THEN … END`. Ещё один довод за A.
- **`read_parquet(glob, union_by_name => true)`:** объединяет партиции по именам колонок (не по позиции) — отсутствующая в части файлов колонка → `NULL` (AC #7). Glob по `*.parquet`; `.parquet.tmp` не матчится. Путь — `as_posix()` (прямые слеши кросс-платформенно; риск №2). Пустой glob (нет файлов) → ошибка при запросе → пустой источник строй без `read_parquet` (риск №3).
- **Пустой типизированный view без `FROM` (проверено):** `SELECT CAST(NULL AS HUGEINT) AS visit_id, CAST(NULL AS HUGEINT[]) AS watch_ids WHERE false` → 0 строк с корректными типами колонок (AC #6).
- **`TRY_CAST` (битая → NULL) — set-based:** даёт NULL на неприводимом значении без падения; per-cell лог в view невозможен (риск №5) — «+лог» FR-7 во view интерпретируется как NULL, опц. агрегатная диагностика — усиление.
- **Web-ресёрч не требуется:** DuckDB стабилен и зафиксирован локом, поведение проверено локально; внешнего сетевого контракта в истории нет (live-smoke неприменим в 2.6 — как 2.1/2.2/2.3/2.4/2.5). Реальный wire-формат массивов Метрики (особенно экранирование строковых) подтверждается live-smoke 2.7; фикстуры освежить из реального ответа ([[realapi-smoke-tests]]).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.6] (строки 307–322) — user story + 8 AC (включая edge-cases: пустой источник, дрейф схемы, парсинг TSV-массива).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-7] (строка 31) — рабочий слой DuckDB поверх Parquet, `TRY_CAST` (битая → NULL + лог), ID → HUGEINT.
- [Source: _bmad-output/planning-artifacts/epics.md#FR-3] (строка 25) — оба источника visits+hits, независимо; связь `visits.watchIDs ↔ hits.watchID`.
- [Source: _bmad-output/planning-artifacts/epics.md#FR-6] (строка 30) — сырьё строками без CAST; смена списка полей не мигрирует старые партиции (корень дрейфа схемы, AC #7).
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2] (строки 230–232) — место 2.6 в упорядоченной цепочке 2.1→2.9.
- [Source: _bmad-output/planning-artifacts/epics.md#Story 3.3 AC] (строка 430) — пустые view → `row_count=0`/`date_range=null` (потребитель пустого-типизированного view, AC #6).
- [Source: _bmad-output/planning-artifacts/epics.md#Story 4.3 AC] (строки 477, 490) — init разворачивает view'ы из каталога, толерантно к нулю партиций (потребитель `create_views`).
- [Source: _bmad-output/planning-artifacts/architecture.md#Data Architecture] (строки 191, 206–209) — OQ#3: рабочий слой = view'ы поверх Parquet с `TRY_CAST`; ID → HUGEINT; каталог = SSOT для DDL view.
- [Source: _bmad-output/planning-artifacts/architecture.md#Format Patterns] (строки 339–369) — маппинг ClickHouse→DuckDB; массивы Array(T)→`LIST` (в TSV строкой, парсятся во view); `TRY_CAST` по `working_type`, битая → NULL + лог.
- [Source: _bmad-output/planning-artifacts/architecture.md#Naming Patterns] (строки 306–323) — storage-имена snake_case; объекты DuckDB: view `visits`/`hits` (AC #5).
- [Source: _bmad-output/planning-artifacts/architecture.md#Scalability] (строки 262–275) — почему view (не материализация): partition pruning, view→таблицы обратимо (AC #4, OQ#3).
- [Source: _bmad-output/planning-artifacts/architecture.md#Boundaries] (строки 510–518) — запись (p81) под локом/write-conn; **чтение (MCP) — read-only, без лока**; контракт данных = каталог.
- [Source: _bmad-output/planning-artifacts/architecture.md#Directory Structure] (строки 451, 484, 526) — `views.py` `[новое]` «DDL view'ов из каталога (TRY_CAST)»; `test_views.py`; FR-7 → `utils/views.py`.
- [Source: _bmad-output/project-context.md#Data & Domain] (строки 101–103) — рабочий слой = view'ы с `TRY_CAST`; битая → NULL + лог (день не падает); CAST в сырье запрещён; массивы парсятся в `LIST` во view, не в сырье; объекты `visits`/`hits`.
- [Source: scripts/utils/catalog.py:174–176] — `Catalog.duckdb_types(source) -> dict[storage_name → duckdb_type]` (готов «для views.py 2.6»). Источник типов/имён DDL.
- [Source: scripts/utils/catalog.py:54–65, 100–117] — маппинг ClickHouse→DuckDB + `duckdb_type_for` (Array(T)→`T[]`); типы каталога уже сидированы (визиты HUGEINT/HUGEINT[], массивы `T[]`).
- [Source: scripts/utils/paths.py:112–118] — `get_raw_source_dir(source) -> {root}/data/raw/{source}` (готов «для views.py 2.6»; чистый fail-loud резолвер, без mkdir). Источник glob партиций.
- [Source: scripts/utils/database_manager.py:39–60] — `DatabaseManager.connection(read_only)` — write-conn для init/p81, read-only для MCP; `conn` инъектируется в `create_views` (views.py НЕ импортирует database_manager, риск №6).
- [Source: scripts/utils/parquet_store.py:81–97, 137] — партиции хранят колонки в **snake_case VARCHAR** (переименование по каталогу) — вход для `read_parquet` view'а (AC #5: view читает snake_case, не `ym:s:*`).
- [Source: scripts/utils/load_state.py:107–113, 192–215, 262–272] — приёмы: `conn` инъектируется/БД не открывается; `_partition_dates` (`get_raw_source_dir`+`is_dir`+`glob('*.parquet')`, `.tmp` исключён) — образец проверки наличия партиций (риск №3); `duckdb` разрешён в ast-анти-зависимости.
- [Source: tests/fixtures/logs_visits_sample.tsv:1–3] — реальный формат массива в TSV: `[8273645,8273646]` (опровергает предпосылку AC #8 «не литерал DuckDB-list»; риск №1).
- [Source: tests/test_parquet_store.py:387–409] — паттерн ast-анти-зависимости (import-узлы + запрещённые корни имён); зеркало для `test_views.py`.
- [Source: docs/working-layer.md:8–11, 62–75] — спека сама обещает раздел типизации в 2.6 («этот файл тогда дополнится разделом про типизацию»); граница 2.6 (Task 2).
- [Source: _bmad-output/implementation-artifacts/deferred-work.md:31] — defer 2.2: Windows `os.replace` поверх партиции, открытой **читателем** (view 2.6/MCP 3.1), → `PermissionError`; рантайм-конкуренция — забота 3.1 (риск №9).
- [Source: _bmad-output/implementation-artifacts/deferred-work.md:35] — defer 2.3: контракт строка↔запись и неподтверждённое экранирование TSV — реальный формат массивов подтверждает live-smoke 2.7 (примечание к AC #8).
- [Source: _bmad-output/implementation-artifacts/2-2-атомарная-запись-parquet-партиции-дня.md] — `write_partition` пишет snake_case VARCHAR строками as-is; типизация — «забота view (2.6)» (граница 2.2↔2.6).
- [Memory: simplicity-first] — вариант A (нативный `TRY_CAST`) проще и корректнее поэлементного разбора; единообразный билдер без спец-кейсов на массивы. [[structure-mirror-directaiq]] — `views.py` в `utils/`, форма узнаваема, CSV→CAST-в-таблице directaiq заменён нашей Parquet-view-моделью. [[field-scope-decisions]] — список полей = весь `schema-catalog.csv` (74 visits + 41 hits), view строится из него целиком. [[review-mode-edge-case-hunter]] — Шеф гоняет edge-case hunter: риски №1 (формат массива A/B, эмпирически проверен), №2 (путь в DDL/posix), №3 (пустой источник), №4 (дрейф/union_by_name), №9 (читатель↔писатель) разобраны проактивно. [[realapi-smoke-tests]] — live применим только к внешнему API → в 2.6 не нужен; формат массивов подтверждает 2.7.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-05-24 — Story 2.6 создана (create-story): рабочий слой — типизированные view `visits`/`hits` из каталога с `TRY_CAST` (FR-7, FR-3). Выделенный модуль `scripts/utils/views.py` (`[новое]`, не вендоринг; зависит от `catalog` 1.5 + `paths.get_raw_source_dir` 2.1 + stdlib; `conn` инъектируется, БД сам не открывает; НЕ импортирует `database_manager`/`parquet_store`/`load_state`/`writer_lock` — риск №6). Чистый билдер `build_view_ddl` (тестируем без БД) + исполнитель `create_views(conn)`. **РЕКОМЕНДОВАН вариант A** (нативный `TRY_CAST(col AS T[])` для массивов): эмпирически (DuckDB 1.5.3) корректно парсит наблюдаемый формат `[v1,v2]`/`[v]`/`[]`→`[]`, единообразный билдер скаляр=массив; вариант B (явный `string_split`+`list_transform`, буква AC #8) хуже — на пустом `[]` даёт `[None]`, рвёт строковые массивы с запятой, сложнее. **AC #8 ТРЕБУЕТ РЕШЕНИЯ ШЕФА** (предпосылка «не литерал DuckDB-list» опровергнута фикстурой). Разобраны риски: путь партиций в DDL строковым литералом → экранирование `''` + `as_posix()` (№2); пустой источник → пустой типизированный view `WHERE false`+`CAST(NULL AS type)` (№3, AC #6); дрейф схемы → `read_parquet(union_by_name=>true)` (№4, AC #7); `TRY_CAST` даёт NULL без per-cell лога (№5, нюанс AC #2); `CREATE OR REPLACE` + ленивый view → перезалив виден без материализации (№7, AC #4); snake_case-имена, `ym:s:*` не во view (№8, AC #5); читатель↔писатель на Windows `os.replace` — забота 3.1, не 2.6 (№9, defer 2.2). Дополнение `docs/working-layer.md` (раздел «типизированные представления»); offline-набор `tests/test_views.py` (AC #1–#8 + битый корень + ast-анти-зависимость, duckdb разрешён); live неприменим (формат массивов подтверждает live-smoke 2.7). Статус → ready-for-dev.
- 2026-05-24 — **Решение Шефа по механизму массивов: вариант A (нативный `TRY_CAST(col AS T[])`) — УТВЕРЖДЁН.** Вариант B (явный `string_split`+`list_transform`, буква AC #8) отклонён: эмпирически (DuckDB 1.5.3) хуже — на пустом `[]` даёт `[None]` вместо `[]`, рвёт строковые массивы с запятой/скобкой внутри, тащит спец-кейс на `[]`-типы. AC #8 закрывается **по исходу** (массив → `LIST`, битая ячейка → NULL, как у скаляров), а не по букве. Зафиксировано в «Главные риски / решения» (риск №1), примечании к AC #8 и DoD #8; контракт/тесты/границы от выбора не зависят (одно выражение типизации) — не менялись.
