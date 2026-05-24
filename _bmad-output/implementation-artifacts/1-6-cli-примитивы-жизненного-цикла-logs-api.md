# Story 1.6: CLI-примитивы жизненного цикла Logs API

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита (агент),
I want неинтерактивный CLI с подкомандами жизненного цикла Logs API,
so that вручную/ad-hoc водить Logs API — AI-native канал действий.

**Контекст эпика.** Шестая и **последняя** история Epic 1 «Каркас юнита и канал Logs API». Все четыре примитива, которые этот CLI сшивает воедино, уже `done`: env-ридер кредов (1.2 → `read_metrica_credentials`), вендоренный `MetricaClient` (1.3 → методы жизненного цикла Logs API), clamp дат (1.4 → `clamp_date_range`/`parse_date`/`format_date`), загрузчик каталога (1.5 → `load_catalog().metrica_fields(source)`). Эта история — **оркестрирующая склейка**: тонкий argparse-CLI `scripts/tools/logs_api_cli.py`, который выставляет жизненный цикл Logs API как скриптуемые неинтерактивные подкоманды (канал действий, AI-native). После 1.6 агент вручную водит реальный Logs API из CLI — эпик закрыт. Покрывает **применение FR-2** (поля выгрузки берутся из каталога) и кладёт **примитивы FR-1** (`create/status/download/clean/evaluate/list`), на которые позже встанет высокоуровневый оркестратор (`gdau-logs update`, story 2.9 — **НЕ здесь**).

**Это «та же форма», но НЕ вендоринг.** В directaiq есть прямой прообраз — `scripts/tools/logs_api_cli.py` (`LogsApiCLI(BaseScript)` с `_create_parser`/`_fetch_data`/`_handle_*`). Архитектура (строка 425) велит держать **ту же форму** (argparse + `_create_parser`, per-command handlers) — глаз Шефа на неё натренирован ([[structure-mirror-directaiq]]). Но это **не построчный перенос**: directaiq-CLI завязан на инфру, которую мы сознательно не тащим (`BaseScript`, `AuthManager`, `get_logger`, `setup_paths`/`get_external_storage_root`, `config_manager` — NFR-6). Берём **только** скелет argparse + диспетчер; `main()` водит его напрямую через наши примитивы 1.2–1.5. Три поведенческих расхождения с directaiq (поля из каталога, clamp дат, креды от env-ридера — ниже) — не «причёсывание», а следствие требований предыдущих историй; вывод же делаем **как directaiq** (человекочитаемый текст).

**Граница скоупа (критично — не выходить за неё).** 1.6 — это **примитивы** жизненного цикла, тонкие проксики над `MetricaClient`. В скоуп НЕ входят: подкоманда `update`/`load` (высокоуровневый приём за диапазон — **story 2.9**), оркестратор p81 (**2.7**), запись в Parquet/хранилище (**Epic 2**), `paths.py`/резолюция `GDAU_DATA_ROOT` для записи данных (**2.1**), парсинг TSV в типы. CLI 1.6 **не пишет данные в dev-репо** (инвариант project-context) и **не зависит** от ещё не существующих модулей хранилища. `download` сохраняет сырые `.tsv`-части туда, куда указал оператор (`--output`), как ad-hoc примитив — настоящая атомарная запись под локом приходит в 2.7.

### Главные риски / расхождения с directaiq (читать до кода)

1. **`create`/`evaluate` НЕ принимают `--fields` — поля берутся из каталога (FR-2, AC #2).** В directaiq `create_parser.add_argument("--fields", required=True, ...)` — оператор передаёт список руками. У нас это **запрещено** (хардкод «на всё» / рассинхрон выгрузки↔каталога). Поля выводятся из SSOT: `fields = load_catalog().metrica_fields(args.source)`. Никакого `--fields` в парсере `create`/`evaluate`. Это и есть «применение FR-2», ради которого 1.5 написал `metrica_fields()` (отдаёт ровно `list[str]` под `MetricaClient.create_log_request(fields=...)`).

2. **`create`/`evaluate` клампят `date2` ДО сети (1.4, AC #2/#4).** directaiq не клампит — отправляет `date2` как есть (Logs API требует `date2 < today`, иначе отказ). У нас: `parse_date` (строгий `YYYY-MM-DD`) → `clamp_date_range` (зажать `date2` на «вчера по МСК», fail-loud на инвертированном диапазоне) → `format_date` обратно в строку для клиента. Невалидная/инвертированная дата → `ValueError` из `dates.py` **до любого сетевого вызова** → CLI печатает сообщение + non-zero (AC #4). `clamp_date_range` сам пишет INFO-лог о clamp.

3. **Креды — от env-ридера (1.2), инъекция в клиент (1.3, шов AC #2).** directaiq зовёт `AuthManager.get_metrica_credentials()` и строит `MetricaClient(counter_id=...)` (токен клиент добывает сам внутри). У нас: `creds = read_metrica_credentials()` → `MetricaClient(token=creds.token, counter_id=creds.counter_id)`. **Нет `--counter-id` CLI-флага** (в directaiq он был): единственный источник кредов — окружение/`.env` (NFR-5, [[cli-tools-ai-native]] не плодит второй путь кредов). Нет кредов → `read_metrica_credentials` бросает `ValueError` **до сети** → non-zero + понятное сообщение (имя недостающей переменной; токен в сообщение не попадает).

4. **Вывод — человекочитаемый текст, как directaiq; `--format` НЕ вводим (решение Шефа 2026-05-24, изменение AC #5).** Принцип: «просто и понятно — агенту в первую очередь, оператору во вторую». Агент (Claude Code) читает stdout как текст (понимает смысл), а не парсит `json.loads`, поэтому чистый человеческий текст — самое читаемое и для LLM, и для человека; json/csv-машинерия здесь — лишняя сложность (NFR-6, [[simplicity-first]], [[cli-output-human-readable]]). Каждый `_handle_*` печатает свой результат метками `Поле: значение` (для списков — простая выровненная таблица), **ровно как directaiq** (`print(f"Request ID: ...")`, `print(f"Status: ...")`). Никакого `--format`, никакого `_format_output`. _Это меняет epic AC #5 — см. раздел AC и синхронный правленый эпик._

5. **Формы ответов `MetricaClient` асимметричны (контракт — точно).** Не угадывать, что возвращает клиент (см. Dev Notes → «Контракт ответов клиента»). Кратко: `create_log_request`/`clean_log_request` возвращают **полный** ответ (нужное — под ключом `log_request`); а `get_log_request` уже **извлекает** внутренний dict; `get_log_requests` — уже список; `download_log_request_part` — `bytes`; `evaluate_log_request` — полный ответ (оценка под `log_request_evaluation`). Перепутать = баг.

## Acceptance Criteria

1. **Given** `scripts/tools/logs_api_cli.py` (argparse + `_create_parser`), **When** запускается `--help`, **Then** перечислены `create`, `status`, `download`, `clean`, `evaluate`, `list` и info-подкоманды, все неинтерактивные.
2. **Given** `create --date1 --date2 --source {visits|hits}`, **When** она выполняется, **Then** поля берутся из каталога (1.5) для источника (FR-2), `date2` проходит clamp (1.4), креды — от env-ридера (1.2), через клиент (1.3) создаётся лог-запрос с возвратом id/статуса.
3. **Given** `status`/`download`/`clean`/`evaluate`/`list` с аргументами (`--request-id`, `--part`, …), **When** они выполняются, **Then** корректно проксируют методы клиента и печатают результат.
4. **Given** любой fail (нет кредов, ошибка API, неверные аргументы), **When** выполняется подкоманда, **Then** ненулевой код + понятное сообщение; успех → `0`.
5. **Given** любую подкоманду, печатающую результат, **When** она завершается успешно, **Then** результат печатается **человекочитаемым текстом** (как directaiq: метки `Поле: значение`; для списков — простая выровненная таблица) — просто и понятно агенту-LLM и оператору; **без** параметра `--format`. _[изменение epic AC #5 по решению Шефа 2026-05-24: машинные форматы json/markdown/csv в этом CLI не вводим — лишняя сложность (NFR-6); агент читает текст, а не парсит json]_
6. **Given** вызов без подкоманды ИЛИ `--source` вне {visits,hits}, **When** CLI разбирает аргументы, **Then** argparse печатает usage/ошибку с ненулевым кодом (`required` subparsers, `choices=[...]`), без трейсбека. _[edge-case: голый вызов / невалидный source]_
7. **Given** `download`/`status` по несуществующему `--request-id` ИЛИ до статуса `processed`, **When** команда выполняется, **Then** понятная ошибка, и неполная/пустая выгрузка НЕ трактуется как успех. _[edge-case: ранний download]_
8. **Given** `create`, который API отклоняет (квота/`possible=false` по `evaluate`), **When** команда выполняется, **Then** ненулевой код + понятное сообщение, **And** `download` пишет в заданный `--output` без молчаливой перезаписи существующего файла. _[edge-case: обречённый запрос жжёт квоту / клоббер файла]_

## Tasks / Subtasks

- [ ] **Task 1 — Каркас модуля + `_create_parser` со всеми подкомандами (AC: #1, #6)**
  - [ ] Заменить стаб `scripts/tools/logs_api_cli.py` (история 1.1: `print("not yet implemented")`) реальным CLI. `from __future__ import annotations` первой строкой. Импорты — абсолютные от корня пакета: `from scripts.utils.metrica_client import MetricaClient`, `from scripts.utils.env_reader import read_metrica_credentials`, `from scripts.utils.catalog import load_catalog`, `from scripts.utils.dates import parse_date, format_date, clamp_date_range`. Stdlib: `argparse`, `logging`, `sys`, `from pathlib import Path` (форматтеры `csv`/`json`/`io` НЕ нужны — вывод человекочитаемый). **Без** `BaseScript`/`AuthManager`/`get_logger`/`setup_paths`/`config_manager`/`polars`/`pandas` — инфру и тяжёлые зависимости directaiq не тащим (NFR-6, [[directaiq-reference]]).
  - [ ] Русский модульный docstring: роль (AI-native канал действий поверх Logs API; склейка 1.2–1.5; примитивы FR-1, применение FR-2; `update` — это 2.9, не здесь). `logger = logging.getLogger(__name__)`.
  - [ ] Класс `LogsApiCLI` с `_create_parser(self) -> argparse.ArgumentParser` (форма directaiq). `description` + `formatter_class=argparse.RawDescriptionHelpFormatter`. **Без** глобального `--format` (решение Шефа: вывод человекочитаемый, как directaiq — см. риск #4 и Task 2).
  - [ ] `subparsers = parser.add_subparsers(dest="command", required=True, ...)` — **`required=True`** закрывает AC #6 (голый вызов → argparse usage + exit 2, без трейсбека).
  - [ ] Подкоманды жизненного цикла (без `--fields` у create/evaluate — риск #1; даты строками, валидирует `dates.py`):
    - `create`: `--date1` (required), `--date2` (required), `--source` (`choices=["visits","hits"]`, **required** — AC #6 не оставляет молчаливый дефолт источника на верхнеуровневой команде создания; осознанно строже directaiq, где был `default="visits"`), `--attribution` (default `"CROSS_DEVICE_LAST_SIGNIFICANT"`, parity с клиентом, опционален).
    - `evaluate`: `--date1` (required), `--date2` (required), `--source` (`choices`, required).
    - `status`: `--request-id` (`type=int`, required).
    - `download`: `--request-id` (`type=int`, required), `--part` (`type=int`, опц. — без неё все части), `--output` (опц., путь файла/каталога), `--clean` (`action="store_true"`, очистить после успешного скачивания).
    - `clean`: `--request-id` (`type=int`, required).
    - `list`: без аргументов.
  - [ ] Info-подкоманда `info` (→ `get_counter_info`) — проверка доступа/счётчика. Без аргументов. _Решение Шефа: только `info`._ **Обоснование состава:** architecture (строка 460) пишет `info` в ед. числе; в directaiq-`logs_api_cli` info-команд НЕ было вовсе (счётчик/цели жили в отдельных `metrica_cli.py`/`goals_cli.py`). `counters`/`goals` НЕ выставляем: `counter_id` приходит из `.env` (единый источник кредов — discovery не нужен), а `goals` — маркетинг-семантика Директа, нерелевантная сессиям/хитам геймдева (NFR-6, простота). epic AC #1 «info-подкоманды» удовлетворяется единственной `info` плюс уже перечисленными lifecycle-подкомандами.
- [ ] **Task 2 — Общие швы: креды+клиент, поля из каталога, человекочитаемая печать (AC: #2, #4, #5)**
  - [ ] `_build_client(self) -> MetricaClient` — `creds = read_metrica_credentials(); return MetricaClient(token=creds.token, counter_id=creds.counter_id)`. Единственная точка построения клиента (риск #3). `ValueError` отсюда (нет кредов) **не** ловить здесь — всплывает в `main()` (AC #4). Info/lifecycle-команды зовут его; клиент строить **после** разбора аргументов и валидации дат, не в парсере.
  - [ ] Поля выгрузки: `fields = load_catalog().metrica_fields(source)` — **в `create`/`evaluate`** (риск #1, FR-2). `load_catalog()` без аргумента (прод-путь, резолвится от модуля сквозь симлинк — см. 1.5). Битый каталог → `ValueError` всплывает в `main()` (non-zero).
  - [ ] **Печать — человекочитаемая, как directaiq (AC #5; без `--format`).** Каждый `_handle_*` сам печатает результат через `print()` чистым текстом (диспетчер централизованно НЕ форматирует). stdout = полезный вывод; диагностика (`logger.info` про clamp/ретраи) идёт в stderr через `logging` — не смешивать. Форма (как directaiq):
    - **Один объект** (`create`/`status`/`clean`/`evaluate`/`info`) — метки `Поле: значение` построчно (напр. `print(f"Request ID: {req_id}")`, `print(f"Status: {status}")`). Ключевые поля называть явно (для `create`/`status` — `request_id`, `status`; для `status` ещё число частей и размер каждой; для `evaluate` — `possible` + макс. дней).
    - **Список** (`list`) — простая выровненная таблица: строка-заголовок + по строке на запрос (`request_id`, `status`, `source`, диапазон дат, размер) — как `_handle_list` directaiq. Пустой список → понятная строка «нет активных запросов».
    - **Можно** вынести крошечные хелперы печати (напр. `_print_kv(pairs)`, `_print_table(rows)`), чтобы не дублировать — но это stdlib-`print`, без формат-движка. NFR-6: ничего тяжелее.
  - [ ] Handler'ы дополнительно **возвращают** структурный результат (dict/list) — это для тестов (capsys проверяет печать, возврат — точечные assert'ы) и возможного переиспользования; `main()` сам ничего не печатает.
- [ ] **Task 3 — Handler `create` (AC: #2, #8)**
  - [ ] `_handle_create(self, args) -> dict`: `d1 = parse_date(args.date1); d2 = parse_date(args.date2)` → `d1c, d2c = clamp_date_range(d1, d2)` → `fields = load_catalog().metrica_fields(args.source)` → `client = self._build_client()` → `resp = client.create_log_request(date1=format_date(d1c), date2=format_date(d2c), fields=fields, source=args.source, attribution=args.attribution)`.
  - [ ] Извлечь `log_request = resp.get("log_request", resp)` (риск #5 — `create_log_request` отдаёт полный ответ; нужное под `log_request`). **Напечатать** человекочитаемо: `request_id` и `status` (AC #2 «возврат id/статуса»; AC #5 — текст), + подсказку «проверь статус командой `status`». Вернуть `log_request` (для тестов).
  - [ ] Отказ API (квота/невозможно) → `MetricaClient` бросает `RuntimeError` (1.3: не-ретраябельные коды и тело ошибки → понятный `RuntimeError`) → всплывает в `main()` → non-zero + сообщение (AC #8, часть «обречённый запрос»). CLI **не** реализует retry/квоту заново (NFR-3, [[directaiq-reference]]).
- [ ] **Task 4 — Handler `evaluate` (AC: #3, #8)**
  - [ ] `_handle_evaluate(self, args) -> dict`: те же `parse_date`/`clamp_date_range`/`format_date` и `fields` из каталога (как create — чтобы оценка совпадала с тем, что create реально отправит). `resp = client.evaluate_log_request(date1=..., date2=..., fields=fields, source=args.source)`.
  - [ ] Извлечь `ev = resp.get("log_request_evaluation", resp)` (риск #5). **Напечатать** `possible` и `max_possible_day_quantity` человекочитаемо (AC #5). Несёт смысл для пред-проверки перед `create` (AC #8 «possible=false по evaluate» — `evaluate` это и выставляет; авто-вызова evaluate из create НЕ делаем, простота). Вернуть `ev`.
- [ ] **Task 5 — Handlers `status` / `list` (AC: #3, #5, #7)**
  - [ ] `_handle_list(self, args) -> list[dict]`: `reqs = client.get_log_requests()` (уже `list`, риск #5). **Напечатать** выровненной таблицей (`request_id`/`status`/`source`/диапазон/размер — как `_handle_list` directaiq); пустой список → строка «нет активных запросов», результат `[]` (валидно, не ошибка). Вернуть `reqs`.
  - [ ] `_handle_status(self, args) -> dict`: `data = client.get_log_request(args.request_id)` (уже извлечённый внутренний dict, риск #5). **Несуществующий `request_id`:** клиент на 404 бросит `RuntimeError` (1.3, не-ретраябельный) → всплывает (AC #7); но если API вернёт 200 с пустым `log_request` → `data == {}` → **fail-loud**: `if not data: raise ValueError(f"Запрос {request_id} не найден")` (AC #7 — пустой ответ НЕ выдавать за успех). _Не повторять directaiq, где пустой `data` логировался и возвращался `{}` как «успех»._ Иначе **напечатать** `request_id`/`status`/диапазон/число частей + размер каждой части (AC #5). Вернуть `data`.
- [ ] **Task 6 — Handler `download`: статус-гейт, выбор частей, no-clobber, без ложного успеха (AC: #3, #7, #8)**
  - [ ] `_handle_download(self, args) -> dict`. **Сначала** `info = client.get_log_request(args.request_id)`; `if not info: raise ValueError("запрос не найден")` (AC #7); `if info.get("status") != "processed": raise ValueError(f"статус '{info.get('status')}', скачивание невозможно (нужен 'processed')")` — non-zero, **ничего не пишем** (AC #7, «неполная/пустая выгрузка не = успех»). _directaiq логировал и возвращал info как «успех» — у нас это fail._
  - [ ] `parts = info.get("parts", [])`; пусто → `ValueError` (нечего качать, не «успех»). `--part` указана, но её нет в `parts` → `ValueError` (AC #7).
  - [ ] Резолюция вывода (БЕЗ `paths.py`/storage — их нет до 2.1; в dev-репо данные не пишем): `--output` задан → если суффикс есть, трактовать как файл (каталог = `parent`, префикс = `stem`), иначе как каталог; не задан → **текущий каталог запуска** (`Path.cwd()`) с префиксом `logs_{request_id}`. `output_dir.mkdir(parents=True, exist_ok=True)`. Имя части: `{prefix}_part{n}.tsv`.
  - [ ] **No-clobber (AC #8):** перед записью каждой части `if filepath.exists(): raise FileExistsError(f"{filepath} уже существует — укажи другой --output (без молчаливой перезаписи)")`. _directaiq делал `open(..., "wb")` — молчаливая перезатирка; у нас запрещено._
  - [ ] Качаем части: `content = client.download_log_request_part(request_id, part_number)` (`bytes`, риск #5) → `filepath.write_bytes(content)`. Любой сбой части (исключение клиента) всплывает → non-zero, **не** «собрали что есть» (AC #7).
  - [ ] `--clean` → после успеха всех частей `client.clean_log_request(request_id)`.
  - [ ] **Напечатать** сводку человекочитаемо (какие файлы сохранены, сколько частей, очищен ли запрос) и вернуть `{"downloaded": [str(p) for p in files], "parts": len(files), "cleaned": bool(args.clean)}` (для тестов).
- [ ] **Task 7 — Handler `clean` (AC: #3)**
  - [ ] `_handle_clean(self, args) -> dict`: `resp = client.clean_log_request(args.request_id)`; извлечь `lr = resp.get("log_request", resp)` (риск #5). **Напечатать** новый `status` человекочитаемо. Вернуть `lr`.
- [ ] **Task 8 — Info-подкоманда `info` (AC: #1, #3, #5)**
  - [ ] `info` → `data = client.get_counter_info()` (dict). Строит клиент через `_build_client` (нужны креды/сеть). **Напечатать** ключевые поля счётчика человекочитаемо (AC #5); вернуть `data`. `counters`/`goals` НЕ заводим (обоснование — Task 1).
- [ ] **Task 9 — `main()`: диспетчер + коды возврата + логирование (AC: #4, #6)**
  - [ ] `_dispatch(self, args) -> object`: по `args.command` вызвать соответствующий `_handle_*` (handler сам печатает результат — Task 2); неизвестная команда невозможна (`required=True` + `choices`), но defensive `else: raise ValueError`.
  - [ ] `def main() -> None`: `logging.basicConfig(level=logging.INFO)` (диагностика clamp/ретраев в stderr; **креды не логируем** — NFR-5). `cli = LogsApiCLI(); parser = cli._create_parser(); args = parser.parse_args()` (плохие аргументы/голый вызов → argparse сам `SystemExit(2)`, AC #6). Затем:
    ```python
    try:
        cli._dispatch(args)              # handler печатает свой результат сам
    except (ValueError, RuntimeError, FileExistsError, OSError) as exc:
        logger.error("%s", exc)          # понятное сообщение, без трейсбека
        raise SystemExit(1) from None    # non-zero (AC #4)
    # успех → неявный exit 0 (вывод уже напечатан handler'ом)
    ```
  - [ ] `if __name__ == "__main__": main()`. Entry point `gdau-logs = scripts.tools.logs_api_cli:main` уже в `pyproject.toml` (1.1) — не трогать.
  - [ ] Перехватывать **узкий** набор исключений (`ValueError`/`RuntimeError`/`FileExistsError`/`OSError`) — это контрактные ошибки 1.2–1.5 и клиента; не глотать `KeyboardInterrupt`/`SystemExit`/прочее «голым» `except` (иначе argparse `SystemExit(2)` исказится).
- [ ] **Task 10 — Спека компонента `docs/cli.md` (часть DoD)**
  - [ ] Завести `docs/cli.md` человеческим языком (project-context прямо называет `cli.md` отдельным логическим компонентом: «поверхность `gdau-logs`»). Три вопроса: **(1) Что делает** — выставляет жизненный цикл Logs API подкомандами (заказать/оценить/статус/скачать/очистить/список + справочные), неинтерактивно; **(2) Зачем** — AI-native канал действий: агент собирает ad-hoc обращения к API из примитивов-команд ([[cli-tools-ai-native]]); **(3) Контракт** — вход: подкоманда + аргументы; поля выгрузки и типы — из каталога (не руками); даты клампятся на «вчера по МСК»; креды из `.env`; выход: результат **человекочитаемым текстом** (просто и понятно агенту и оператору), успех → код 0, любой сбой → понятное сообщение + non-zero. Упомянуть границу: **`update`/полный приём — отдельная команда (Epic 2)**, здесь только примитивы. Без сигнатур кода.
- [ ] **Task 11 — Offline-тесты `tests/test_logs_api_cli.py` (AC: #1–#8)**
  - [ ] `from __future__ import annotations`; без сети. Зеркалит `scripts/` → `tests/test_logs_api_cli.py`. Сеть мокается **monkeypatch'ем имён в неймспейсе CLI-модуля** (`MetricaClient`, `read_metrica_credentials`, `load_catalog`) — фейковый клиент с заранее заданными ответами; **никакого requests-mock** (нет такой dev-зависимости — только `pytest`/`mypy`; см. `pyproject`). Шов: фейк-класс с методами `create_log_request`/`get_log_request`/… возвращает фикстурные dict/bytes.
  - [ ] **AC #1:** `_create_parser().parse_args(["--help"])` → `SystemExit(0)`; `capsys` содержит `create`, `status`, `download`, `clean`, `evaluate`, `list`, `info`.
  - [ ] **AC #2 (create):** monkeypatch `load_catalog` → фейк с `metrica_fields("visits")==["ym:s:date","ym:s:visitID"]`; monkeypatch `read_metrica_credentials`/`MetricaClient` → фейк, фиксирующий аргументы `create_log_request`. Прогон `main()` с `create --date1 ... --date2 <будущее> --source visits` → проверить: (a) клиент получил `fields` ровно из каталога (FR-2, **не** из CLI); (b) `date2` зажат на «вчера по МСК» (clamp 1.4 — передать фикс «сегодня»? clamp берёт `moscow_today()` внутри create-пути → проверять, что `date2` ≤ вчера, через monkeypatch `dates.moscow_today` ИЛИ через то, что фейк-клиент получил клампнутую строку < сегодня); (c) токен/счётчик пришли от `read_metrica_credentials`; (d) exit 0, вывод несёт `request_id`/`status`.
  - [ ] **AC #3 (прокси):** для `status`/`list`/`clean`/`evaluate`/`info` — фейк-клиент с известным ответом → проверить, что вызван правильный метод с правильными аргументами и результат напечатан (`capsys`).
  - [ ] **AC #4 (коды/сообщения):** нет кредов → monkeypatch `read_metrica_credentials` бросает `ValueError("YANDEX_METRICA_TOKEN ...")` → `main()` → `SystemExit` с кодом 1, stderr несёт сообщение, **токен в выводе не светится**; ошибка API → фейк-клиент бросает `RuntimeError` → exit 1 + сообщение. Успех → exit 0.
  - [ ] **AC #5 (человекочитаемый вывод, без `--format`):** `capsys` после `status`/`create`/`list` содержит ключевые поля понятным текстом (напр. `Request ID`/`Status`, значения `request_id`/`status`); `list` печатает строку на каждый запрос; пустой `list` → строка «нет активных запросов». Проверить, что парсер **не** принимает `--format` (`parse_args(["--format","json","list"])` → `SystemExit(2)`).
  - [ ] **AC #6 (argparse-гард):** `parse_args([])` (голый, без подкоманды) → `SystemExit` код 2; `create --date1 .. --date2 .. --source sessions` (вне choices) → `SystemExit` код 2; оба — `capsys` содержит usage, без трейсбека.
  - [ ] **AC #7 (ранний download/status):** фейк `get_log_request` → `{"status":"created","parts":[]}` → `download` поднимает ошибку, **ни один файл не записан** (проверить `tmp_path` пуст), exit 1; `get_log_request` → `{}` (не найден) → `status` и `download` → ошибка not-found, exit 1; `--part 5`, которой нет в `parts` → ошибка.
  - [ ] **AC #8 (квота/клоббер):** фейк `create_log_request` бросает `RuntimeError("quota")` → exit 1 + сообщение; `download` в `tmp_path`, где целевой `logs_{id}_part1.tsv` **уже существует** → `FileExistsError` всплывает → exit 1, существующий файл **не перезаписан** (сверить содержимое до/после).
  - [ ] **AC #2 невалидная дата:** `create --date1 2026-13-99 ...` → `parse_date` `ValueError` → exit 1 + сообщение, **до** построения клиента (фейк-клиент не вызван); инвертированный диапазон (`date1 > date2` после clamp) → `clamp_date_range` `ValueError` → exit 1.
  - [ ] **Анти-зависимость (через `ast`, не подстроку):** распарсить `ast` модуля `logs_api_cli.py`, проверить отсутствие `Import`/`ImportFrom` на `pandas`, `polars`, `numpy`, и на directaiq-инфру `base_script`/`auth_manager`/`config_manager` (top-level имя `name.split(".")[0]`; приём из `test_catalog.py`/`test_env_reader.py`). Гарантирует «та же форма, но не та же обвязка» (NFR-6).
- [ ] **Task 12 — Live-smoke `tests/test_logs_api_cli_live.py` (opt-in, обязателен для внешнего API)**
  - [ ] По project-context и [[realapi-smoke-tests]]: компонент дёргает **реальный** Logs API → нужен opt-in live (моки не отражают контракт). `@pytest.mark.live`; нет кредов → `pytest.skip` с причиной (как `test_metrica_client_live.py`).
  - [ ] **Минимальный и дешёвый — через `evaluate`** (GET оценки; **не жжёт** create/download-квоту, уважает ≤5000/day): построить `evaluate`-путь CLI на реальных кредах с **полным набором полей из реального каталога** за окно в 1 день. Цель — подтвердить **реальный контракт**: каталожные `metrica_fields(source)` (115 полей) **приняты** настоящим API (`possible` присутствует в ответе), т.е. список полей каталога не разошёлся с Logs API. Это уникальная ценность 1.6 поверх live-теста 1.3 (тот бьёт только `get_counter_info`).
  - [ ] Прогонять для обоих источников visits и hits (по одному дешёвому `evaluate` на источник). Узкое окно (`date2`=вчера по МСК, `date1`=вчера) — минимум нагрузки.
- [ ] **Task 13 — Гейты верификации (обязательны перед закрытием)**
  - [ ] `uv run mypy scripts` → зелено (strict; CLI полностью типизирован; argparse `Namespace`-атрибуты — `Any`, но сигнатуры handler'ов аннотированы). Новых зависимостей нет → `uv.lock` не меняется.
  - [ ] `uv run pytest` → зелено (новый offline-набор + регрессия 1.1–1.5; live отсеян `addopts="-m 'not live'"`).
  - [ ] (Ручной, документировать) `uv run pytest -m live` против реального счётчика — проверить, что каталожные поля приняты API (см. Task 12). В CI не гоняется.
  - [ ] Прогнать чек-лист «Definition of Done» из Dev Notes.

## Dev Notes

### Контракт ответов клиента (риск #5 — НЕ угадывать; источник: `scripts/utils/metrica_client.py`)

| Метод | Возвращает | Где «полезное» |
|---|---|---|
| `create_log_request(date1, date2, fields, source, attribution)` | **полный** dict ответа | `resp["log_request"]` → `request_id`, `status` |
| `evaluate_log_request(date1, date2, fields, source)` | **полный** dict ответа | `resp["log_request_evaluation"]` → `possible`, `max_possible_day_quantity` |
| `get_log_request(request_id)` | **уже извлечённый** `log_request` dict (или `{}` если нет) | сам dict: `status`, `parts:[{part_number,size}]`, `date1/date2` |
| `get_log_requests()` | **уже** `list[dict]` (или `[]`) | элементы: `request_id`, `status`, `source`, … |
| `download_log_request_part(request_id, part_number)` | `bytes` (сырой TSV) | пишем как есть, без разбора |
| `clean_log_request(request_id)` | **полный** dict ответа | `resp["log_request"]` → новый `status` |
| `get_counter_info()` / `get_counters()` / `get_goals()` | dict / list[dict] / dict | как есть |

Асимметрия `create`/`clean` (полный ответ) vs `get_log_request` (извлечённый) — наследие вендора; учесть в handler'ах (`resp.get("log_request", resp)` для первых, прямой dict для второго).

### Поведенческие расхождения с directaiq-CLI (свести воедино)

| Аспект | directaiq `LogsApiCLI` | Наш CLI 1.6 | Почему |
|---|---|---|---|
| Базовый класс | `BaseScript` + `run()`/`_run()`/`_fetch_data()` | автономный класс + `main()` | NFR-6, не тащим инфру |
| Поля выгрузки | `--fields` (required CLI-арг) | из каталога `metrica_fields(source)` | FR-2 (SSOT, AC #2) |
| Даты | без clamp | `parse_date`→`clamp_date_range`→`format_date` | FR-5 / 1.4 (AC #2) |
| Креды | `AuthManager` + `--counter-id` | `read_metrica_credentials()`, без `--counter-id` | 1.2/1.3 шов, NFR-5 (AC #2) |
| Вывод | `print(...)` человеческий в каждом handler | **то же** — человекочитаемый `print(...)` в каждом handler, **без** `--format` | как directaiq (решение Шефа: просто и понятно агенту/оператору, NFR-6) |
| `download` clobber | `open(...,"wb")` молча перезатирает | `FileExistsError` если файл есть | AC #8 |
| `status`/`download` пустой ответ | логирует, возвращает `{}` как «успех» | fail-loud not-found / not-processed | AC #7 |
| `load`/`update` подкоманда | есть (делегирует p81) | **НЕТ** (это story 2.9) | граница скоупа |
| Логирование | `get_logger` (directaiq) | stdlib `logging` | NFR-6 |

### Границы 1.6 (не выходить)

- Только примитивы жизненного цикла поверх `MetricaClient`. **Не** реализуем: `update`/`load` (2.9), p81-оркестрацию (2.7), запись Parquet/атомарность/лок/сверку (Epic 2), `paths.py`/резолюцию storage для записи (2.1), парсинг TSV→типы, view'ы (2.6).
- **В dev-репо данные не пишем.** `download` — ad-hoc примитив: пишет сырые `.tsv` туда, куда указал оператор (`--output`; дефолт — cwd). Реальная атомарная запись в `data/raw/{source}/{date}.parquet` под `.writer.lock` — это 2.2/2.7, не здесь.
- Retry/rate-limit/квота — **только** в `MetricaClient` (NFR-3), CLI их не реализует заново. Отказ API всплывает как `RuntimeError` → non-zero.

### Тестируемость — швы через monkeypatch имён модуля

Единственные швы — три имени, импортированные в неймспейс CLI-модуля: `MetricaClient`, `read_metrica_credentials`, `load_catalog`. Offline-тесты монкейпатчат их на фейки (фейк-клиент с фикстурными ответами; фейк-каталог с известным `metrica_fields`). Нет сетевых вызовов, нет `.env`, нет requests-mock (нет такой dev-зависимости). Для проверки clamp в `create` без зависимости от реального «сегодня» — монкейпатчить `scripts.utils.dates.moscow_today` (он же используется в `clamp_date_range` по умолчанию) ЛИБО проверять, что фейк-клиент получил `date2` строго `< today`. Анти-зависимость — через `ast` (не подстроку: docstring/комментарии содержат `directaiq`/`polars` → ложный красный).

### Project Structure Notes

- Модуль — `scripts/tools/logs_api_cli.py` ровно по дереву архитектуры (строка 460) и таблице соответствия directaiq (строка 425, «та же форма»). `scripts/tools/` — регулярный пакет (`__init__.py` из 1.1) → entry point `gdau-logs = scripts.tools.logs_api_cli:main` (pyproject, 1.1) резолвится.
- Имена snake_case (модуль/функции), класс `LogsApiCLI` CapWords; type hints обязательны (mypy strict). CLI = `{name}_cli.py` + класс с `_create_parser()` (форма directaiq — architecture#Naming строка 325).
- `tests/` зеркалит `scripts/`: `tests/test_logs_api_cli.py` + `tests/test_logs_api_cli_live.py` (опт-ин). `[tool.pytest.ini_options]` (маркер `live`, `addopts`) уже настроен (1.3).
- **`docs/cli.md` — заводится** (Task 10): project-context прямо называет его компонентом «поверхность `gdau-logs`». Часть DoD.
- `uv.lock` не трогаем — stdlib-only (`argparse`/`csv`/`json`/`io`/`logging`/`pathlib`) + уже существующие `requests`(в клиенте). Конфликтов со структурой нет; не реорганизовывать, не переводить на src-layout, не переименовывать пакет `scripts`.

### Definition of Done — чек-лист самопроверки

1. `scripts/tools/logs_api_cli.py` — реальный CLI вместо стаба 1.1; `from __future__ import annotations`; русский docstring; stdlib + примитивы 1.2–1.5; **без** `BaseScript`/`AuthManager`/`get_logger`/`config_manager`/`polars`/`pandas`.
2. `_create_parser`: **без** `--format`; `subparsers(required=True)`; подкоманды `create`/`evaluate` (без `--fields`, `--source` required choices), `status`/`download`/`clean` (`--request-id`), `list`, info-подкоманда `info`. (AC #1, #6)
3. `create`: поля из `load_catalog().metrica_fields(source)` (FR-2), `date2` clamp (1.4), креды `read_metrica_credentials` → `MetricaClient(token=,counter_id=)` (1.3), возврат `request_id`/`status`. (AC #2)
4. `status`/`download`/`clean`/`evaluate`/`list`/info корректно проксируют методы клиента с учётом асимметрии ответов; каждый печатает результат **человекочитаемым текстом** (как directaiq, без `--format`). (AC #3, #5)
5. `download`: статус-гейт `processed`, not-found/empty → fail (не «успех»), выбор `--part`, **no-clobber** существующего файла, опц. `--clean`. (AC #7, #8)
6. `main()`: argparse-гард (голый вызов/невалидный source → exit 2 без трейсбека); контрактные исключения → понятное сообщение + exit 1; успех → результат + exit 0; `logging.basicConfig`, креды не логируются. (AC #4, #6, NFR-5)
7. Offline-тесты покрывают AC #1–#8 (вкл. невалидные даты, клоббер, ранний download, человекочитаемый вывод + отсутствие `--format`, анти-зависимость по `ast`); live-smoke `evaluate` подтверждает приём каталожных полей реальным API (opt-in, skip без кредов).
8. `docs/cli.md` заведён (3 вопроса простыми словами) — DoD компонента.
9. `uv run mypy scripts` и `uv run pytest` — зелёные; `uv.lock` не менялся (новых зависимостей нет).
10. Велась в отдельной ветке `story/1.6-logs-api-cli` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС (ubuntu + windows).

### Latest Tech Information

- **stdlib `argparse` (Python 3.13):** `add_subparsers(required=True, dest="command")` — голый вызов даёт `error: the following arguments are required: command` + exit 2 (AC #6). `choices=["visits","hits"]` — невалидный source → exit 2. `argparse` сам печатает usage в stderr и поднимает `SystemExit` — не перехватывать его «голым» except. `RawDescriptionHelpFormatter` сохраняет форматирование описания.
- **Коды возврата console_scripts:** entry point зовёт `main()`; для non-zero — `raise SystemExit(1)`/`sys.exit(1)` внутри `main()` (возврат `int` из `main` тоже сработал бы, но проект фиксирует `def main() -> None` + явный `SystemExit` — project-context, форма стаба 1.1).
- **Вывод — человекочитаемый `print()`** (как directaiq), без формат-движка: метки `Поле: значение` для объектов, простая выровненная таблица для списков. Решение Шефа (2026-05-24): просто и понятно агенту-LLM (он читает текст, а не парсит json) и оператору; json/csv-машинерия — лишняя сложность (NFR-6). Это меняет epic AC #5.
- **Web-ресёрч не требуется:** argparse стабилен; контракт Logs API инкапсулирован в `MetricaClient` (1.3, уже live-протестирован). Версии стека зафиксированы локом.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 1.6] (строки 213–228) — user story + 8 AC (усилены edge-case hunter).
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1] (строки 114–116, 130–132) — роль 1.6 в эпике: CLI-примитивы жизненного цикла; FR-2 примен., примитивы FR-1.
- [Source: _bmad-output/planning-artifacts/epics.md#FR-1] (строка 23) / #FR-2 (строка 24) — полный цикл Logs API; заданный список полей из каталога, не хардкод.
- [Source: _bmad-output/planning-artifacts/epics.md#Story 2.9] (строки 354–369) — `gdau-logs update` (НЕ здесь): подтверждает границу скоупа 1.6.
- [Source: _bmad-output/planning-artifacts/architecture.md#API & Communication Patterns] (строки 231–234) — CLI = канал действий, argparse + `_create_parser`, полная поверхность lifecycle + info, неинтерактивно, AI-native.
- [Source: _bmad-output/planning-artifacts/architecture.md#Format Patterns] (строка 370) — CLI-вывод `json|markdown|csv` **НЕ применяем** (решение Шефа 2026-05-24: человекочитаемый вывод как directaiq — см. риск #4/AC #5); (строки 362–363) формат дат `YYYY-MM-DD`, МСК — применяем.
- [Source: _bmad-output/planning-artifacts/architecture.md#Communication & Process Patterns] (строки 375–377) — успех → 0; любой fail → non-zero + сообщение.
- [Source: _bmad-output/planning-artifacts/architecture.md#directaiq mapping] (строка 425) — `tools/logs_api_cli.py` = «та же форма (argparse + `_create_parser`)».
- [Source: _bmad-output/planning-artifacts/architecture.md#Directory Structure] (строка 460) — `logs_api_cli.py # argparse: update|create|status|download|clean|evaluate|list|info` (`update` — 2.9); (строки 506–508) entry points `gdau-logs = scripts.tools.logs_api_cli:main`.
- [Source: _bmad-output/project-context.md#Language-Specific Rules] (строки 46–50) — CLI только stdlib argparse, `_create_parser`, `main() -> None`, успех 0 / fail non-zero, без Typer/Click; logging, не print для диагностики.
- [Source: _bmad-output/project-context.md#Границы и каналы] (строки 121–124) — CLI = действия/запись/жизненный цикл; не тащить инфру directaiq (BaseScript/config_manager).
- [Source: scripts/utils/metrica_client.py:228–374] — сигнатуры и формы ответов методов жизненного цикла (риск #5).
- [Source: scripts/utils/dates.py] — `parse_date`/`format_date`/`clamp_date_range` (clamp date2 «вчера по МСК», fail-loud на инвертированном диапазоне).
- [Source: scripts/utils/env_reader.py] — `read_metrica_credentials() -> MetricaCredentials(token, counter_id)`; fail-loud до сети; токен `repr=False`.
- [Source: scripts/utils/catalog.py + story 1.5] — `load_catalog().metrica_fields(source) -> list[str]` (ровно под `create_log_request(fields=...)`); 74 visits + 41 hits.
- [Source: ../directaiq/scripts/tools/logs_api_cli.py] — форма-образец: `_create_parser`, per-command `_handle_*`, обработка download/parts/clean. НЕ переносить `BaseScript`/`AuthManager`/`--fields`/silent-clobber/«пустой как успех».
- [Source: docs/metrica-client.md, docs/creds.md] — образец человекочитаемой спеки компонента (для `docs/cli.md`); контракт: клиент не решает даты/поля (это CLI), креды передаются готовыми.
- [Source: tests/test_metrica_client_live.py] — паттерн opt-in live (`@pytest.mark.live`, skip без кредов) для Task 12.
- [Source: tests/test_catalog.py, tests/test_env_reader.py] — паттерн offline-тестов: monkeypatch/tmp_path/capsys, анти-зависимость через `ast`.
- [Memory: cli-tools-ai-native] — возможности = скриптуемые CLI-команды; агент водит. [[cli-output-human-readable]] — вывод CLI человекочитаемым текстом (как directaiq, без `--format`): просто и понятно агенту-LLM, затем оператору. [[structure-mirror-directaiq]] — держать форму directaiq. [[directaiq-reference]] — вендорим примитивы, не инфру. [[realapi-smoke-tests]] — внешний API требует opt-in live. [[feedback-decide-and-apply]] — решения (вывод как directaiq, info=только `info`, no `--counter-id`) согласованы с Шефом.

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
