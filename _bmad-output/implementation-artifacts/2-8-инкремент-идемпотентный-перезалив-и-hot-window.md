# Story 2.8: Инкремент, идемпотентный перезалив и hot-window

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want чтобы обновление за диапазон грузило только отсутствующие дни и всегда перезаливало hot-window последних N дней,
so that повтор был дёшев и идемпотентен, а доезжающие за последние дни данные подтягивались без ручного перезалива.

**Контекст эпика.** Восьмая история Epic 2 «Приём данных и безопасное обновление хранилища» — **слой решения «какие дни грузить»** поверх готового цикла одного дня. 2.7 дала ядро `load_day(conn, client, source, date)` (инъекция, протокол идемпотентного дня) и run-level `ingest_day` (лок один раз, один день). 2.8 добавляет **диапазонный** run-level в тот же модуль: берёт `.writer.lock`/`conn`/клиент **один раз** на весь прогон, реконсилирует журнал × факт, вычисляет набор дней (skip загруженных + перезалив hot-window) и зовёт `load_day` **напрямую** по каждому дню. 2.9 сверху ставит CLI-команду `gdau-logs update` (поверхность, exit-коды, агрегация источников). Покрывает **FR-9** (инкрементальная догрузка — только отсутствующие дни по мета + факту, не `SELECT DISTINCT date`), **FR-10** (идемпотентный перезалив одного дня — перезапись одной партиции, без `DROP`) и **FR-11** (hot-window перезалива последних N дней, N конфигурируем, по умолчанию 3). Место в цепочке: 2.7 = **один день**; **2.8 = диапазон + какие дни**; 2.9 = команда. После 2.8 «обновить данные игры за период» — это один вызов диапазонной функции, идемпотентный и не ломающий базу.

**Почему 2.8 — это «решение о днях», а не новый путь записи (корень требования).** Вся запись (атомарность temp→rename, сверка, чекпойнт, лок) уже живёт в примитивах и в `load_day` (2.2–2.7). 2.8 **не пишет** Parquet и **не** реализует перезалив заново: «идемпотентный перезалив дня» (FR-10) — это просто **повторный вызов `load_day` для того же дня** (он перезаписывает один файл через `write_partition`, без `DROP` — анти-паттерн directaiq `_force_drop_tables`). Единственная **новая** логика 2.8 — **чистое решение «какой набор дней лить»**: пройти диапазон, пропустить подтверждённо-загруженные (по `reconcile`), но **всегда** включить дни hot-window. Это решение выносится в **чистую тестируемую функцию** `_select_days_to_load` (без `conn`/сети/часов — главный шов юнит-тестов, как `build_view_ddl` в 2.6), а run-level `ingest_range` только обвязывает её локом/соединением/клиентом и крутит `load_day`.

**Это прямая реализация решения Шефа по 2.7 (вариант A), без новой развилки.** 2.7 зафиксировала: `writer_lock` **не реентерабелен** → диапазон дней берёт лок **один раз** вокруг всего прогона и зовёт `load_day` напрямую, **НЕ** `ingest_day` в цикле (`writer_lock.py:82`, p81 docstring `ingest_day`, architecture.md:384). 2.8 — это и есть тот «диапазон-вход». Никакого повторного A/B-выбора: контракт `load_day` готов, 2.8 просто строит над ним диапазонный держатель лока. Архитектура прямо помещает hot-window (N=3) в `p81_load_logs.py` (architecture.md:458) и называет отдельный тест `test_hot_window.py` (architecture.md:484); FR-9/10/11 → `p81_load_logs.py` (architecture.md:528–529).

**2.8 — offline, без живого API.** В отличие от 2.7 (первый end-to-end live-smoke), 2.8 не вводит нового сетевого контракта: реальный цикл дня уже подтверждён live-smoke 2.7. Логика «какие дни» детерминирована и тестируется **без сети** — поддельный `load_day` (или мок `MetricaClient` + in-memory `conn`) + инъекция якоря/`loaded`. **Live-smoke для 2.8 не обязателен** (нет нового API-контракта); регрессия live 2.7 остаётся в силе.

> ⚠️ **ЗАВИСИМОСТЬ:** 2.8 строится прямо на `load_day`/`ingest_day` (2.7) и `load_state.reconcile` (2.4). На момент создания истории **2.7 в статусе `in-progress`** (модуль `scripts/8x_metrica_logs_api/p81_load_logs.py` реализован — сигнатуры ниже сверены с фактическим кодом, не только со спекой). Перед стартом 2.8 убедиться, что 2.7 доведена до `done` (зелёные гейты), иначе база `load_day` может ещё измениться. Контракт `load_day(conn, client, source, date, *, catalog, poll_interval_s, poll_timeout_s, max_consecutive_errors, sleep) -> int` — финальный (вариант A утверждён).

### Главные риски / решения (читать до кода)

1. **Где живёт и scope лока: диапазонный `ingest_range` в `p81_load_logs.py`, лок ОДИН раз, зовёт `load_day` напрямую (НЕ `ingest_day` в цикле).** `writer_lock` **не реентерабелен** (`writer_lock.py:82`): повторный захват того же пути тем же процессом → `WriterLockHeldError` сам-с-собой. Поэтому `ingest_range` берёт `with writer_lock():` **один раз**, внутри `with DatabaseManager.connection() as conn:` строит клиент один раз, `ensure_load_state_table(conn)` + `create_views(conn)` один раз, и в цикле зовёт **`load_day(conn, client, source, date, …)`** напрямую (оно лок НЕ берёт — это его контракт по варианту A). **НЕ** звать `ingest_day` в цикле (он сам берёт лок → реентрантность). Архитектура помещает hot-window в `p81_load_logs.py` (architecture.md:458, 528–529) → новый код 2.8 — в этом же модуле, рядом с `load_day`/`ingest_day`.

2. **Чистое ядро решения «какие дни» = тестируемый шов `_select_days_to_load` (без `conn`/сети/часов).** Вынести решение в **чистую** функцию `_select_days_to_load(source, loaded, date1, date2, *, hot_window_days, anchor) -> list[str]`: на вход — `loaded: frozenset[tuple[str, str]]` (от `reconcile`), границы диапазона (`date`-объекты), `hot_window_days`, `anchor: date` (якорь окна, инъектируется). Возврат — упорядоченный (по возрастанию даты) список `YYYY-MM-DD` к загрузке. Логика: для каждого дня `d ∈ [date1, date2]` грузить, если `d` в hot-window **ИЛИ** `(source, format_date(d)) not in loaded`. Это **главный юнит-тест 2.8** (приём 2.6 `build_view_ddl`: чистая функция тестируется без БД/сети — детерминированно инъекцией `anchor`/`loaded`). `ingest_range` — тонкая обвязка над ней.

3. **Инкремент опирается на `reconcile`, НЕ на `SELECT DISTINCT date` (FR-9 дословно).** «Загружен» = подтверждено `reconcile(conn, sources=[source])` (2.4): факт партиции + `status='loaded'` + `row_count == факт` (три условия). 2.8 **не** считает день загруженным по одному наличию файла или строки журнала. `reconcile` зови **один раз** в начале прогона (внутри лока — он **мутирует** `load_state`: `DELETE` ложной меты, `load_state.py:234`), получи `frozenset` и передай в `_select_days_to_load`. Не дублировать логику реконсиляции в 2.8 — звать готовое (2.4).

4. **Hot-window: якорь = «вчера по МСК», окно клипуется к диапазону, НЕ якорится на `date2` (AC #5).** Якорь `anchor = moscow_yesterday()` (`dates.py:58`; 2.7 уже импортирует), окно = последние `N` дней, заканчивающихся на `anchor`: `[anchor - (N-1), anchor]`. Дни окна, **попавшие в запрошенный диапазон `[date1, date2]`**, грузятся **всегда** (даже если `loaded`). Клиппинг к диапазону — естественный (итерируем только `[date1, date2]`). **Не** якорить окно на `date2`: для исторического диапазона (`date2 < anchor`) окно с диапазоном не пересекается → чистый инкремент (доезжать нечему — hot-window про «свежие» данные). `N=0` → окно пустое (чистый инкремент); `N<0` → `ValueError` (понятная ошибка). **Один замер часов:** прочитай `today = moscow_today()` один раз, передай `today_msk=today` в `clamp_date_range` (его потолок = `today - 1`), и `anchor = today - timedelta(days=1)` — потолок clamp и якорь окна гарантированно консистентны (нет TOCTOU на полночи).

5. **Hot-window побеждает skip (AC #4).** День, попавший и под «skip загруженных», и под hot-window → **перезаливается** (hot-window выигрывает). В `_select_days_to_load` это `in_hot or key not in loaded` (дизъюнкция — hot-window игнорирует `loaded`). Перезалив идемпотентен (FR-10): `load_day` перезапишет один файл через `write_partition` (2.2), без `DROP`.

6. **На сбой дня внутри диапазона → пробросить (остановить прогон), НЕ «продолжить и собрать что вышло».** Если `load_day` бросает (например `RowCountMismatchError` 2.3 — жёсткий fail целостности, или терминальный статус API, или исчерпание poll) — исключение **пробрасывается наружу** из `ingest_range` (после release лока в `finally` контекст-менеджера). Уже **закоммиченные** дни (каждый `load_day` коммитит независимо через `mark_loaded`) **остаются** загруженными; повторный прогон до-грузит хвост через инкремент (skip уже-загруженных). Это и есть resumability: per-day-коммит + incremental-skip. **НЕ** глушить сбой дня ради продолжения (маскировка потери — анти-паттерн; «не сломать базу» = громкая остановка). **Агрегация смешанного результата visits/hits и exit-коды — забота 2.9** (она ловит исключение `ingest_range` на источник и решает итоговый код); исчерпание дневной квоты Logs API с resumable-сообщением — тоже **2.9**.

7. **`ingest_range` — на один источник; 2.9 крутит источники и агрегирует.** Сигнатура `ingest_range(source, date1, date2, …)` (один `source ∈ VALID_SOURCES`), как `load_day(source, …)`/`ingest_day(source, …)` — чистая композиция. `reconcile(conn, sources=[source])` — только этот источник (не сканировать второй зря). Прогон обоих visits+hits под **одним** локом и агрегацию (частичный сбой → non-zero) делает 2.9 (epics.md:367). Если 2.9 захочет оба источника под одним локом — она вынесет лок/conn наружу и позовёт диапазонную логику; зафиксируй в docstring, что лок-scope — зацикливающий вход (как `ingest_day`/`load_day` в 2.7), чтобы 2.9 могла переиспользовать без реентрантности.

8. **Clamp диапазона ДО лока (fail-fast).** `clamp_date_range(parse_date(date1), parse_date(date2), today_msk=today)` (`dates.py:90`) клампит `date2` на «вчера по МСК» с INFO-логом **и** валидирует диапазон: будущий `date1`/инвертированный диапазон (`date1 > date2` после clamp) → `ValueError` **до** возврата. Сделать это **до** `with writer_lock():` — не брать лок и не строить клиент для заведомо пустого/инвертированного диапазона. `parse_date` строгий `YYYY-MM-DD` (`dates.py:63`) — мусорная дата → понятная ошибка до лока.

9. **`load_day` уже валидирует `date <= вчера` — двойная страховка не мешает.** Поскольку `date2` клампится на «вчера по МСК», все дни набора `<= anchor = вчера`. `load_day` всё равно сам валидирует `parse_date(date) <= moscow_yesterday()` (риск №10 в 2.7) — это нормально, набор 2.8 этого правила не нарушает (clamp гарантирует). Не отключать и не дублировать валидацию.

10. **2.8 НЕ трогает путь записи и примитивы.** Не реализовывать заново: запись Parquet (2.2), сверку (2.3), мету/`reconcile` (2.4), лок (2.5), view (2.6), цикл дня `load_day`/poll/download/parse (2.7), клиент/даты/каталог/креды (1.x). 2.8 **зовёт готовое**. Не тащить инфру directaiq (`BaseScript`/`config_manager`/параллельную очередь/`DROP TABLE`/`view_builders`). Новых зависимостей нет (`datetime`/`collections.abc`/`typing` — stdlib) → `uv.lock` не меняется.

## Acceptance Criteria

1. **Given** диапазон, **When** запускается обновление, **Then** грузятся только отсутствующие дни (по `load_state` + факту через `reconcile`, FR-9), загруженные пропускаются.
2. **Given** отдельный день, **When** он переливается, **Then** перезапись одной партиции (2.2), без `DROP`; повтор идемпотентен (FR-10).
3. **Given** hot-window N (конфиг, по умолчанию 3), **When** обновление, **Then** последние N дней перезаливаются всегда (FR-11), опираясь на per-day идемпотентность.
4. **Given** день попадает и под «skip загруженных», и под hot-window, **When** разрешается приоритет, **Then** hot-window побеждает — день перезаливается, даже если помечен загруженным. _[edge-case: приоритет hot-window vs инкремент]_
5. **Given** якорь hot-window, **When** он вычисляется, **Then** это «вчера по МСК» (clamp, 1.4), окно клипуется к загруженному диапазону; `N=0` отключает окно, `N<0` → понятная ошибка. _[edge-case: якорь/границы/невалидный N]_

> **Примечание к AC #1/#3 (источник истины «загружено»).** «Загружен» = подтверждено `reconcile` (факт партиции + `status='loaded'` + `row_count==факт`), а НЕ `SELECT DISTINCT date` и не одно наличие файла (риск №3, FR-9 дословно). Hot-window игнорирует это подтверждение (всегда перезаливает — AC #4).

> **Примечание к AC #2 (идемпотентность = перезапись одного файла).** Перезалив дня = повторный `load_day` → `write_partition` одного `{date}.parquet` (temp→`os.replace`, 2.2). Никакого `DROP TABLE`/удаления партиции (анти-паттерн directaiq `_force_drop_tables`). 2.8 нового пути записи не вводит.

> **Примечание к scope/границам.** Поверхность `gdau-logs update` (argparse, exit-коды, агрегация visits+hits, resumable-сообщение про квоту) — **2.9** (epics.md:354–369). 2.8 даёт диапазонную **функцию** (`ingest_range` на один источник) + чистое решение «какие дни» (`_select_days_to_load`). Сбой дня → проброс (resumable через per-day-коммит + incremental-skip, риск №6).

## Tasks / Subtasks

- [ ] **Task 1 — Чистое решение «какие дни лить» в `scripts/8x_metrica_logs_api/p81_load_logs.py` (AC: #1, #3, #4, #5)**
  - [ ] Добавить модульную константу `DEFAULT_HOT_WINDOW_DAYS = 3` (FR-11; рядом с poll-константами; комментарий «почему 3» — доезжающие данные, architecture.md:39–41/197). Добавить в `__all__`: `DEFAULT_HOT_WINDOW_DAYS`, `ingest_range`, `IngestRangeResult` (+ существующие).
  - [ ] **`_select_days_to_load(source: str, loaded: frozenset[tuple[str, str]], date1: date, date2: date, *, hot_window_days: int, anchor: date) -> list[str]`** — **ЧИСТАЯ** функция (без `conn`/сети/часов; главный тестируемый шов — риск №2). Логика: `if hot_window_days < 0: raise ValueError(...)` (AC #5, понятное сообщение); `hot_start = anchor - timedelta(days=hot_window_days - 1)` при `hot_window_days > 0` (иначе окно пустое — `N=0`); для каждого `d` в `[date1, date2]` (по возрастанию, helper `_iter_dates`): `in_hot = hot_window_days > 0 and hot_start <= d <= anchor`; `key = (source, format_date(d))`; включить `format_date(d)`, если `in_hot or key not in loaded` (hot-window побеждает skip — AC #4). Возврат — список `YYYY-MM-DD` по возрастанию. Русский docstring: якорь = вчера по МСК, окно клипуется к диапазону итерацией (риск №4), `N=0` off / `N<0` fail (AC #5), hot-window ⊃ skip (AC #4).
  - [ ] **`_iter_dates(date1: date, date2: date) -> Iterator[date]`** — генератор по дням `[date1, date2]` включительно (`cur += timedelta(days=1)`). Маленький helper; пустой при `date1 > date2` (но clamp в `ingest_range` уже не пускает инвертированный диапазон — риск №8).
- [ ] **Task 2 — Run-level диапазона `ingest_range` (AC: #1–#5; вариант A — лок один раз)**
  - [ ] **`ingest_range(source: str, date1: str, date2: str, *, hot_window_days: int = DEFAULT_HOT_WINDOW_DAYS, catalog: Catalog | None = None, poll_interval_s: float = POLL_INTERVAL_S, poll_timeout_s: float = POLL_TIMEOUT_S, max_consecutive_errors: int = MAX_CONSECUTIVE_POLL_ERRORS, sleep: Callable[[float], None] = time.sleep) -> IngestRangeResult`**. Порядок СТРОГО:
    - [ ] **До лока (fail-fast — риск №8):** `_require_valid_source(source)`; `if hot_window_days < 0: raise ValueError(...)` (AC #5, до лока); `today = moscow_today()` (один замер — риск №4); `d1, d2 = clamp_date_range(parse_date(date1), parse_date(date2), today_msk=today)` (клампит `date2`→вчера + валидирует инверсию); `anchor = today - timedelta(days=1)` (== потолок clamp).
    - [ ] **Под локом ОДИН раз (AC #3 запись под локом):** `with writer_lock():` `with DatabaseManager.connection() as conn:` `creds = read_metrica_credentials(); client = MetricaClient(token=creds.token, counter_id=creds.counter_id)`; `ensure_load_state_table(conn)` + `create_views(conn)` (один раз); `loaded = reconcile(conn, sources=[source])` (один раз, мутирует `load_state` — потому под локом; риск №3); `days = _select_days_to_load(source, loaded, d1, d2, hot_window_days=hot_window_days, anchor=anchor)`; затем **цикл** `for day in days: rows = load_day(conn, client, source, day, catalog=catalog, poll_interval_s=…, poll_timeout_s=…, max_consecutive_errors=…, sleep=sleep)` (НЕ `ingest_day` — реентрантность лока, риск №1) с аккумуляцией итога; сбой `load_day` **пробрасывается** (НЕ глушить — риск №6).
    - [ ] Вернуть `IngestRangeResult(source=source, loaded_dates=[…], skipped_dates=[…], total_rows=…)`. `skipped_dates` = дни диапазона, которых нет в `days` (для отчёта 2.9). Лог INFO: диапазон, сколько грузим / сколько пропущено / hot-window N.
  - [ ] **`IngestRangeResult`** — `NamedTuple(source: str, loaded_dates: list[str], skipped_dates: list[str], total_rows: int)` (итог для 2.9: что перезалито, что пропущено, сумма строк). Русский docstring. `from typing import NamedTuple`.
  - [ ] Русский docstring `ingest_range`: run-level диапазона (лок+conn+клиент **один раз** — вариант A), инкремент по `reconcile` (FR-9), hot-window N (FR-11), idemпотентный перезалив = повторный `load_day` (FR-10), сбой дня → проброс (resumable через per-day-коммит). **Явно:** «2.9 берёт лок один раз вокруг прогона; для обоих источников под одним локом 2.9 выносит лок/conn наружу и зовёт логику — лок-scope зацикливающий вход, `writer_lock` не реентерабелен (риск №1/№7)»; «поверхность `update`/exit-коды/агрегация источников/квота — 2.9».
  - [ ] **Новые импорты** в `p81_load_logs.py`: `from datetime import date, timedelta`; `from collections.abc import Callable, Iterator` (`Callable` уже есть — добавить `Iterator`); `from typing import Any, NamedTuple` (`Any` уже — добавить `NamedTuple`); расширить `from scripts.utils.dates import format_date, moscow_today, moscow_yesterday, parse_date, clamp_date_range` (добавить `clamp_date_range`, `moscow_today`; `moscow_yesterday` оставить — используется `load_day`); расширить `from scripts.utils.load_state import …, reconcile` (добавить `reconcile`).
  - [ ] **НЕ делать:** звать `ingest_day` в цикле (реентрантность лока — риск №1); брать `.writer.lock` внутри per-day-цикла (лок один раз на `ingest_range` — риск №1); считать «загружено» по `SELECT DISTINCT date`/наличию файла вместо `reconcile` (риск №3, FR-9); якорить hot-window на `date2` вместо «вчера по МСК» (риск №4); глушить сбой дня ради продолжения (риск №6 — проброс); `DROP TABLE`/удаление партиции ради перезалива (перезалив = `load_day`→`write_partition`, риск №5/AC #2); реализовывать заново запись/сверку/мету/лок/view/poll/download/parse (зовём 2.2–2.7); двойной замер часов для clamp и якоря (один `moscow_today()` — риск №4); тащить `BaseScript`/`config_manager`/параллель directaiq (NFR-6); агрегацию visits+hits/exit-коды/квоту (это 2.9 — риск №6/№7); `import scripts.8x_metrica_logs_api…` как statement (digit-префикс — `importlib`, как 2.7).
- [ ] **Task 3 — Дополнить спеку `docs/ingestion.md` (часть DoD)**
  - [ ] Добавить раздел **«Какие дни грузить: инкремент и hot-window»** человеческим языком, без жаргона/сигнатур. Три вопроса: **(1) Что делает** — за запрошенный период решает, какие дни **реально** грузить: уже загруженные (по честному учёту — журнал, сверенный с файлами) **пропускает**, отсутствующие — грузит, а **последние несколько дней (по умолчанию три) перезаливает всегда**, даже если они уже были загружены; каждый день проводит через обычный цикл приёма (история 2.7), все дни — под **одним замком** на весь прогон. **(2) Зачем** — повтор обновления должен быть **дёшев и безопасен**: не перекачивать уже лежащее (экономия квоты Метрики), но и не пропустить **доезжающие** данные за свежие дни (Метрика дособирает статистику задним числом несколько суток) — отсюда «свежее окно» поверх инкремента; повтор того же периода **ничего не ломает** (каждый день — это перезапись одного файла, идемпотентно). **(3) Контракт** — на вход период (две даты) и источник; конец периода не дальше «вчера по МСК» (дальше Метрика не отдаёт — тихо подрезается с записью в лог, инвертированный период — понятная остановка); размер свежего окна настраивается (ноль — окно выключено, отрицательное — понятная ошибка); якорь окна — «вчера по МСК», окно прижато к запрошенному периоду; если какой-то день не загрузился — прогон **останавливается** (уже загруженные дни остаются на месте, повтор до-грузит остаток — пропущенные уже-загруженные дни не трогаются). **Явно границы:** один день целиком — оркестратор приёма (2.7); запись/сверка/журнал/замок/типы — 2.2–2.6; **команда `gdau-logs update` одной строкой, коды возврата и прогон обоих источников сразу — следующая история (2.9)**; разговор с Метрикой по сети — `metrica-client.md`. Обновить «Границы»: пункт про «решение какие дни / hot-window — 2.8» из «обещано» перевести в «реализовано здесь», передав «командную поверхность» в 2.9. Не дублировать соседние разделы.
- [ ] **Task 4 — Offline-тесты `tests/test_hot_window.py` (AC: #1–#5, без сети) — имя по architecture.md:484**
  - [ ] `from __future__ import annotations`; **импорт p81 через `importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")`** (digit-префикс; образец из 2.7 `test_p81_orchestrator.py`). Без сети/реальных пауз. Кросс-платформенно (`tmp_path`/`pathlib`; CI ubuntu+windows).
  - [ ] **Чистое ядро `_select_days_to_load` (главный шов — риск №2; детерминизм инъекцией `anchor`/`loaded`):**
    - [ ] **AC #1 (инкремент):** диапазон, часть дней в `loaded` (вне hot-window) → пропущены; отсутствующие → в наборе. Возврат по возрастанию даты.
    - [ ] **AC #3 (hot-window всегда):** последние `N` дней (по умолчанию 3) в наборе **даже если** в `loaded`; дни до окна — по инкременту.
    - [ ] **AC #4 (hot-window > skip):** день и в `loaded`, и в окне → **в наборе** (перезалив). Дизъюнкция доказана.
    - [ ] **AC #5 (границы N):** `N=0` → окно пусто (чистый инкремент, загруженные вне набора); `N<0` → `ValueError`; `N` больше длины диапазона → клиппинг (только дни диапазона, без выхода за `date1`).
    - [ ] **Якорь/клиппинг (риск №4):** исторический диапазон (`date2 < anchor`) → окно не пересекается → чистый инкремент; `anchor` в наборе всегда при `N>=1` и `anchor ∈ [date1,date2]`.
  - [ ] **Run-level `ingest_range` (интеграция; поддельный шов):** замокать `load_day` (через `monkeypatch.setattr(p81, "load_day", fake)`) → фиксировать порядок/набор вызванных дней == `_select_days_to_load`; `reconcile`/`create_views`/`DatabaseManager.connection`/`read_metrica_credentials`/`MetricaClient` замокать для изоляции (как `ingest_day`-тест в 2.7). Проверить:
    - [ ] **Лок один раз (риск №1, AC #3):** `writer_lock` взят **один раз** на весь прогон (шов фиксирует один вход), `load_day` зван по каждому дню без отдельного лока; `ingest_day` **не** зван (анти-реентрантность).
    - [ ] **Clamp до лока (риск №8):** `ingest_range(..., date2=завтра)` → `date2` подрезан к вчера (загруженные дни корректны); инвертированный диапазон (`date1>date2` после clamp) → `ValueError` **до** взятия лока (шов лока не вызван).
    - [ ] **Сбой дня → проброс (риск №6):** `load_day` бросает на 2-м дне → `ingest_range` пробрасывает; 1-й день (закоммиченный фейком) «остался»; 3-й день не начат; лок освобождён (`finally`).
    - [ ] **Итог `IngestRangeResult`:** `loaded_dates`/`skipped_dates`/`total_rows` корректны на смешанном диапазоне (часть skip, часть hot-window).
  - [ ] **Анти-зависимость (через `ast`, приём `test_parquet_store.py:387`/`test_p81_orchestrator.py`):** новый код не вводит top-level импортов `pandas`/`polars`/`numpy`/`pyarrow`/directaiq-инфры — проверяется существующим ast-тестом p81 (расширить, если 2.8 добавил импорты `datetime`/`collections.abc`/`typing` — они разрешены как stdlib).
  - [ ] **Без live:** 2.8 не вводит API-контракта (риск: live — у 2.7). Здесь — детерминированный offline.
- [ ] **Task 5 — Гейты верификации (обязательны перед закрытием)**
  - [ ] `uv run mypy scripts` → зелено (strict; `loaded: frozenset[tuple[str, str]]`; `anchor: date`; `IngestRangeResult` поля; `Iterator[date]`; без `Any`-дыр).
  - [ ] `uv run pytest` → зелено (новый `test_hot_window.py` + регрессия 1.x/2.1–2.7, включая `test_p81_orchestrator.py`/`test_row_check.py` без правок ожиданий; live отсеян). Обе ОС (digit-импорт/`importlib`/пути/`pathlib`).
  - [ ] `uv run pytest -m live` → 2.8 нового live не вводит; регрессия live 2.7 (`test_p81_orchestrator_live.py`) — без правок (или `skip` без кредов). Задокументировать в Dev Agent Record.
  - [ ] Новых зависимостей нет (`datetime`/`collections.abc`/`typing` — stdlib; `duckdb`/`requests` в стеке) → **`uv.lock` не меняется**.
  - [ ] Прогнать чек-лист «Definition of Done».

## Dev Notes

### Рекомендуемый контракт 2.8 (добавляется к `p81_load_logs.py`; финализируй под 2.9)

| Имя | Сигнатура | Смысл |
|---|---|---|
| `DEFAULT_HOT_WINDOW_DAYS` | `int = 3` | размер hot-window по умолчанию (FR-11; конфигурируем через `hot_window_days`) |
| `_select_days_to_load` | `(source: str, loaded: frozenset[tuple[str, str]], date1: date, date2: date, *, hot_window_days: int, anchor: date) -> list[str]` | **ЧИСТАЯ** (без conn/сети/часов): какие дни лить — skip загруженных, кроме hot-window (всегда); `N<0`→`ValueError`. Главный тестируемый шов (риск №2) |
| `_iter_dates` | `(date1: date, date2: date) -> Iterator[date]` | дни `[date1, date2]` включительно по возрастанию |
| `ingest_range` | `(source: str, date1: str, date2: str, *, hot_window_days: int = DEFAULT_HOT_WINDOW_DAYS, catalog: Catalog \| None = None, poll_interval_s=…, poll_timeout_s=…, max_consecutive_errors=…, sleep=time.sleep) -> IngestRangeResult` | **run-level диапазона** (вариант A): lock+conn+client+ensure+views+`reconcile` **один раз** → `_select_days_to_load` → `load_day` по каждому дню. Сбой дня → проброс |
| `IngestRangeResult` | `NamedTuple(source: str, loaded_dates: list[str], skipped_dates: list[str], total_rows: int)` | итог прогона диапазона для 2.9 (что перезалито/пропущено/сумма строк) |

**Использование (вариант A; лок ОДИН раз вокруг диапазона — НЕ `ingest_day` в цикле):**
```python
# 2.8 — обновить один источник за период (ad-hoc / зовётся 2.9 на источник):
result = ingest_range("visits", "2026-05-01", "2026-05-24")  # лок+conn+клиент внутри, один раз

# 2.9 (будущая) — оба источника под ОДНИМ локом + агрегация + exit-коды:
#   вынесет writer_lock/conn/client наружу и позовёт диапазонную логику по каждому source,
#   ловя исключение на источник (частичный сбой visits/hits → non-zero). Лок-scope —
#   зацикливающий вход (writer_lock не реентерабелен — риск №1/№7).
```

**Внутренний порядок `ingest_range` (риск №1/№3/№4/№8):**
```
# --- до лока (fail-fast) ---
_require_valid_source(source)
if hot_window_days < 0: raise ValueError(...)            # AC #5
today = moscow_today()                                   # ОДИН замер часов (риск №4)
d1, d2 = clamp_date_range(parse_date(date1), parse_date(date2), today_msk=today)  # clamp+валидация (риск №8)
anchor = today - timedelta(days=1)                       # == потолок clamp = вчера по МСК
# --- под локом ОДИН раз (AC #3) ---
with writer_lock():
    with DatabaseManager.connection() as conn:
        client = MetricaClient(token=creds.token, counter_id=creds.counter_id)  # creds = read_metrica_credentials()
        ensure_load_state_table(conn); create_views(conn)                       # один раз
        loaded = reconcile(conn, sources=[source])        # 2.4 — подтверждённо загруженные (мутирует мету → под локом)
        days = _select_days_to_load(source, loaded, d1, d2, hot_window_days=hot_window_days, anchor=anchor)
        for day in days:                                  # load_day НАПРЯМУЮ (не ingest_day — реентрантность!)
            total += load_day(conn, client, source, day, catalog=catalog, …poll-параметры…, sleep=sleep)
return IngestRangeResult(source, days, skipped, total)    # сбой load_day внутри цикла → проброс (риск №6)
```

### Карта примитивов 2.8 (что зовём; сигнатуры сверены с кодом)

- `p81_load_logs.load_day(conn, client, source, date, *, catalog=None, poll_interval_s=…, poll_timeout_s=…, max_consecutive_errors=…, sleep=time.sleep) -> int` (`p81_load_logs.py:107`) — **ядро дня** (2.7, вариант A): лок НЕ берёт, БД НЕ открывает, клиент НЕ строит; валидирует `date <= вчера`; коммит = `mark_loaded`; перезалив = `write_partition` одного файла. **Звать НАПРЯМУЮ в цикле** (риск №1). НЕ звать `ingest_day` (`:230` — он берёт лок).
- `load_state.reconcile(conn, *, sources=VALID_SOURCES) -> frozenset[tuple[str, str]]` (`load_state.py:192`) — подтверждённо-загруженные дни (факт+`loaded`+`row_count==факт`; **мутирует** `load_state` — `DELETE` ложной меты `:234` → под локом). Звать `sources=[source]` (риск №3/№7). Опора FR-9 (не `SELECT DISTINCT`).
- `dates.moscow_today() -> date` (`dates.py:49`), `moscow_yesterday() -> date` (`:58` — якорь окна), `parse_date` строгий `YYYY-MM-DD` (`:63`), `format_date` (`:85`), `clamp_date_range(date1, date2, *, today_msk=None) -> tuple[date, date]` (`:90` — клампит `date2`→вчера + fail на инверсии **до** возврата). Один `moscow_today()` → clamp-потолок и `anchor` консистентны (риск №4).
- `writer_lock(*, lock_path=None)` контекст-менеджер (`writer_lock.py:71`; `WriterLockHeldError` занят; **не реентерабелен** `:82` — лок ОДИН раз, риск №1). `DatabaseManager.connection(read_only=False)` (`database_manager.py:39` — write-conn). `MetricaClient(token, counter_id)` (`metrica_client.py`). `ensure_load_state_table(conn)` (`load_state.py:107`), `create_views(conn, *, catalog=None, sources=VALID_SOURCES)` (`views.py`, 2.6). `read_metrica_credentials() -> MetricaCredentials(token, counter_id)` (`env_reader.py:48`; fail-loud до сети). `VALID_SOURCES` (`catalog.py:35`), `Catalog`/`load_catalog` (`catalog.py`).

### Паттерны от историй 1.x/2.1–2.7 (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` уже первой строкой p81; русские docstrings/комментарии, английские идентификаторы; type hints везде, `mypy --strict`, без `Any`-дыр; абсолютные импорты от корня пакета; `logger = logging.getLogger(__name__)` (есть).
- **Тонкий run-level + чистое тестируемое ядро** (приём 2.6 `build_view_ddl`/2.7 `load_day`): `_select_days_to_load` без conn/сети/часов тестируется инъекцией `anchor`/`loaded`; `ingest_range` исполняет с реальными лок/conn/клиент/`reconcile`/`load_day`. Решение и обвязка разделены.
- **conn/client инъектируются в `load_day`** (зовём как есть, БД/клиента строит run-level `ingest_range` один раз). `catalog=None`→`load_catalog()` (шов как `parquet_store`/`load_day`).
- Fail-loud наследуется: невалидный `source` → `ValueError` (`VALID_SOURCES`); инвертированный диапазон/будущий `date1` → `ValueError` из `clamp_date_range` до лока; `N<0` → `ValueError`; нет кредов → `ValueError` из `read_metrica_credentials` до сети; `RowCountMismatchError` (2.3) из `load_day` — наружу (НЕ глушить).
- Анти-зависимость через `ast` (приём `test_parquet_store.py:387`/`test_p81_orchestrator.py`): `duckdb`/`scripts.utils.*`/stdlib разрешены; directaiq-инфра/аналит-стек — нет.
- **Импорт p81 в тестах/CLI — через `importlib.import_module`** (digit-префикс каталога; образец 2.7). 2.9 импортирует `ingest_range` так же.

### Границы 2.8 (не выходить)

- Дополняем `scripts/8x_metrica_logs_api/p81_load_logs.py` (+ `tests/test_hot_window.py` + дополнение `docs/ingestion.md`). **Новое только:** `_select_days_to_load`, `_iter_dates`, `ingest_range`, `IngestRangeResult`, константа `DEFAULT_HOT_WINDOW_DAYS`. **Не** реализуем заново: `load_day`/poll/download/parse (2.7), запись (2.2), сверку (2.3), мету/`reconcile` (2.4), лок (2.5), view (2.6), клиент/даты/каталог/креды (1.x) — **зовём готовое**.
- **Команда `gdau-logs update` (argparse-поверхность, exit-коды, прогон обоих источников под одним локом, агрегация частичного сбоя, resumable-сообщение про дневную квоту) — 2.9** (epics.md:354–369). 2.8 даёт **функцию** на один источник; 2.9 крутит источники и владеет UX/кодами (риск №6/№7).
- MCP-чтение, конкуренция читатель↔писатель на Windows `os.replace` — 3.1 (defer 2.2 `deferred-work.md:31`; читателей пока нет).
- Лок берётся ОДИН раз в `ingest_range`; `load_day` зовётся напрямую (не `ingest_day` — реентрантность, риск №1). Не тащить инфру directaiq (`BaseScript`/`config_manager`/параллель/`DROP TABLE`/`view_builders`, NFR-6).

### Project Structure Notes

- Код 2.8 — в `scripts/8x_metrica_logs_api/p81_load_logs.py` (architecture.md:458 «hot-window (N=3)»; FR-9/10/11 → p81 по architecture.md:528–529). Каталог без `__init__.py` (неявный namespace) → импорт через `importlib.import_module` (как 2.7). Имена snake_case; type hints обязательны (mypy strict).
- Тест — `tests/test_hot_window.py` (имя дословно по architecture.md:484; отдельно от `test_p81_orchestrator.py` 2.7). Конфиг pytest (`markers`/`addopts`) есть (1.3); `conftest.py`/`tests/__init__.py` нет — `tmp_path`/`monkeypatch`/`importlib` напрямую.
- `docs/ingestion.md` — **дополняется** (Task 3): уже обещает «решение какие дни / hot-window — 2.8» (`docs/ingestion.md:229–231`). Часть DoD (project-context: компонент без актуальной спеки не «готов»).
- `gdau.duckdb`/`*.parquet`/`.writer.lock`/`*.tsv` — артефакты хранилища (`GDAU_DATA_ROOT`), в dev-репо не создаются/не коммитятся (`.gitignore`). Offline-тесты пишут только в `tmp_path`.
- `uv.lock` не трогаем (всё в стеке/stdlib). Не переводить на src-layout, не переименовывать пакет `scripts` (hatchling `packages=["scripts"]`). Не реорганизовывать раскладку.

### Definition of Done — чек-лист самопроверки

1. `p81_load_logs.py` дополнен: чистое `_select_days_to_load` (без conn/сети/часов; skip загруженных + hot-window всегда; `N<0`→`ValueError`) + `_iter_dates` + run-level `ingest_range` (lock+conn+client+ensure+views+`reconcile` ОДИН раз → `load_day` по каждому дню) + `IngestRangeResult` + `DEFAULT_HOT_WINDOW_DAYS=3`. (AC #1–#5)
2. Инкремент по `reconcile` (факт+`loaded`+`row_count==факт`), НЕ `SELECT DISTINCT date`/наличие файла; загруженные пропускаются. (AC #1, FR-9, риск №3)
3. Перезалив дня = повторный `load_day`→`write_partition` одного файла, без `DROP`; идемпотентно. (AC #2, FR-10, риск №5)
4. Hot-window: последние N дней (default 3) перезаливаются всегда; якорь = «вчера по МСК», окно клипуется к диапазону итерацией (НЕ якорь на `date2`); `N=0` off, `N<0` `ValueError`. (AC #3/#5, FR-11, риск №4)
5. Hot-window побеждает skip — день и в `loaded`, и в окне → перезаливается. (AC #4, риск №5)
6. Лок ОДИН раз в `ingest_range`; `load_day` напрямую (НЕ `ingest_day` в цикле — реентрантность); `reconcile`/`create_views`/`ensure_table` один раз под локом. (AC #3, риск №1)
7. Clamp диапазона + валидация N — **до** лока (fail-fast); инвертированный/будущий диапазон → `ValueError` без взятия лока; один замер `moscow_today()`. (риск №8/№4)
8. Сбой дня → проброс (НЕ глушить); закоммиченные дни остаются, повтор до-грузит через инкремент (resumable); агрегация источников/exit-коды/квота — 2.9. (риск №6/№7)
9. `docs/ingestion.md` дополнен разделом «Какие дни грузить: инкремент и hot-window» (3 вопроса; границы 2.2–2.7/2.9/3.1 названы; «Границы» обновлены) — DoD компонента. (Task 3)
10. Offline `tests/test_hot_window.py` (имя по architecture.md:484): чистое `_select_days_to_load` (AC #1/#3/#4/#5 + якорь/клиппинг) + интеграция `ingest_range` (лок один раз, clamp до лока, сбой→проброс, `IngestRangeResult`) + ast-анти-зависимость; поддельный `load_day`/мок-окружение, без сети/реальных пауз, `importlib`-импорт. (Task 4)
11. Live 2.8 не вводится (нет нового API-контракта); регрессия live 2.7 без правок. (Task 5)
12. `uv run mypy scripts` и `uv run pytest` — зелёные на обеих ОС; `test_p81_orchestrator.py`/`test_row_check.py` зелёные без правок ожиданий; `uv.lock` не менялся; `data/`-артефактов в dev-репо не создано. (Task 5)
13. Велась в отдельной ветке `story/2.8-increment-hotwindow` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС. PR в `main`.

### Latest Tech Information

- **Веб-ресёрч не требуется:** стек зафиксирован локом; 2.8 — чистая логика над готовыми примитивами, нового внешнего контракта нет (реальный цикл API подтверждён live-smoke 2.7). `datetime`/`collections.abc`/`typing` — stdlib.
- **`frozenset[tuple[str, str]]` от `reconcile`** — ключ `(source, date)`, `date` в формате `YYYY-MM-DD` (`load_state._load_meta` приводит `.isoformat()`). Сравнение в `_select_days_to_load` — `(source, format_date(d)) in loaded` (тот же формат). Согласовано по форме ключа.
- **Hot-window анти-паттерн (НЕ копировать directaiq):** directaiq перезаливал через `_force_drop_tables`(`DROP TABLE`) — у нас перезалив = `write_partition` одного `{date}.parquet` (2.2), без `DROP`. 2.8 нового пути записи не вводит — только решает набор дней.
- **`importlib.import_module` для digit-префикс-пакета** (как 2.7): `import_module("scripts.8x_metrica_logs_api.p81_load_logs")` работает (строка), `import scripts.8x_…` как statement = `SyntaxError`. Образец для импорта `ingest_range` в CLI `update` (2.9).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.8] (строки 340–352) — user story + 5 AC (инкремент FR-9, перезалив FR-10, hot-window FR-11, приоритет окна, якорь/границы N).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-9/FR-10/FR-11] (строки 35–37) — догрузка только отсутствующих (мета+факт, не `DISTINCT date`); перезапись одной партиции без `DROP`; hot-window N конфиг default 3, опирается на per-day идемпотентность.
- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.9] (строки 354–369) — границы: команда `update`, exit-коды, агрегация visits+hits, resumable-сообщение про квоту — НЕ 2.8.
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2] (строки 230–232) — место 2.8 в цепочке 2.1→2.9 (после 2.7 один день; 2.9 поверхность).
- [Source: _bmad-output/planning-artifacts/architecture.md:458] — `p81_load_logs.py` несёт «hot-window (N=3)»; [architecture.md:484] — отдельный тест `test_hot_window.py`; [architecture.md:528–529] — FR-9/10/11 → `p81_load_logs.py`.
- [Source: _bmad-output/planning-artifacts/architecture.md:379–384] — протокол идемпотентного дня; перезалив = перезапись одного файла без `DROP`; реконсиляция на старте (источник истины — факт партиции); лок один раз, fail-fast.
- [Source: _bmad-output/planning-artifacts/architecture.md:536–538] — поток приёма (write): `update`→p81 лок→клиент→`parquet_store`→сверка→rename→`load_state`; «Hot-window перезаливает N последних дней».
- [Source: _bmad-output/project-context.md:105–111] — целостность: реконсиляция мета×факт; перезалив = один файл без `DROP`; один писатель (лок), чтение лок не берёт.
- [Source: scripts/8x_metrica_logs_api/p81_load_logs.py:107–227] — `load_day` (вариант A, инъекция conn/client, коммит=`mark_loaded`, лок НЕ берёт); [:230–274] — `ingest_day` (run-level один день, лок один раз — НЕ звать в цикле); [:91–93] — poll-константы.
- [Source: scripts/utils/load_state.py:192–244] — `reconcile(conn, *, sources=VALID_SOURCES) -> frozenset[tuple[str,str]]` (три условия «загружен», мутирует мету `DELETE` `:234` → под локом); [:107] `ensure_load_state_table`.
- [Source: scripts/utils/dates.py:49–116] — `moscow_today`/`moscow_yesterday` (якорь), `parse_date`/`format_date` (строгий `YYYY-MM-DD`), `clamp_date_range(…, today_msk=)` (клампит `date2`→вчера + fail на инверсии до возврата).
- [Source: scripts/utils/writer_lock.py:71–95, 82] — `writer_lock(*, lock_path=None)`; **не реентерабелен** «один захват на весь прогон» (риск №1).
- [Source: scripts/utils/database_manager.py:39] — `DatabaseManager.connection(read_only=False)` write-conn. [scripts/utils/env_reader.py:48] — `read_metrica_credentials()` fail-loud до сети. [scripts/utils/views.py] — `create_views(conn, …)` (2.6). [scripts/utils/catalog.py:35] — `VALID_SOURCES`.
- [Source: docs/ingestion.md:205–231] — «Границы»: «решение какие дни / пропуск загруженных / перезалив свежего окна (hot-window) — история 2.8» (Task 3 переводит в «реализовано»).
- [Source: tests/test_parquet_store.py:387] — паттерн ast-анти-зависимости (import-узлы); зеркало для `test_hot_window.py`.
- [Source: _bmad-output/implementation-artifacts/2-7-…p81-полный-цикл.md] — вариант A (УТВЕРЖДЁН Шефом 2026-05-25): `writer_lock` не реентерабелен → диапазон (2.8) берёт лок ОДИН раз и зовёт `load_day` напрямую, НЕ `ingest_day` в цикле (риск №1 этой истории — прямое следствие).
- [Memory: gamedev-analytics-unit prd] — FR-9/10/11 (инкремент/перезалив/hot-window N=3). [[simplicity-first]] — чистая функция решения вместо инфры directaiq; per-day-коммит+incremental-skip вместо отдельного resume-механизма. [[structure-mirror-directaiq]] — hot-window в `p81_load_logs.py` по дереву архитектуры, без `_force_drop_tables`/`config_manager`.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-05-25 — Story 2.8 создана (create-story): инкремент, идемпотентный перезалив и hot-window (FR-9/FR-10/FR-11). Дополнение `scripts/8x_metrica_logs_api/p81_load_logs.py` диапазонным слоем над `load_day` (2.7): чистое `_select_days_to_load` (skip подтверждённо-загруженных по `reconcile` 2.4, кроме hot-window — главный тестируемый шов без conn/сети/часов) + `_iter_dates` + run-level `ingest_range` (lock+conn+client+ensure+views+reconcile ОДИН раз → `load_day` напрямую по каждому дню — НЕ `ingest_day` в цикле, `writer_lock` не реентерабелен, прямое следствие варианта A 2.7) + `IngestRangeResult` + `DEFAULT_HOT_WINDOW_DAYS=3`. Разобраны риски: лок один раз/зацикливающий вход (№1); чистый шов решения (№2); инкремент через `reconcile`, не `DISTINCT date` (№3, FR-9); якорь hot-window = вчера по МСК, окно клипуется к диапазону, не якорь на `date2`, `N=0` off/`N<0` fail, один замер часов (№4, AC #5); hot-window > skip (№5, AC #4); сбой дня → проброс, resumable через per-day-коммит+incremental-skip, агрегация/exit-коды/квота — 2.9 (№6/№7); clamp+валидация N до лока fail-fast (№8); `load_day` сам валидирует `date<=вчера` (№9); не трогать путь записи/примитивы, без инфры directaiq (№10). Перезалив дня = `load_day`→`write_partition` одного файла, без `DROP` (FR-10, AC #2). 2.8 offline (нет нового API-контракта — live у 2.7); тест `tests/test_hot_window.py` (имя по architecture.md:484): чистое решение (AC #1/#3/#4/#5 + якорь/клиппинг) + интеграция `ingest_range` (лок один раз, clamp до лока, сбой→проброс, итог) + ast-анти-зависимость. Дополнение `docs/ingestion.md` (раздел «Какие дни грузить»). Зависимость: 2.7 (`load_day`/`ingest_day`) — на момент создания `in-progress`; сигнатуры сверены с фактическим кодом p81. Статус → ready-for-dev.
