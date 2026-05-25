# Story 2.9: Команда `gdau-logs update`

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита (агент),
I want высокоуровневую команду обновления за диапазон (`gdau-logs update`),
so that одной командой безопасно довести/обновить данные игры (оба источника), с понятными кодами возврата и идемпотентным повтором.

## Acceptance Criteria

1. **Given** `gdau-logs update --date1 --date2 --source {visits|hits|both}`, **When** выполняется, **Then** оркеструет диапазон через диапазонный слой p81 (2.8 `ingest_range`/`_select_days_to_load`) поверх цикла дня (2.7 `load_day`) с инкрементом + hot-window; результат — данные в сырье и рабочем слое (view'ы отражают сразу).
2. **Given** успех, **When** завершён, **Then** код `0`; любой fail → ненулевой код + понятное сообщение (без трейсбека).
3. **Given** повтор той же команды, **When** данные уже загружены, **Then** идемпотентно — база не дублируется/не ломается (SM-2): загруженные дни пропускаются (инкремент, FR-9), hot-window перезаливается перезаписью одной партиции без `DROP` (FR-10/FR-11).
4. **Given** `update` добавлен к CLI (1.6 `logs_api_cli.py`), **When** смотрим `gdau-logs --help` / `gdau-logs update --help`, **Then** соседствует с подкомандами жизненного цикла (`create`/`evaluate`/`status`/`download`/`clean`/`list`/`info`), неинтерактивно.
5. **Given** `--source` не указан, **When** разбираются аргументы, **Then** поведение явное: **задокументированный `default=both`** (грузит и `visits`, и `hits`) — без молчаливой загрузки одного источника. _[edge-case: неуказанный source]_
6. **Given** прогон по нескольким источникам, где один успешен, другой упал, **When** агрегируется итог, **Then** **оба источника опрашиваются** (сбой одного не отменяет попытку другого), а итоговый код **ненулевой, если упал хотя бы один** источник (частичный сбой не маскируется успехом). _[edge-case: смешанный результат visits/hits]_
7. **Given** прерывание (SIGINT/SIGTERM) посреди обновления, **When** процесс завершается, **Then** `.writer.lock` освобождается (SIGINT → `KeyboardInterrupt` → `finally` контекст-менеджера внутри `ingest_range`; SIGTERM/смерть процесса → ядро освобождает advisory-lock 2.5), а частично загруженный диапазон до-грузится при повторе через инкремент (resumability через per-day-коммит + incremental-skip). _[edge-case: прерывание / resumability]_
8. **Given** большой диапазон, упирающийся в дневную квоту Logs API (≤5000 req/day), **When** квота исчерпана (или любой день прерван по сети), **Then** остановка с понятным **resumable-сообщением** (что уже загружено, что повторный запуск пропустит уже-загруженные дни, что докрутить позже), а не невнятный сбой. _[edge-case: исчерпание дневной квоты]_

---

## Главные риски / решения (читать ДО кода)

> Эта история — **тонкая поверхность/UX поверх готового диапазонного слоя**. Вся тяжёлая механика (цикл дня, запись, сверка, мета, лок, view, инкремент, hot-window, clamp) уже реализована в 2.7/2.8 — 2.9 их **зовёт и оборачивает в команду**, владея только: argparse-поверхностью, списком источников, агрегацией частичного сбоя, кодами возврата и resumable-сообщениями. **Не** реализовывать заново ни один шаг приёма; **p81 не трогаем** (см. РЕШЕНИЕ).

### ✅ РЕШЕНИЕ (зафиксировано): scope `.writer.lock` для двух источников — **вариант B2 (реюз `ingest_range` per source)**

**Корень.** 2.8 `ingest_range(source, …)` берёт `.writer.lock` **внутри себя** (один источник = один захват), а `writer_lock` **не реентерабелен** (2.5). 2.9 должна прогнать оба источника — отсюда был выбор: один лок на оба (B) или захват per source (B2).

**Зафиксировано B2:** `_handle_update` (тонкий CLI-handler) зовёт `ingest_range(source, …)` по каждому источнику **последовательно** (лок берётся/освобождается per source), ловит исключение на источник, агрегирует код возврата. **p81 НЕ меняется** — `ingest_range` зовётся verbatim.

**Почему B2 (учитывая цели проекта):**
- **Целостность базы (NFR №1) — одинакова у B и B2.** Каждая запись дня атомарна (temp→rename, 2.2), под локом (2.5), per-day идемпотентна (FR-10), сверка строк — жёсткий fail (2.3), `reconcile` — под локом (2.4). Инвариант «один писатель» (FR-15) держится в обоих. Разница лишь в «атомарности всего прогона» — украшение, не защита от порчи. Правило project-context «более строгий вариант **вокруг целостности**» здесь нейтрально: строже (B) ≠ безопаснее.
- **«Один оператор — агент» обнуляет выгоду B.** Единственный плюс B — закрыть окно вклинивания между visits и hits; второго параллельного писателя в модели нет. Гипотетический второй писатель в B2 → второй источник падает fail-fast (`WriterLockHeldError` → per-source error → ненулевой код + resumable), а не портит данные. Деградация чистая.
- **Простота — топ-принцип проекта (CLAUDE.md «усложнять только по реальной потребности»).** Реальной потребности в one-lock нет. B2: ноль нового кода в p81, реюз `ingest_range` дословно, логика в тонком handler — дух «склейки тонких примитивов».
- **Стабильность.** 2.8 в `review` (21 тест зелёный). B потребовал бы рефакторить её свежий код (вынос `_ingest_range_locked`) → риск регресса + повторное ревью почти готовой истории. B2 не трогает 2.8 — стабильнее.
- «Оба под одним локом» из docstring `ingest_range` 2.8 — аспирация без реальной отдачи в однооператорной модели; эпик-AC #7 требует лишь release лока в `finally` (выполняется обоими).

### Риски (решены в дизайне ниже)

- **Риск №1 — digit-префикс импорта.** `_handle_update` зовёт `ingest_range`/`DEFAULT_HOT_WINDOW_DAYS`/`IngestRangeResult` из `scripts/8x_metrica_logs_api/p81_load_logs.py`; каталог начинается с цифры → `import scripts.8x_…` как statement = `SyntaxError`. Импорт **только** через `importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")` (образец 2.7/2.8; CLI сейчас импортирует лишь из `scripts.utils.*` — добавить importlib-импорт **в handler**, не в шапку модуля).
- **Риск №2 — частичный сбой не маскировать (AC #6).** «Опросить оба источника» ≠ «проглотить ошибку». Сбой источника **фиксируется** (текст + ненулевой код), просто не отменяет попытку второго. Per-source `except` ловит `(ValueError, RuntimeError, OSError)` — НО **не** `KeyboardInterrupt`/`SystemExit` (они пробрасываются → лок `ingest_range` освобождается `finally`, AC #7).
- **Риск №3 — fail-fast ДО прогона vs per-source mid-range.** Невалидный `--source` (отсеян `choices` argparse) и пустой список источников невозможны; но невалидная дата/инверсия диапазона/`hot_window<0`/нет кредов/лок занят — это fail-fast **внутри первого же `ingest_range`** (валидация/clamp/lock до сети) → как `ValueError`/`WriterLockHeldError`. **Эти ошибки тоже ловятся per-source `except`** и дают ненулевой код через агрегацию (а не трейсбек). Не путать с mid-range-сбоем дня (`RowCountMismatchError`, терминальный статус API, исчерпание poll/квоты) — он тоже per-source outcome.
- **Риск №4 — exit-код только в одном месте.** `update` — единственная команда, где код возврата считается агрегацией. `_handle_update` сам печатает сводку и при наличии хоть одного сбоя поднимает `SystemExit(1)` (AC #2/#6). Прочие подкоманды не трогаем — их код по-прежнему через `main()`.
- **Риск №5 — resumable-сообщение, а не «партиальный успех» (AC #8).** Прерванный/упавший по квоте прогон оставляет **закоммиченные дни** (per-day `mark_loaded`). Сообщение о сбое обязано подсказать: «повторите ту же команду — загруженные дни пропустятся (инкремент); при исчерпании дневной квоты Logs API ≤5000/сут докрутите остаток позже». Детектировать именно «квоту» из текста ошибки клиента **не** пытаемся (хрупко) — сообщение общее, покрывает квоту и любой mid-range-сбой.
- **Риск №6 — KeyboardInterrupt → чистый выход (AC #7).** По умолчанию Python печатает трейсбек + exit 130. Добавить в `main()` ветку `except KeyboardInterrupt` → понятное сообщение «прервано, лок освобождён, повторите для до-грузки» + `SystemExit(130)` (ненулевой). Лок уже освобождён `finally` контекст-менеджера `writer_lock` внутри `ingest_range` — отдельный сигнальный хендлер **не** заводим (SIGTERM полагается на авто-release advisory-lock ядром, 2.5 вариант A; signal-хендлеры — лишняя сложность, NFR-6).
- **Риск №7 — наблюдаемость/прогресс (LESSONS Сложность 4).** Прод-поверхность обязана печатать прогресс по фазам. Это **наследуется бесплатно**: `load_day` логирует INFO по фазам (заказ/poll/скачано/загружен/очищено), `ingest_range` — INFO по диапазону (дней к загрузке/пропущено/hot-window), а `main()` уже ставит `logging.basicConfig(level=INFO)`. 2.9 **дополнительно** печатает финальную человекочитаемую сводку по источнику (загружено/пропущено/строк). Не глушить логи и не буферизовать вывод.

---

## Tasks / Subtasks

- [x] **Task 1 — Подкоманда `update` в парсере + диспетч + docstring (AC #1/#4/#5)**
  - [x] В `_create_parser` добавить subparser **`update`** рядом с lifecycle-командами (AC #4): `--date1` (required, `YYYY-MM-DD`), `--date2` (required, help: «клампится на «вчера по МСК»»), `--source` (`choices=["visits","hits","both"]`, **`default="both"`**, help проговаривает дефолт — AC #5), `--hot-window` (`type=int`, `default=None`, help: «размер hot-window, дней; 0 — выключить; по умолчанию 3»). Неинтерактивно.
  - [x] В `_dispatch` добавить ветку `if command == "update": return self._handle_update(args)`.
  - [x] Обновить **module-docstring** `logs_api_cli.py` и комментарий «Граница скоупа»: `update` теперь **здесь** (убрать формулировку «update — отдельная работа Эпика 2, не этот компонент»; оставить, что тяжёлая механика — в p81/utils, CLI лишь оркеструет поверхность и владеет кодами возврата).
- [x] **Task 2 — Оркестрация `_handle_update`: цикл по источникам, агрегация, коды (AC #1/#2/#6/#7/#8)**
  - [x] Добавить **`_handle_update(self, args) -> list[_UpdateOutcome]`** (локальный `NamedTuple` `_UpdateOutcome` — типизировано строго, без `Any`-дыр: поля копируются сразу за `Any`-границей importlib):
    - [x] `p81 = importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")` (риск №1, **в теле handler**).
    - [x] `sources = ["visits", "hits"] if args.source == "both" else [args.source]`.
    - [x] `hot_window: int = p81.DEFAULT_HOT_WINDOW_DAYS if args.hot_window is None else args.hot_window` (пиннинг `: int` уводит `Any` в тип).
    - [x] Цикл per source: `try: result = p81.ingest_range(source, args.date1, args.date2, hot_window_days=hot_window); record outcome` → `except (ValueError, RuntimeError, OSError) as exc: logger.error("Источник %s не доведён: %s", source, exc); record error`. **`KeyboardInterrupt`/`SystemExit` НЕ ловить** (риск №2/№6 — проброс к `main`; лок `ingest_range` снят `finally`).
  - [x] Печать сводки (человекочитаемо, как остальные handler'ы): успех `f"{source}: загружено … дн., пропущено … дн., строк …"`; сбой `f"{source}: ОШИБКА — {error}"`.
  - [x] **Агрегация кода (риск №4, AC #2/#6):** если есть хоть один источник с ошибкой → напечатать resumable-подсказку (риск №5/AC #8) и `raise SystemExit(1)`. Иначе вернуть список outcomes (неявный exit 0).
  - [x] В `main()` добавить **`except KeyboardInterrupt`** (риск №6, AC #7): `logger.error("Прервано оператором — .writer.lock освобождён; повторите ту же команду …")` + `raise SystemExit(130) from None`. Существующий `except (ValueError, RuntimeError, FileExistsError, OSError)` покрывает `WriterLockHeldError`/`RowCountMismatchError`, но в `update` они ловятся **раньше**, per-source (риск №3).
- [x] **Task 3 — Документация (часть DoD)**
  - [x] `docs/cli.md`: добавить `update` в список команд («довести/обновить данные игры за диапазон одной командой; инкремент + перезалив hot-window; коды возврата; оба источника по умолчанию»); обновить раздел «Границы» (полный приём за диапазон `update` теперь реализован здесь как тонкая поверхность).
  - [x] `docs/ingestion.md`: новый раздел «Команда обновления» вместо forward-ссылок «история 2.9»; человеческим языком: одна команда доводит диапазон по источникам (каждый — под своим замком, последовательно), пропускает уже загруженное, перезаливает свежее окно, возвращает ненулевой код при сбое любого источника, повтор безопасен.
- [x] **Task 4 — Offline-тесты (`tests/test_logs_api_cli.py`, дополнение)**
  - [x] **Парсинг (AC #4/#5):** `update` среди подкоманд (расширен `test_help_lists_all_subcommands`); `--source` отсутствует → `default="both"`; `--source both/visits/hits`; `--hot-window` опционален (None → дефолт `DEFAULT_HOT_WINDOW_DAYS`, явное значение пробрасывается); даты required; невалидный source → exit 2.
  - [x] **Диспетч + сводка (AC #1):** мок `p81.ingest_range` (`monkeypatch.setattr` на importlib-загруженный кэшированный модуль) возвращает `IngestRangeResult` → печатается сводка, код 0, `ingest_range` вызван **по каждому источнику** (`both` → два вызова с верными `source`/`date1`/`date2`/`hot_window_days`).
  - [x] **Агрегация кода (AC #2/#6):** все ок → exit 0; `both`, где `ingest_range` бросает для visits и ок для hits → печать обоих + `SystemExit(1)` + resumable-подсказка; оба бросают → `SystemExit(1)`. **Второй источник опрашивается даже после сбоя первого** (`sources_called`).
  - [x] **Fail-fast как per-source (риск №3):** `ingest_range` бросает `WriterLockHeldError`/`ValueError` → ловится per-source → ненулевой код через агрегацию, без трейсбека.
  - [x] **KeyboardInterrupt (AC #7):** `ingest_range` (мок) бросает `KeyboardInterrupt` → НЕ выловлен per-source → `main()` ловит → `SystemExit(130)` + сообщение (hits не опрошен — цикл оборван).
  - [x] **Импорт через importlib:** dispatch отрабатывает на реальном модуле p81 с мок-`ingest_range` (подтверждает, что digit-префикс грузится строкой без `SyntaxError`).
- [x] **Task 5 — Live-smoke (`tests/test_logs_api_cli_live.py`, дополнение; opt-in `@pytest.mark.live`)**
  - [x] End-to-end `update` за **узкое окно (1 день)** через `main()` против РЕАЛЬНОГО Logs API: оба источника — реально доехали в сырьё + view'ы. Креды/корень хранилища из окружения; нет → `pytest.skip` (не ложный красный).
  - [x] **Критерий live-DoD (LESSONS Сложность 1):** закрыт **зелёным end-to-end** (2026-05-24: visits 1893 стр., hits 79472 стр.; партиции созданы, `load_state` loaded — сверка сошлась, код 0). Узкое окно; докстринг проговаривает side-effects: запись в реальное хранилище под `.writer.lock` + трата квоты + осиротевшие log-запросы при обрыве (LESSONS Сложность 6).
  - [x] **Идемпотентность вживую (AC #3/SM-2):** повторный `update` того же дня (visits, в hot-window) → перезалив, код 0, число строк стабильно (1893 → 1893) — база не сломана.
- [x] **Гейты перед сдачей**
  - [x] `uv run mypy scripts` → зелено (strict, win32 + `--platform linux`, 19 файлов; локальный `_UpdateOutcome`, `hot_window: int`, `sources: list[str]`; без `Any`-дыр).
  - [x] `uv run pytest` (offline) → зелено (331 passed, 8 live deselected); маркер `live` НЕ гоняется в стандартном прогоне.
  - [x] `uv run pytest -m live` → зелёный end-to-end (4 теста файла: 2 evaluate + 2 update); освежение фикстур не потребовалось (контракт сошёлся).
  - [x] `uv.lock` не менялся (всё в стеке/stdlib — `argparse`/`importlib`/`datetime`/`typing` + готовые примитивы). Чек-лист «Definition of Done» пройден.

## Dev Notes

### Рекомендуемый контракт 2.9 (вариант B2; p81 НЕ трогаем)

| Имя | Сигнатура | Смысл | Где |
|---|---|---|---|
| `_handle_update` | `(self, args) -> list[...]` | тонкий CLI-handler: importlib→цикл `ingest_range` per source→per-source `except`→печать сводки→`SystemExit(1)` если есть сбой | `logs_api_cli.py` (новое) |
| outcome-запись | `tuple[str, IngestRangeResult \| None, str \| None]` (или локальный `NamedTuple`) | итог по источнику для агрегации кода (успех/сбой); **локально в CLI**, не в p81 | `logs_api_cli.py` |

**Скелет `_handle_update` (вариант B2):**
```python
p81 = importlib.import_module("scripts.8x_metrica_logs_api.p81_load_logs")   # риск №1 (digit-префикс)
sources = ["visits", "hits"] if args.source == "both" else [args.source]
hot_window = p81.DEFAULT_HOT_WINDOW_DAYS if args.hot_window is None else args.hot_window
outcomes: list[tuple[str, object | None, str | None]] = []
for source in sources:
    try:
        result = p81.ingest_range(source, args.date1, args.date2, hot_window_days=hot_window)
        outcomes.append((source, result, None))
    except (ValueError, RuntimeError, OSError) as exc:        # KeyboardInterrupt/SystemExit — НЕ ловим (риск №2/№6)
        logger.error("Источник %s не доведён: %s", source, exc)
        outcomes.append((source, None, str(exc)))
for source, result, error in outcomes:
    if error is None:
        print(f"{source}: загружено {len(result.loaded_dates)} дн., "
              f"пропущено {len(result.skipped_dates)} дн., строк {result.total_rows}")
    else:
        print(f"{source}: ОШИБКА — {error}")
if any(error is not None for _, _, error in outcomes):       # риск №4 (AC #2/#6)
    print("Часть источников не доведена. Повторите ту же команду — уже загруженные дни "
          "пропускаются (инкремент); при исчерпании дневной квоты Logs API (≤5000 запр./сут) "
          "докрутите остаток позже.")                        # риск №5 (AC #8)
    raise SystemExit(1)
return outcomes
```

### Карта примитивов, которые зовём (сигнатуры сверены с фактическим кодом 2026-05-25)

- `p81.ingest_range(source, date1, date2, *, hot_window_days=DEFAULT_HOT_WINDOW_DAYS, catalog=None, poll_interval_s=…, poll_timeout_s=…, max_consecutive_errors=…, sleep=time.sleep) -> IngestRangeResult` (`p81_load_logs.py`, 2.8) — **зовём как есть, per source**. Внутри: валидация `source`/`N`/clamp дат **до** лока, `with writer_lock(): with conn: client; ensure_load_state_table; create_views; reconcile; _select_days_to_load; цикл load_day`. Лок берётся **внутри** → 2.9 зовёт последовательно (B2). Сбой дня → проброс (ловим per-source).
- `p81.IngestRangeResult(source: str, loaded_dates: list[str], skipped_dates: list[str], total_rows: int)` — `NamedTuple`, итог одного источника (используем `loaded_dates`/`skipped_dates`/`total_rows` в сводке).
- `p81.DEFAULT_HOT_WINDOW_DAYS = 3` (2.8) — дефолт hot-window; экспонируется флагом `--hot-window` (реализация FR-11 «N конфигурируемый» на поверхности оператора). `N<0` → `ValueError` из `ingest_range` до лока → per-source error.
- **CLI-форма (1.6):** `LogsApiCLI._create_parser()` (`subparsers(dest="command", required=True)`), per-command `_handle_*` (сам печатает результат), `_dispatch`, `main()` с `logging.basicConfig(INFO)` и `except (ValueError, RuntimeError, FileExistsError, OSError) → SystemExit(1)`. `WriterLockHeldError(WriterLockError(RuntimeError))` и `RowCountMismatchError(RuntimeError)` — оба `RuntimeError`-наследники (в `update` ловятся per-source раньше `main`).
- **НЕ зовём напрямую:** `load_day`/`ingest_day` (их зовёт `ingest_range`); `reconcile`/`writer_lock`/`DatabaseManager`/`MetricaClient`/`create_views`/`ensure_load_state_table` (всё внутри `ingest_range`). 2.9 их не импортирует.

### Паттерны (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` первой строкой (есть); русские docstrings/комментарии, английские идентификаторы; type hints везде, `mypy --strict`, без `Any`-дыр; абсолютные импорты от корня пакета; `logger = logging.getLogger(__name__)`.
- **Тонкий CLI:** код возврата/печать/агрегация частичного сбоя — в `_handle_update`; вся оркестрация/лок/циклы — внутри `ingest_range` (p81). Лок/оркестрацию в CLI-слой не тащить (граница `docs/cli.md`).
- **Импорт p81 — только `importlib.import_module`** (digit-префикс; образец 2.7/2.8). В шапку `logs_api_cli.py` p81 НЕ добавлять как statement.
- Fail-loud наследуется из `ingest_range`: невалидная дата/инверсия/`N<0`/нет кредов/лок занят → `ValueError`/`WriterLockHeldError`; `RowCountMismatchError` (2.3) из дня — всё это per-source `error` (фиксируем + ненулевой код, не глушим как успех).
- Анти-зависимость: 2.9 не вводит новых импортов тяжёлого стека; `logs_api_cli.py` использует только stdlib (`argparse`/`logging`/`importlib`/`pathlib`) + `scripts.*`. ast-анти-зависимость p81 не затрагивается (p81 не меняется).
- **Не тащить** инфру directaiq (`BaseScript`/`config_manager`/`AuthManager`/параллель), машинные `--format` (решение Шефа: вывод человекочитаемый), сигнальные хендлеры (SIGTERM → авто-release advisory-lock ядром, 2.5).

### Уроки live-прогона 2.7 (LESSONS.md — учтены)

- **Сложность 1 (live-DoD):** «сделан» ≠ «зелёный». Закрывать Task 5 только зелёным end-to-end, не фактом запуска.
- **Сложность 4 (наблюдаемость):** прод-`update` печатает прогресс по фазам — **наследуется** из INFO-логов `load_day`/`ingest_range` + `basicConfig(INFO)`; плюс финальная сводка. Live гонять с `-s --log-cli-level=INFO`.
- **Сложность 5 (preflight):** лёгкий `evaluate` до дорогого цикла — **вне скоупа 2.9** (у CLI уже есть отдельная `evaluate`-подкоманда 1.6; авто-preflight в `update` осознанно не добавляем, простота-первой). Возможный follow-up, не задача.
- **Сложность 6 (side-effects):** live `update` пишет в реальное хранилище под `.writer.lock` + тратит квоту + прерванные прогоны оставляют осиротевшие log-запросы — проговорить в докстринге смоука.

### Границы 2.9 (не выходить)

- Трогаем: `scripts/tools/logs_api_cli.py` (подкоманда `update` + `_handle_update` + docstring/`main`), `tests/test_logs_api_cli.py`(+`_live`), `docs/cli.md`, `docs/ingestion.md`. **`scripts/8x_metrica_logs_api/p81_load_logs.py` НЕ трогаем** (вариант B2 — реюз `ingest_range` verbatim).
- **Не** реализуем заново: цикл дня/poll/download/parse (2.7), запись (2.2), сверку (2.3), мету/`reconcile` (2.4), лок (2.5), view (2.6), инкремент/hot-window/`_select_days_to_load`/clamp (2.8/1.4), клиент/каталог/креды (1.x) — **зовём готовое через `ingest_range`**.
- MCP-чтение, конкуренция читатель↔писатель на Windows `os.replace`, авто-preflight/`evaluate` в `update`, «один лок на оба источника» — **не здесь** (3.1 / осознанный non-goal).
- `--format` json/csv для `update` не вводим (вывод человекочитаемый — решение Шефа по 1.6).

### Project Structure Notes

- Entry-point `gdau-logs = scripts.tools.logs_api_cli:main` (`pyproject.toml`); архитектура фиксирует `logs_api_cli.py # argparse: update|create|status|download|clean|evaluate|list|info` (architecture.md:460) — `update` штатно живёт здесь.
- Каталог `scripts/8x_metrica_logs_api/` без `__init__.py` (неявный namespace, digit-префикс) → `importlib.import_module` (как 2.7/2.8). Имена snake_case; типы обязательны (mypy strict).
- Тесты: `test_logs_api_cli.py`/`_live` (есть, 1.6 — дополняем). `conftest.py` нет — `tmp_path`/`monkeypatch`/`importlib` напрямую; маркер `live` + `addopts = "-m 'not live'"` (1.3). Offline-тесты `update` — только моки `ingest_range` (без сети/лока/БД).
- `gdau.duckdb`/`*.parquet`/`.writer.lock`/`*.tsv` — артефакты хранилища (`GDAU_DATA_ROOT`), в dev-репо не создаются/не коммитятся.
- `uv.lock` не трогаем; не переводить на src-layout, не переименовывать `scripts` (hatchling `packages=["scripts"]`).

### Зависимость от 2.8 (статус на момент создания истории)

- 2.9 строится **поверх** 2.8 (`ingest_range`/`IngestRangeResult`/`DEFAULT_HOT_WINDOW_DAYS` в `p81_load_logs.py`). На момент создания этой истории 2.8 — **`review`** (реализация приземлена параллельным dev-story: `p81_load_logs.py` содержит `ingest_range` с локом внутри + `__all__` обновлён; `tests/test_hot_window.py` — 21 тест; гейты зелёные [mypy strict, pytest 317/5]). Сигнатуры в этой истории **сверены с фактическим кодом**, не только со спекой 2.8.
- **2.8 должна приземлиться (merge в `main`/статус `done`) ДО реализации 2.9.** Вариант B2 **не трогает** код 2.8 (реюз `ingest_range` дословно) → нет риска регресса её тестов; нужна лишь её публичная поверхность (`ingest_range`/`IngestRangeResult`/`DEFAULT_HOT_WINDOW_DAYS`).
- Новая история → новая ветка от `main` (напр. `story/2.9-update-command`); секреты/данные не коммитятся.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.9] (строки 354-369) — 8 AC, edge-cases (неуказанный source, смешанный visits/hits, прерывание/resumability, исчерпание квоты).
- [Source: _bmad-output/planning-artifacts/prds/.../prd.md#FR-9..FR-11] (строки 167-195) — инкремент по дню / идемпотентный перезалив без DROP / hot-window N=3 конфигурируемый; [#SM-2] (строка 346) — повтор идемпотентен, база не ломается; [#Acceptance] (строки 94-96) — команда доводит диапазон или ненулевой код с причиной, после успеха `clean`.
- [Source: _bmad-output/planning-artifacts/architecture.md] — :460 (CLI-подкоманды incl `update`), :506 (entry-point), :536-538 (поток приёма `gdau-logs update`→p81→lock→client→parquet→сверка→rename→load_state, hot-window), :285-287 (CLI argparse: update + lifecycle + info).
- [Source: scripts/8x_metrica_logs_api/p81_load_logs.py] — `__all__` (DEFAULT_HOT_WINDOW_DAYS/load_day/ingest_day/ingest_range/IngestRangeResult), `ingest_range` (лок внутри; clamp/N-валидация до лока; reconcile→select→load_day).
- [Source: scripts/tools/logs_api_cli.py] — `_create_parser`/`_handle_*`/`_dispatch`/`main` (форма, `subparsers(required=True)`, `except (ValueError, RuntimeError, FileExistsError, OSError)→SystemExit(1)`, `basicConfig(INFO)`); module-docstring «update — Epic 2, не здесь» (обновить).
- [Source: scripts/utils/writer_lock.py:63] — `WriterLockHeldError(WriterLockError(RuntimeError))` (не реентерабелен → корень решения B2; `RuntimeError`-наследник). [scripts/utils/row_check.py:55] — `RowCountMismatchError(RuntimeError)`.
- [Source: _bmad-output/implementation-artifacts/2-8-…-hot-window.md] (строки 100-119, 157-162) — контракт `ingest_range`, намерение «2.9 — оба источника под одним локом» (отклонено в пользу B2, см. РЕШЕНИЕ).
- [Source: LESSONS.md] — Сложность 1 (live-DoD = зелёный end-to-end), 4 (прогресс по фазам в прод-CLI 2.9), 5 (preflight/`evaluate` — follow-up, не задача), 6 (live пишет в реальное хранилище + квота + осиротевшие запросы).
- [Source: _bmad-output/project-context.md] — каналы (CLI=действия/запись), коды возврата (успех 0 / fail non-zero), fail-loud, лок одного писателя, importlib для digit-префикса, не тащить инфру directaiq, docs/<component>.md как часть DoD.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Claude Opus 4.7, 1M context) — dev-story workflow.

### Debug Log References

- Эмпирическая проверка mypy: атрибуты модуля из `importlib.import_module(...)` видятся как
  `Any` (revealed type `Any`). Следствие дизайна: `p81.ingest_range(...)`/
  `p81.DEFAULT_HOT_WINDOW_DAYS` пиннятся в конкретные типы **сразу** за `Any`-границей
  (`hot_window: int`, поля `IngestRangeResult` → локальный `_UpdateOutcome`), чтобы не было
  `Any`-дыр (project-context). `IngestRangeResult` нельзя импортировать в шапку (digit-префикс
  `8x_…` → `SyntaxError` на `import`-statement, и под `TYPE_CHECKING` тоже).
- RED→GREEN: 10 новых offline-тестов падали до реализации (`update` — invalid choice), затем
  все зелёные.
- Live-прогон (`GDAU_DATA_ROOT=G:/gdau-smoke uv run pytest -m live`): end-to-end `update`
  visits 1893 / hits 79472 строки за 2026-05-24, идемпотентный повтор visits 1893 → 1893; оба
  теста PASSED (~4:45 на 4 цикла create→poll→download→write→verify→mark_loaded→clean).

### Completion Notes List

- **Вариант B2 соблюдён дословно:** `scripts/8x_metrica_logs_api/p81_load_logs.py` **НЕ
  тронут** (отсутствует в `git status`). `_handle_update` зовёт `ingest_range(source, …)`
  verbatim по каждому источнику — лок берётся **внутри** `ingest_range` per source
  (последовательно). Один лок на оба источника осознанно не вводился (в модели «один
  оператор» выгоды нет, простота+стабильность перевешивают).
- **Все 8 AC закрыты:** AC #1 (оркестрация диапазона по источникам через `ingest_range`, view
  отражают — подтверждено live); #2 (успех→0, сбой→ненулевой без трейсбека); #3 (идемпотентность
  — live повтор 1893→1893, наследуется из 2.7/2.8); #4 (`update` в `--help` рядом с lifecycle);
  #5 (`default="both"` задокументирован); #6 (оба источника опрашиваются, ненулевой код при сбое
  любого — `sources_called` тест); #7 (`KeyboardInterrupt`→`SystemExit(130)`+сообщение, лок снят
  `finally` ingest_range); #8 (resumable-подсказка про дневную квоту).
- **`_UpdateOutcome` (локальный `NamedTuple` в CLI)** — вместо `IngestRangeResult` в сигнатуре:
  обходит digit-префикс и `Any`-дыры, пиннит поля за границей importlib. История это явно
  разрешает («локальный dataclass/NamedTuple на усмотрение dev»).
- **Наблюдаемость (LESSONS Сложность 4)** наследуется бесплатно: прогресс по фазам — из INFO-логов
  `load_day`/`ingest_range` (видно в live-прогоне) + `basicConfig(INFO)`; `_handle_update`
  добавляет финальную человекочитаемую сводку по источнику.
- Гейты зелёные: mypy strict win32+linux (19 файлов), pytest offline 331 passed, live 4 passed;
  `uv.lock`/`pyproject.toml` не менялись.

### File List

- `scripts/tools/logs_api_cli.py` — M: подкоманда `update` в `_create_parser`; `_UpdateOutcome`
  (`NamedTuple`); `_handle_update`; ветка `_dispatch`; `except KeyboardInterrupt` в `main`;
  обновлён module-docstring/«Граница скоупа»; импорты `importlib`, `NamedTuple`.
- `tests/test_logs_api_cli.py` — M: секция тестов `update` (парсинг/диспетч/агрегация/fail-fast/
  KeyboardInterrupt/importlib); `import importlib`; `update` добавлен в `test_help_lists_all_subcommands`.
- `tests/test_logs_api_cli_live.py` — M: live-smoke `update` (end-to-end оба источника +
  идемпотентность); обновлён module-docstring; импорты `importlib`/`sys`/`DatabaseManager`/`paths`.
- `docs/cli.md` — M: `update` в списке команд; переписан раздел «Границы»; нюанс кодов возврата
  `update` в контракте.
- `docs/ingestion.md` — M: новый раздел «Команда обновления (`gdau-logs update`)»; forward-ссылки
  «история 2.9» → ссылки на раздел; вводная сноска и реализация-строка обновлены.

### Change Log

- 2026-05-25: Реализована подкоманда `gdau-logs update` (story 2.9, вариант B2). Тонкая
  CLI-поверхность поверх `ingest_range` (2.8): прогон обоих источников per source под своим
  локом, агрегация частичного сбоя в ненулевой код, resumable-сообщение, `KeyboardInterrupt`→
  `SystemExit(130)`. p81 не тронут. +10 offline-тестов, +2 live-теста. Гейты зелёные, `uv.lock`
  не менялся.

## Definition of Done

1. `scripts/tools/logs_api_cli.py`: подкоманда `update` (`--date1`/`--date2`/`--source {visits|hits|both}` default=both / `--hot-window`), `_handle_update` (importlib→цикл `ingest_range` per source→агрегация кода→сводка), ветка `_dispatch`, `except KeyboardInterrupt` в `main`, обновлённый module-docstring. **p81 не тронут (вариант B2).** (AC #1/#2/#4/#5/#6/#7/#8)
2. Идемпотентность/инкремент/hot-window наследуются из 2.7/2.8 (повтор не дублирует/не ломает; перезалив без DROP). (AC #3, SM-2, FR-9/10/11)
3. Частичный сбой не маскируется: оба источника опрашиваются, ненулевой код при сбое любого; ошибки `ingest_range` (аргументы/лок/креды/сверка/квота) ловятся per-source → `SystemExit(1)` без трейсбека. (AC #2/#6)
4. Прерывание: `.writer.lock` освобождается (finally внутри `ingest_range` / авто-release ядром); `KeyboardInterrupt`→чистое сообщение+`SystemExit(130)`; частичный диапазон до-грузится повтором. (AC #7)
5. Resumable-сообщение при сбое/исчерпании квоты (что загружено, что повтор пропустит, что докрутить). (AC #8)
6. `docs/cli.md` + `docs/ingestion.md` обновлены (update реализован здесь; границы пересмотрены). (project-context: компонент без актуальной спеки не «готов»)
7. Offline-тесты `test_logs_api_cli.py`: парсинг/дефолт source/диспетч/цикл per source/агрегация кода/смешанный результат (оба опрошены)/fail-fast как per-source/KeyboardInterrupt/importlib-загрузка. (AC #1/#2/#5/#6/#7)
8. Live-smoke `test_logs_api_cli_live.py`: зелёный end-to-end `update` за 1 день (skip без кредов); идемпотентность вживую. Критерий live-DoD соблюдён (LESSONS Сложность 1). (AC #1/#3)
9. Гейты зелёные: `mypy --strict scripts` (win32 + `--platform linux`), `pytest` (offline, ubuntu + windows), `pytest -m live` зелёный; `uv.lock` не менялся.

### Review Findings

_Code review 2026-05-25 (3 слоя Opus: Blind Hunter / Edge Case Hunter / Acceptance Auditor). Acceptance Auditor: все 8 AC PASS. Триаж: 1 decision-needed, 0 patch, 0 defer, 22 dismiss._

- [x] [Review][Decision→Patch применён, вариант 1] Сырой `duckdb.Error` из `ingest_range` утекал мимо обоих `except` → нарушал AC #2/#4 (трейсбек вместо понятного сообщения) и AC #6 (второй источник не опрашивается) — `duckdb.Error` наследует напрямую `Exception` (MRO: `Error→Exception→BaseException`), НЕ `RuntimeError`/`OSError`/`ValueError` (проверено `uv run`). `ingest_range` зовёт `ensure_load_state_table` (`load_state.py:113`), `create_views`, `reconcile` (`load_state.py:234/253`), `mark_loaded/loading/failed` (`load_state.py:134/146/156`) **без** обёртки их `duckdb.Error` (обёрнут только `count_partition_rows:182`). При сбое БД/IO (повреждение/занятый файл/нет места/битый parquet при выводе схемы view) сырой `duckdb.Error`: (1) минул бы per-source `except` в `_handle_update` → цикл рвётся, второй источник не опрошен (AC #6); (2) минул бы `except` в `main` → полный трейсбек (AC #2/#4). `update` — первая CLI-команда, открывающая DuckDB, → зазор живой именно с 2.9. **✅ РЕШЕНО (Шеф делегировал «выбери лучший вариант»): вариант 1 — фикс в CLI, B2 сохранён.** `import duckdb` в `logs_api_cli.py`; `duckdb.Error` добавлен в per-source `except (ValueError, RuntimeError, OSError, duckdb.Error)` `_handle_update` (→ второй источник опрашивается, AC #6) и в `except` `main` (→ без трейсбека, AC #2/#4); докстринги `_handle_update`/`main` синхронизированы. **+2 регресс-теста:** `test_update_duckdb_error_caught_per_source` (per-source: visits→`duckdb.Error`, hits ок → оба опрошены, exit 1, сводка), `test_main_duckdb_error_clean_exit` (сетка `main`: `duckdb.Error` из `_dispatch` → exit 1 + сообщение). Гейты зелёные: mypy strict win32+linux 19 файлов, pytest **333 passed** (было 331), 8 live deselected. **p81 НЕ тронут.** [Источники: edge (High) + blind]
