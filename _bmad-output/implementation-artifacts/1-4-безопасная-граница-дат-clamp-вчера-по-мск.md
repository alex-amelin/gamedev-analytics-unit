# Story 1.4: Безопасная граница дат — clamp «вчера по МСК»

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want чтобы запросы не уходили за «вчера по МСК»,
so that не нарушать `date2 < today` Logs API и не тянуть неполные данные.

**Контекст эпика.** Четвёртая история Epic 1 «Каркас юнита и канал Logs API». Каркас (1.1 = done), env-ридер (1.2 = done), вендоренный `MetricaClient` (1.3 = done) уже стоят. Эта история кладёт **независимый stdlib-примитив** `scripts/utils/dates.py` — единственное место, где живёт правило «`date2` не дальше вчера по МСК» + строгий формат `YYYY-MM-DD`. Покрывает **FR-5** (NFR-3/безопасность данных по краю). От него зависят: CLI `create` (1.6 — клампит `date2` перед запросом), оркестратор p81 (2.7) и hot-window (2.8 — якорь окна = «вчера по МСК», берётся отсюда).

**Это НЕ вендоринг.** В directaiq аналог (`scripts/utils/date_utils.py` + clamp в `p81_load_logs.py::_process_date_range`) тянет тяжёлый `pytz` (`pytz.timezone("Europe/Moscow")`) — в наш стек `pytz` НЕ входит и тащить его нельзя (NFR-6 «простота-первой»). directaiq-код берётся **только как прецедент формы логики**, не копируется. Пишем крошечную версию на чистой stdlib `datetime`.

**Главные риски истории.**
1. **Таймзона через зависимость или системную БД зон.** Не `pytz` (нет в стеке) и **не `zoneinfo("Europe/Moscow")** — `zoneinfo` на Windows требует пакет `tzdata` (stdlib НЕ несёт базу зон под Windows) → либо новая зависимость, либо `ZoneInfoNotFoundError` в рантайме на машине Шефа. Решение: **фиксированный офсет `timezone(timedelta(hours=3))`** — ноль зависимостей, кросс-платформенно, корректно (Москва постоянно UTC+3, без DST с 26.10.2014; все наши даты — после 2014). Это прямо обслуживает NFR-2 (Win↔Linux) и NFR-6.
2. **Clamp адресует только `date2`, оставляя дыру в `date1`.** directaiq клампил `end_date`, а на `start_date > safe_end` тихо возвращал «успех, 0 файлов». Наш AC #5 требует **жёсткой ошибки** на будущем `date1` / инвертированном диапазоне после clamp — осознанное отличие от directaiq (fail-loud, не молчаливый успех).
3. **Нестрогий парсинг даты.** `date.fromisoformat` в 3.11+ принимает basic-формат (`20260524`) и иные ISO-формы — это НЕ «строго YYYY-MM-DD». Нужен guard на каноничный вид (см. Dev Notes).
4. **Недетерминированные тесты на стене часов.** «Сегодня по МСК» от `datetime.now` делает clamp-тесты флаки. Нужен инъектируемый шов времени (см. Dev Notes → «Тестируемость»).

## Acceptance Criteria

1. **Given** `date2` = сегодня/будущее, **When** вычисляется граница, **Then** `date2` зажимается на «вчера по МСК» с записью в лог, без падения.
2. **Given** `date2` ≤ вчера по МСК, **When** вычисляется граница, **Then** значение не меняется.
3. **Given** любые даты, **When** они форматируются для Logs API, **Then** формат строго `YYYY-MM-DD`.
4. **Given** смену локальной таймзоны машины, **When** вычисляется «вчера», **Then** расчёт опирается на МСК (UTC+3), а не на локальную зону.
5. **Given** `date1` в будущем ИЛИ `date1 > date2` после clamp, **When** вычисляется граница, **Then** поднимается понятная ошибка «пустой/инвертированный диапазон», без отправки запроса. _[edge-case: clamp адресовал только date2]_
6. **Given** неразбираемую дату на входе, **When** она парсится, **Then** строгий парсинг `YYYY-MM-DD` с понятной ошибкой (не падение в clamp-логике). _[edge-case: мусорная дата]_

## Tasks / Subtasks

- [x] **Task 1 — Создать `scripts/utils/dates.py` со stdlib-таймзоной МСК (AC: #4)**
  - [x] `from __future__ import annotations` первой строкой кода (инвариант проекта — так в каждом модуле).
  - [x] Модульный docstring **на русском**: роль модуля (единственное место правила «`date2` ≤ вчера по МСК» + строгий формат `YYYY-MM-DD` для Logs API; потребители — CLI 1.6, p81 2.7, hot-window 2.8). Идентификаторы — английские.
  - [x] `import logging` + `logger = logging.getLogger(__name__)` (как в `env_reader.py`; project-context «только stdlib logging»).
  - [x] **Константа `MSK = timezone(timedelta(hours=3))`** — фиксированный офсет, НЕ `zoneinfo`/`pytz`. Комментарий «почему» прямо у константы: Москва постоянно UTC+3 (без DST с 2014), `zoneinfo("Europe/Moscow")` требует `tzdata` на Windows → фикс-офсет ноль-зависимостей и кросс-платформенно. _[anti-pattern: zoneinfo/pytz тянут зависимость/ломаются на Windows]_
  - [x] Импорты: `from datetime import date, datetime, timedelta, timezone`, `import logging`, `import re` (для guard формата). Без сторонних импортов.
- [x] **Task 2 — Шов времени и «сегодня/вчера по МСК» (AC: #4)**
  - [x] `def _now_utc() -> datetime: return datetime.now(timezone.utc)` — **единственный шов к стене часов** (тесты его монкейпатчат фиксированным aware-UTC инстантом). Внутренний (нижнее подчёркивание).
  - [x] `def moscow_today() -> date: return _now_utc().astimezone(MSK).date()` — сегодня по МСК. Независимо от локальной зоны машины (инстант берётся в UTC, переводится в МСК). _AC #4._
  - [x] `def moscow_yesterday() -> date: return moscow_today() - timedelta(days=1)` — потолок clamp и **якорь hot-window** (потребляется 2.8). Публичная.
- [x] **Task 3 — Строгий парсинг и форматирование `YYYY-MM-DD` (AC: #3, #6)**
  - [x] `def parse_date(value: str) -> date`: **guard каноничного вида** `re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip())` → иначе `raise ValueError`; затем `date.fromisoformat(...)`. Guard обязателен: голый `fromisoformat` в 3.11+ принимает basic-формат `20260524` и week-даты — это не «строго YYYY-MM-DD» (AC #3). Сообщение об ошибке содержит сам некорректный ввод (`{value!r}`) — даты не секрет, помогает диагностике. _[edge-case: мусорная дата → ValueError, НЕ падение в clamp]_
  - [x] `def format_date(value: date) -> str: return value.isoformat()` — для `date` всегда даёт `YYYY-MM-DD` (zero-padded). _AC #3._
- [x] **Task 4 — Ядро: clamp + валидация диапазона (AC: #1, #2, #5)**
  - [x] `def clamp_date_range(date1: date, date2: date, *, today_msk: date | None = None) -> tuple[date, date]`:
    ```python
    ceiling = (today_msk if today_msk is not None else moscow_today()) - timedelta(days=1)
    clamped2 = date2
    if date2 > ceiling:
        logger.info("Clamp date2 %s → %s (вчера по МСК)", date2, ceiling)
        clamped2 = ceiling
    if date1 > clamped2:
        raise ValueError(
            f"Пустой/инвертированный диапазон: date1={date1} > date2={clamped2} "
            f"(вчера по МСК {ceiling})"
        )
    return date1, clamped2
    ```
  - [x] **Параметр `today_msk` (keyword-only, default `None`)** — инъекция «сегодня» для детерминированных тестов; в проде не передаётся (берётся `moscow_today()`). _Не_ городить отдельный «мокабельный» класс — одного kwarg достаточно.
  - [x] **AC #1:** `date2` сегодня/будущее (> ceiling) → лог INFO + `clamped2 = ceiling`, без исключения.
  - [x] **AC #2:** `date2 ≤ ceiling` (в т.ч. ровно `== ceiling`) → не меняется, лог не пишется (нет off-by-one на границе).
  - [x] **AC #5 (единственная проверка `date1 > clamped2` ловит оба кейса):** будущий `date1` (> ceiling ≥ clamped2) и инвертированный диапазон (`date1 > date2`, обе в прошлом, clamp не сработал) → `ValueError`. Функция чистая — `raise` происходит ДО возврата, т.е. до любого сетевого вызова у потребителя. **Осознанное отличие от directaiq:** там `start>safe_end` → тихий «успех, 0 файлов»; у нас — fail-loud.
  - [x] `__all__ = ["MSK", "moscow_today", "moscow_yesterday", "parse_date", "format_date", "clamp_date_range"]`.
- [x] **Task 5 — Offline-тесты `tests/test_dates.py` (AC: #1–#6)** — _см. Dev Notes → «Тестирование»_
  - [x] `from __future__ import annotations`; без сети. Детерминизм: в clamp-тестах **всегда передавать `today_msk=date(...)`** (не зависеть от стены часов).
  - [x] **AC #4 (МСК, не локальная зона) — через шов `_now_utc`:** `monkeypatch.setattr("scripts.utils.dates._now_utc", lambda: datetime(2026, 5, 24, 22, 30, tzinfo=timezone.utc))`. В UTC дата = 24-е, в МСК (=01:30 25-го) → `moscow_today() == date(2026,5,25)`, `moscow_yesterday() == date(2026,5,24)`. Доказывает, что используется МСК, а не UTC/локальная. (Симметрично можно проверить инстант, где МСК-дата < следующего UTC-дня — необязательно.)
  - [x] **AC #1:** `clamp_date_range(date(2026,5,1), date(2026,5,25), today_msk=date(2026,5,25))` → `date2 == date(2026,5,24)`; `caplog` содержит INFO про clamp.
  - [x] **AC #1 (будущее):** `date2 = date(2030,1,1)` → клампится к вчера; без исключения.
  - [x] **AC #2:** `date2 == ceiling` (== вчера) → не меняется, лог пуст; `date2 < ceiling` → не меняется. _Граница без off-by-one._
  - [x] **AC #3:** `format_date(date(2026,5,1)) == "2026-05-01"` (zero-pad); `parse_date("2026-05-01") == date(2026,5,1)`; round-trip.
  - [x] **AC #5:** будущий `date1` (`date1=date(2030,1,1)`, любой `date2`) → `ValueError` (match «инвертирован|пустой»); инвертированный (`date1=date(2026,5,20), date2=date(2026,5,10), today_msk` поздняя) → `ValueError`. Проверить, что текст содержит обе даты.
  - [x] **AC #6 (параметризовать мусор):** `["", " ", "garbage", "2026/05/24", "24-05-2026", "2026-13-01", "2026-05-40", "20260524", "2026-W21-1", "2026-5-1", "0000-00-00"]` → каждый `parse_date(...)` поднимает `ValueError` (match — имя/значение). **Guard критичен именно для `20260524` (basic-формат) и `2026-W21-1` (week-дата): голый `date.fromisoformat` в 3.13 их ПРИНЯЛ БЫ** (→ `2026-05-24`/`2026-05-18`) — guard `\d{4}-\d{2}-\d{2}` их отсекает. `2026-5-1`/`2026/05/24`/`24-05-2026` отвергаются и guard'ом (нет каноничного вида). `0000-00-00`/`2026-13-01`/`2026-05-40` проходят guard, но `fromisoformat` добивает (`year 0 out of range` / невалидный месяц/день) — демонстрирует связку. Подтвердить, что это ошибка парсинга, а не падение в clamp.
  - [x] **Анти-зависимость (закрепляет решение Task 1) — через `ast`, не подстроку** (docstring/комментарии содержат `zoneinfo`/`pytz` → ложный красный): распарсить `ast` модуля, проверить, что в `Import`/`ImportFrom`-узлах НЕТ `zoneinfo`, `pytz`, `tzdata`. (Приём из `tests/test_env_reader.py::test_no_heavy_dependencies_imported`.) Гарантирует фикс-офсет и кросс-платформенность.
  - [x] Один день: `clamp_date_range(date(2026,5,24), date(2026,5,24), today_msk=date(2026,5,25))` → `(24, 24)` без ошибки (одинокий валидный день == вчера).
- [x] **Task 6 — Гейты верификации (обязательны перед закрытием)**
  - [x] `uv run mypy scripts` → зелено (strict; модуль полностью типизирован, stdlib-only, `uv.lock` не меняется — новых зависимостей нет).
  - [x] `uv run pytest` → зелено (новые тесты + 1.1/1.2/1.3; live по-прежнему отсеян `addopts="-m 'not live'"`).
  - [x] Прогнать чек-лист «Definition of Done» из Dev Notes.

> **Live-smoke НЕ требуется.** `dates.py` не ходит во внешний API — правило project-context «обязателен live-smoke» относится к компонентам, дёргающим Logs API (как 1.3). Не заводить пустой `@pytest.mark.live`.

## Dev Notes

### Источник прецедента (не вендорим — пишем заново на stdlib)

directaiq (ref `7718bd65`): `scripts/utils/date_utils.py::get_moscow_safe_end_date()` = `datetime.now(pytz.timezone("Europe/Moscow")).date() - timedelta(days=1)`; clamp в `scripts/8x_metrica_logs_api/p81_load_logs.py::_process_date_range` (строки ~328–338): клампит `end_date` до `safe_end`, на `start_date > safe_end` логирует warning и **возвращает «успех, 0 файлов»**. Берём **форму логики** (вчера по МСК = потолок), но: (1) `pytz` → фиксированный офсет stdlib; (2) тихий «0 файлов» на инвертированном диапазоне → **жёсткая ошибка** (AC #5). Это два осознанных отличия от directaiq.

### Таймзона МСК — фиксированный офсет, не библиотека (AC #4, NFR-2/6)

- **`MSK = timezone(timedelta(hours=3))`.** Москва — постоянный UTC+3 с 26.10.2014 (отмена «зимнего времени», ФЗ-№193); DST нет. Все даты, с которыми работает юнит (данные Logs API, недавние дни), — после 2014 → фиксированный офсет точен.
- **Почему не `zoneinfo`:** `zoneinfo.ZoneInfo("Europe/Moscow")` на Windows ищет системную базу зон, которой в Windows нет → нужен пакет `tzdata` (новая зависимость) либо рантайм-`ZoneInfoNotFoundError`. Среда разработки Шефа — Windows 11 (см. CLAUDE.md). Фикс-офсет это исключает.
- **Почему не `pytz`:** тяжёлая зависимость, нет в стеке; `pytz` к тому же требует `localize()` (легко ошибиться). Запрещено NFR-6.
- **Независимость от локальной зоны:** `datetime.now(timezone.utc)` берёт абсолютный инстант; `.astimezone(MSK)` переводит в МСК независимо от `TZ` машины. Поэтому `moscow_today()` корректен на любой локальной зоне (AC #4) — это и проверяет тест через шов `_now_utc`.

### Строгий парсинг `YYYY-MM-DD` (AC #3, #6)

`date.fromisoformat` с Python 3.11 расширен и принимает не только `YYYY-MM-DD`, но и basic-формат (`20260524` → `2026-05-24`) и week-даты (`2026-W21-1` → `2026-05-18`) — это шире контракта (проверено на 3.13). Поэтому: **сначала** `re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip())` (каноничный вид, zero-padded — режет basic/week-формы, чужой разделитель `2026/05/24`, не-zero-pad `2026-5-1`), **потом** `date.fromisoformat` (добивает невалидные календарные: `2026-13-01`, `2026-05-40`, `0000-00-00` → `ValueError`). Связка обязательна: guard ловит то, что `fromisoformat` ошибочно принял бы, `fromisoformat` — то, что прошло по форме, но невалидно календарно. Сообщение об ошибке несёт `{value!r}` — дата не секрет.

### Тестируемость — один шов времени

Единственная точка к стене часов — `_now_utc()`. Тесты на «сегодня/вчера по МСК» (AC #4) монкейпатчат `scripts.utils.dates._now_utc` фиксированным aware-UTC инстантом — детерминированно и без `time.tzset`/манипуляций `TZ` (на Windows `tzset` отсутствует — лишняя кросс-платформенная ловушка). Тесты clamp/валидации (AC #1,#2,#5) **не** трогают часы вовсе — передают `today_msk=date(...)` явно. Так набор зелёный в любой день и в любой зоне CI.

### Контракт с потребителями (НЕ реализуем здесь — для 1.6/2.7/2.8)

- **CLI `create` (1.6):** `d1 = parse_date(args.date1); d2 = parse_date(args.date2)` → `d1, d2 = clamp_date_range(d1, d2)` → `client.create_log_request(date1=format_date(d1), date2=format_date(d2), source=...)`. Парсинг (AC #6) и clamp (AC #5) ловятся CLI и дают ненулевой код + сообщение (паттерн как с `ValueError` env-ридера в 1.2).
- **hot-window (2.8):** якорь окна = `moscow_yesterday()` отсюда; окно клипуется к загруженному диапазону. Поэтому `moscow_yesterday()` — публичная.
- **Границы 1.4:** только примитивы дат. Никакого argparse, сетей, путей хранилища, чтения каталога. Не добавлять строковую «обёртку всё-в-одном» (parse+clamp+format) — потребители собирают сами (явная цепочка читаемее, AC #6 отделяет ошибку парсинга от clamp).

### Project Structure Notes

- Модуль — `scripts/utils/dates.py` ровно по карте архитектуры (`utils/dates.py` — «clamp date2 «вчера по МСК», формат YYYY-MM-DD»). Каталог `scripts/utils/` — регулярный пакет (`__init__.py` из 1.1) → `from scripts.utils.dates import clamp_date_range, moscow_yesterday, parse_date, format_date` резолвится.
- Импорты абсолютные от корня пакета; имена snake_case (модуль/функции), константа `MSK` — UPPER_CASE; type hints обязательны (mypy strict).
- `tests/` зеркалит `scripts/`: `tests/test_dates.py`. `[tool.pytest.ini_options]` уже заведён (1.3: маркеры + `addopts`); `testpaths` не настроен (deferred 1.1) — не задача 1.4.
- **Спека компонента — НЕ отдельный файл.** project-context прямо относит `dates` к «мелким хелперам, которые описываются внутри родственной спеки, а не отдельным файлом» (вместе с `paths`, `logging_utils`). Родственная спека (`cli.md` от 1.6 или `ingestion.md` от 2.7) ещё не существует. Решение: **не заводить `docs/dates.md`**; контракт несёт подробный модульный docstring + эта история; человекочитаемый абзац про clamp ложится в `cli.md`/`ingestion.md`, когда та спека родится. (Отличие от 1.3, где `metrica_client` получил отдельный `docs/metrica-client.md` — там это самостоятельный компонент, а не «мелкий хелпер» из явного списка project-context.) _Вынесено в финальные вопросы Шефу; по умолчанию применяю правило project-context — [[feedback-decide-and-apply]]._
- Конфликтов со структурой нет. Не реорганизовывать раскладку, не переводить на src-layout.

### Definition of Done — чек-лист самопроверки

1. `scripts/utils/dates.py` создан; `from __future__ import annotations` первой строкой; модульный docstring русский; идентификаторы английские.
2. `MSK = timezone(timedelta(hours=3))` (фикс-офсет); НЕ импортируются `zoneinfo`/`pytz`/`tzdata` (тест по `ast` зелёный). (AC #4)
3. `moscow_today`/`moscow_yesterday` через шов `_now_utc`; корректны независимо от локальной зоны. (AC #4)
4. `parse_date` — guard `\d{4}-\d{2}-\d{2}` + `date.fromisoformat`, понятная ошибка с вводом; `format_date` даёт zero-padded `YYYY-MM-DD`. (AC #3, #6)
5. `clamp_date_range`: `date2 > вчера` → clamp + INFO-лог, без падения (AC #1); `date2 ≤ вчера` (вкл. границу) → без изменений и без лога (AC #2); будущий `date1`/инвертированный диапазон → `ValueError` (AC #5). `today_msk` инъектируется для тестов.
6. Тесты покрывают: AC #1 (сегодня+будущее), AC #2 (граница ==вчера, <вчера), AC #3 (формат+round-trip), AC #4 (МСК через `_now_utc`-шов), AC #5 (будущий date1 + инвертированный), AC #6 (параметризованный мусор, вкл. `20260524`/`2026-W21-1` — guard режет принятое бы `fromisoformat`), анти-зависимость по `ast`, одинокий день ==вчера. Часы в clamp-тестах инъектированы (`today_msk`), не стена.
7. `uv run mypy scripts` и `uv run pytest` — зелёные; `uv.lock` не менялся (новых зависимостей нет).
8. Велась в отдельной ветке `story/1.4-dates-clamp` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС (ubuntu + windows — кросс-платформенность фикс-офсета критична).

### Latest Tech Information

- **Python 3.13 stdlib `datetime`:** `timezone(timedelta(hours=3))` — фикс-офсет, ноль зависимостей. `datetime.now(tz)` / `.astimezone(tz)` дают инстант независимо от локальной `TZ`.
- **`zoneinfo` (3.9+):** на Windows нет системной базы зон → нужен пакет `tzdata`, иначе `ZoneInfoNotFoundError`. Поэтому для постоянного UTC+3 фикс-офсет предпочтительнее (наш кейс).
- **`date.fromisoformat` (3.11+):** принимает расширенный ISO 8601 (вкл. basic-формат `20260524`) → нужен regex-guard для строгого `YYYY-MM-DD`. `date.isoformat()` всегда возвращает `YYYY-MM-DD`.
- **Москва UTC+3 без DST** с 26.10.2014 — фикс-офсет корректен для всех релевантных дат. Web-ресёрч не требуется (stdlib + зафиксированный факт о таймзоне).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 1.4] — user story + 6 AC (усилены edge-case hunter).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-5] — clamp `date2` на «вчера по МСК» с записью в лог (Logs API требует `date2 < today`).
- [Source: _bmad-output/planning-artifacts/architecture.md#Format Patterns] — «Даты/время: формат `YYYY-MM-DD` везде; таймзона МСК для clamp «вчера»». (строки 362–363)
- [Source: _bmad-output/planning-artifacts/architecture.md#Дерево репозитория] — `utils/dates.py` = «clamp date2 «вчера по МСК», формат YYYY-MM-DD». (строка 455)
- [Source: _bmad-output/planning-artifacts/architecture.md#Requirements to Structure Mapping] — «FR-5 clamp → `utils/dates.py`». (строка 525)
- [Source: _bmad-output/project-context.md#Logs API и креды] — «`date2` clamp на «вчера по МСК». Формат дат `YYYY-MM-DD` везде». (строка 116)
- [Source: _bmad-output/project-context.md#Документация компонентов] — `dates`/`paths`/`logging_utils` — мелкие хелперы, описываются внутри родственной спеки, не отдельным файлом. (строка 56)
- [Source: _bmad-output/project-context.md#Critical Don't-Miss Rules] — «Никогда: `date2 = today`. Всегда clamp на «вчера по МСК»». (строка 183)
- [Source: scripts/utils/env_reader.py] — паттерн модуля (1.2): `from __future__ import annotations`, stdlib `logging`, русский docstring, `ValueError` fail-loud, `__all__`.
- [Source: tests/test_env_reader.py] — паттерн offline-тестов: `monkeypatch`, autouse-изоляция, анти-зависимость через `ast` (не подстроку).
- [Source: G:/git/directaiq/scripts/utils/date_utils.py @ 7718bd65] — прецедент `get_moscow_safe_end_date` (вчера по МСК через `pytz`; мы — фикс-офсет stdlib).
- [Source: G:/git/directaiq/scripts/8x_metrica_logs_api/p81_load_logs.py:326-343 @ 7718bd65] — прецедент clamp `_process_date_range` (клампит только `end_date`, на `start>safe_end` тихий «0 файлов»; мы — fail-loud, AC #5).
- [Memory: feedback-decide-and-apply] — решения о гранулярности доков/выборе подхода принимаю сам и применяю до конца; реальные развилки выношу Шефу.
- [Memory: dotenv-usecwd-gotcha] — соседний `dates.py` потребитель `paths.py` (2.1); не путать зоны ответственности.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Opus 4.7, 1M context)

### Debug Log References

- `uv run pytest tests/test_dates.py -q` → 25 passed.
- `uv run mypy scripts` → Success: no issues found in 10 source files (strict).
- `uv run pytest -q` (полный набор) → 87 passed, 1 deselected (live отсеян `addopts="-m 'not live'"`); регрессий 1.1/1.2/1.3 нет.
- `git diff --name-only -- uv.lock pyproject.toml` → пусто (новых зависимостей нет, модуль stdlib-only).

### Completion Notes List

- Реализован `scripts/utils/dates.py` — крошечный stdlib-примитив (Tasks 1–4): `from __future__ import annotations` первой строкой, русский модульный docstring, английские идентификаторы, `logging.getLogger(__name__)`, `__all__`.
- **Таймзона МСК = фиксированный офсет `timezone(timedelta(hours=3))`** (AC #4), НЕ `zoneinfo`/`pytz`/`tzdata` — ноль зависимостей, кросс-платформенно (NFR-2/6). Решение зафиксировано комментарием «почему» прямо у константы. Тест по `ast` (`test_no_timezone_dependencies_imported`) гарантирует отсутствие запрещённых таймзонных импортов (приём из `test_env_reader.py`, не подстрока).
- **Шов времени `_now_utc()`** — единственная точка к стене часов; `moscow_today`/`moscow_yesterday` строятся от UTC-инстанта через `.astimezone(MSK)`, поэтому корректны независимо от локальной зоны. Тест AC #4 монкейпатчит шов инстантом 24-го 22:30 UTC (= 25-го 01:30 МСК) и доказывает, что сутки считаются от МСК, а не UTC.
- **Строгий парсинг (AC #3, #6):** `parse_date` = guard `\d{4}-\d{2}-\d{2}` + `date.fromisoformat`. Guard режет `20260524`/`2026-W21-1`, которые голый `fromisoformat` в 3.13 ПРИНЯЛ БЫ; `fromisoformat` добивает невалидные календарно (`2026-13-01`/`2026-05-40`/`0000-00-00`). Сообщение несёт `{value!r}`. `format_date` = `date.isoformat()` (всегда zero-padded `YYYY-MM-DD`).
- **Ядро `clamp_date_range` (AC #1, #2, #5):** `date2 > вчера` → clamp + INFO-лог; `date2 ≤ вчера` (вкл. границу) → без изменений и без лога (нет off-by-one); будущий `date1` / инвертированный диапазон → `ValueError` ДО возврата (fail-loud, осознанное отличие от directaiq «0 файлов»). Детерминизм через keyword-only `today_msk` — без отдельного мокабельного класса.
- **`docs/dates.md` НЕ заведён осознанно:** project-context относит `dates` к «мелким хелперам, описываемым внутри родственной спеки (`cli.md`/`ingestion.md`), а не отдельным файлом»; родственная спека ещё не создана. Контракт несёт подробный модульный docstring + эта история. Применил правило project-context по умолчанию.
- **Live-smoke не заводился** — `dates.py` не ходит во внешний API (правило «обязателен live-smoke» относится к компонентам, дёргающим Logs API).

### File List

- `scripts/utils/dates.py` — **новый**. Примитивы границы дат: `MSK` (фикс-офсет UTC+3), `moscow_today`/`moscow_yesterday` (через шов `_now_utc`), `parse_date`/`format_date` (строгий `YYYY-MM-DD`), `clamp_date_range` (clamp + fail-loud валидация).
- `tests/test_dates.py` — **новый**. 25 offline-тестов: AC #1–#6, анти-зависимость по `ast`, одинокий день == вчера; детерминизм через `today_msk` и монкейпатч `_now_utc`.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — **изменён**. Статус 1-4: `ready-for-dev → in-progress → review`.

### Change Log

- 2026-05-24 — Реализована история 1.4: `scripts/utils/dates.py` (stdlib-примитив clamp «вчера по МСК» + строгий `YYYY-MM-DD`) и `tests/test_dates.py` (25 тестов). mypy strict + полный pytest зелёные; `uv.lock` без изменений. Status → review.

## Review Findings

_Code review 2026-05-24 (bmad-code-review; 3 слоя: Blind Hunter / Edge Case Hunter / Acceptance Auditor). Гейты подтверждены прогоном: `uv run pytest tests/test_dates.py` → 25 passed; `uv run mypy scripts` → Success (10 files, strict). Все 6 AC выполнены, ядро корректно. Итог: 0 decision-needed, 4 patch (тест-покрытие), 0 defer, 9 dismissed._

### Patch (код корректен — не хватает тестов)

- [x] [Review][Patch] Непокрыт ключевой путь — clamp-индуцированная инверсия → ValueError [tests/test_dates.py; код scripts/utils/dates.py:108-115] — заявленное отличие от directaiq (`date2` в будущем клампится НИЖЕ `date1` → fail-loud) не покрыто ни одним тестом: в обоих AC#5-тестах `date2 ≤ ceiling`, т.е. clamp не срабатывает и инверсию даёт уже-будущий `date1`, а не сам clamp. Добавить: (а) `date1` валиден, `date2` в будущем клампится ниже `date1` → `ValueError`; (б) одинокий «сегодня» `clamp_date_range(d, d, today_msk=d)` → `ValueError` (граница `ceiling+1`).
- [x] [Review][Patch] Непокрыт прод-путь `clamp_date_range` без `today_msk` [tests/test_dates.py; код scripts/utils/dates.py:104] — все 7 clamp-тестов передают `today_msk` явно; ветка default (`moscow_today()`) внутри `clamp_date_range` не исполняется ни разу. Добавить тест: monkeypatch `_now_utc`, вызвать `clamp_date_range` БЕЗ `today_msk` → проверить clamp от «вчера по МСК».
- [x] [Review][Patch] Непокрыта граница `date1 == clamped2` после активного clamp [tests/test_dates.py; код scripts/utils/dates.py:111] — равенство (нижняя кромка inversion-guard `>`) проверено только без clamp (`test_single_valid_day_equals_yesterday`). Добавить: `date2` в будущем клампится ровно к `date1` → возврат `(date1, date1)` без ошибки.
- [x] [Review][Patch] Тесты на календарно-невалидные даты не различают ветку сообщения [tests/test_dates.py; код scripts/utils/dates.py:79-82] — `2026-13-01`/`2026-05-40`/`0000-00-00` проверяются лишь на факт `ValueError`; ветка «Невалидная календарная дата» не отделена от format-guard. Добавить `match="Невалидная календарная"` для одного такого кейса.

### Dismissed (зафиксировано, патч не нужен)

- `parse_date` принимает дату с обрамляющими пробелами (`.strip()`) — **спец-мандат** Task 3 (`re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip())`); намеренное поведение, не ослабление контракта.
- guard `\d` матчит Unicode-цифры — fail-loud сохраняется (`fromisoformat` добивает), регекс задан спекой буквально; экзотический вход.
- `MSK.utcoffset(None).total_seconds()` в тесте — потенциальная strict-ошибка, но CI гоняет `mypy scripts`, не `tests/`; рантайм безопасен (фикс-офсет ≠ None).
- `format_date(datetime)` дал бы время — тип-контракт `date` это документирует; защищаться от подкласса = переусложнение (NFR-6).
- ast-тест ловит только статические import-узлы — намеренный приём спеки (против ложного срабатывания по подстроке); динамических импортов модуль не делает.
- docstring `moscow_today` приписывает независимость от зоны `.astimezone` — формулировка-нит; код корректен.
- около-полуночный МСК-кейс протестирован в одну сторону — линейная арифметика офсета, одного направления достаточно.
- расхождение «25 тестов» в Dev Agent Record — **проверено**: ровно 25 прогонов (14 функций + 11 параметризаций); цифра точна.
- `_ISO_DATE_RE` вынесен в `re.compile` вместо инлайн-`fullmatch` — косметика, поведенчески эквивалентно и чище.
