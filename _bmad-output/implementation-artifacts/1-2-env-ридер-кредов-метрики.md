# Story 1.2: env-ридер кредов Метрики (FR-4)

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want тонкий env-ридер токена и счётчика Метрики,
so that инструменты получают креды единообразно и падают понятно ДО сетевых вызовов при их отсутствии.

**Контекст эпика:** Вторая история Epic 1 «Каркас юнита и канал Logs API». Каркас (`uv`-пакет `scripts/`, entry points, CI, mypy strict) уже стоит (1.1 = done). Эта история кладёт **первый рабочий модуль-примитив** — `scripts/utils/env_reader.py`. От него зависят: вендоренный `MetricaClient` (1.3 — инъекция кредов в развязанный конструктор), CLI `create` (1.6 — берёт креды отсюда), оркестратор p81 (2.7) и MCP. Это **развязка шва вендоринга №1**: directaiq читает креды через `AuthManager.get_metrica_credentials()`, который тянет `tapi_yandex_direct` + `ConfigManager` и делает fallback на Direct-токен. Мы заменяем это тонким ридером без тяжёлых зависимостей и без Direct-fallback.

**Главный риск истории — НЕ перетащить сложность directaiq.** Соблазн скопировать `auth_manager.py` целиком велик; делать этого нельзя (он тянет multi-account, `ConfigManager`, `YandexDirect`). Нужен тонкий ридер на `python-dotenv` + `os.environ`, и ничего больше.

## Acceptance Criteria

1. **Given** `.env` с `YANDEX_METRICA_TOKEN` и `YANDEX_METRICA_COUNTER_ID`, **When** вызывается env-ридер, **Then** он возвращает оба значения.
2. **Given** отсутствует любая из двух переменных, **When** вызывается env-ридер, **Then** поднимается понятная ошибка с именем недостающей переменной — до любых сетевых вызовов (fail-loud).
3. **Given** есть `YANDEX_DIRECT_TOKEN`, но нет `YANDEX_METRICA_TOKEN`, **When** вызывается env-ридер, **Then** fallback на Direct-токен НЕ происходит (осознанное отличие от directaiq).
4. **Given** env-ридер, **When** смотрим зависимости, **Then** он не тянет `ConfigManager`/`AuthManager`/`tapi_yandex_*` (тонкий ридер на `python-dotenv`/`os.environ`).
5. **Given** переменная задана, но пустая или из одних пробелов, **When** вызывается env-ридер, **Then** это трактуется как отсутствие → fail-loud (а не «прошла проверку»). _[edge-case: пустой токен падает позже с opaque 4xx]_
6. **Given** `YANDEX_METRICA_COUNTER_ID` не приводится к целому, **When** вызывается env-ридер, **Then** понятная ошибка (счётчик — целочисленный). _[edge-case: мусорный counter_id]_
7. **Given** `.env` отсутствует ИЛИ путь хранилища (`GDAU_DATA_ROOT`) не разрешается, **When** вызывается env-ридер, **Then** fail-loud с понятным сообщением о ненайденном `.env`. _[edge-case: тихое чтение пустого окружения]_

## Tasks / Subtasks

- [x] **Task 1 — Создать модуль `scripts/utils/env_reader.py` (AC: #1, #4)**
  - [x] Первой строкой кода — `from __future__ import annotations` (инвариант проекта, так в каждом модуле).
  - [x] Модульный docstring на русском: роль модуля — «тонкий ридер кредов Метрики из окружения; замена `auth_manager.py` directaiq, без Direct-fallback и тяжёлых зависимостей».
  - [x] Импорты — **только** stdlib (`os`, `pathlib`, `dataclasses`, `logging`) + `from dotenv import load_dotenv`. **НЕ импортировать** `ConfigManager`/`AuthManager`/`tapi_yandex_*`/`requests`/`duckdb` (AC #4 — проверяется тестом).
  - [x] Объявить frozen dataclass результата:
    ```python
    @dataclass(frozen=True, slots=True)
    class MetricaCredentials:
        token: str
        counter_id: int
    ```
    `counter_id` хранится как `int` (валидируется в Task 4; вендоренный клиент 1.3 подставит его в URL-путь f-строкой — `int` сериализуется корректно).
  - [x] Публичная функция-вход: `def read_metrica_credentials() -> MetricaCredentials:` — единственная точка, которую дёргают 1.3/1.6/2.7. (Имя — аналог directaiq `get_metrica_credentials`, но без его семантики fallback.)
  - [x] Имена переменных-констант вынести в модуль: `TOKEN_ENV = "YANDEX_METRICA_TOKEN"`, `COUNTER_ENV = "YANDEX_METRICA_COUNTER_ID"`, `DATA_ROOT_ENV = "GDAU_DATA_ROOT"` (см. Dev Notes → «Имя DATA_ROOT»).
- [x] **Task 2 — Best-effort загрузка `.env` без полного `paths.py` (AC: #1, #7)**
  - [x] Приватный `def _load_env() -> bool:` — загружает `.env` в `os.environ`, **возвращает агрегат `found_storage or found_cwd`** (был ли найден ХОТЬ один файл — для диагностики AC #7). Оба вызова `load_dotenv` возвращают bool «нашёл ли файл» — их надо собрать через OR, НЕ брать флаг только от последнего вызова:
    ```python
    found_storage = False
    if (data_root := os.environ.get(DATA_ROOT_ENV)):
        storage_env = Path(data_root) / ".env"
        if storage_env.is_file():
            found_storage = load_dotenv(storage_env, override=False, interpolate=False)
    found_cwd = load_dotenv(override=False, interpolate=False)  # поиск в cwd/родителях
    return bool(found_storage) or bool(found_cwd)
    ```
  - [x] **`override=False` ВЕЗДЕ:** процесс-окружение (CI / `uv --env-file .env`) не затирается `.env`-файлом — реальные креды имеют приоритет. _См. Dev Notes → «Precedence» и [[mcp-env-delivery]]._
  - [x] **`interpolate=False` ВЕЗДЕ (критично):** по умолчанию `python-dotenv` интерполирует `${VAR}`/`$VAR` в значениях → токен, содержащий `$`, молча искажается (тихий провал ровно того класса, против которого AC #5/#7). Креды — не шаблоны. _См. Dev Notes → «interpolate»._
  - [x] Использовать `storage_env.is_file()` (не `.exists()`) — если `GDAU_DATA_ROOT` указывает на файл/мусор, `Path(file)/".env"` не пройдёт `is_file()`, загрузка просто пропускается (см. AC #7-трактовку в Task 3).
  - [x] **НЕ реализовывать полную резолюцию путей** (`data/raw/...`, `gdau.duckdb`, `.writer.lock`) — это `paths.py`, история 2.1. Здесь только локализация `.env`. _[anti-scope: не тащить 2.1 вперёд]_
  - [x] `_load_env()` не должен падать только из-за отсутствия/битости файла (креды могут прийти из процесс-окружения); решение о fail — в Task 3 по факту отсутствия кредов. **Граница с 2.1:** там `DATA_ROOT` на несуществующий путь = жёсткий fail-loud (Story 2.1 AC); здесь — мягко (best-effort), не копировать строгую логику 2.1 сюда.
  - [x] **Никаких side-effect на уровне модуля:** `load_dotenv`/чтение `os.environ` — только ВНУТРИ функций, не при импорте (частый промах при вендоринге `paths.py`, который грузит env лениво через module-флаг). Модуль обязан импортироваться без побочных эффектов (проверяется тестом, Task 5).
- [x] **Task 3 — Валидация присутствия + пусто/пробелы как отсутствие (AC: #2, #5, #7)**
  - [x] Приватный `def _require(env_name: str, *, env_found: bool) -> str:`:
    - читает `os.environ.get(env_name)`, делает `.strip()`;
    - пустое/`None`/только пробелы → `raise ValueError(...)` с **именем переменной** и подсказкой про `.env` (AC #2, #5).
  - [x] Сообщение об ошибке зависит от `env_found` (AC #7): если ни один `.env` не найден И переменной нет — текст явно указывает «`.env` не найден (проверь `GDAU_DATA_ROOT` или запусти из каталога хранилища)»; если `.env` найден, но переменной нет — «переменная `X` отсутствует в `.env`/окружении». Так нет «тихого чтения пустого окружения».
  - [x] **AC #7 — «путь хранилища не разрешается» трактуется как «`.env` не найден»** (осознанно, без отдельной проверки существования каталога — это 2.1): если `GDAU_DATA_ROOT` задан, но указывает на несуществующий/битый путь, `_load_env()` вернёт `env_found=False` (storage-файла нет, cwd-файла нет) → при отсутствии кредов сообщение `_require` упомянет и `.env`, и `GDAU_DATA_ROOT`. Отдельной ветки «каталог не существует → свой текст» не делаем.
  - [x] **Тип исключения — `ValueError`** (как directaiq `auth_manager.py:391`), НЕ кастомный класс. Контракт с 1.6: CLI ловит `ValueError` от ридера и маппит в non-zero exit + печать сообщения; контракт с 1.3: конструктор клиента не ловит — падение до сети.
  - [x] Логировать факт через stdlib `logging` (уровень ERROR перед `raise`), **не** `print`; **креды не логировать** (ни значение токена, ни в каком виде, ни в `repr`) — NFR-5.
- [x] **Task 4 — Приведение counter_id к int (AC: #6)**
  - [x] Приватный `def _coerce_counter_id(raw: str) -> int:` — `int(raw.strip())` в `try/except ValueError` → `raise ValueError("YANDEX_METRICA_COUNTER_ID должен быть положительным целым, получено: <repr>")`.
  - [x] **Проверить знак/ноль:** `int("-5")` и `int("0")` валидны для `int()`, но счётчик Метрики — строго положительное целое; `value <= 0 → raise ValueError(...)` (иначе мусорный counter молча уйдёт в URL клиента 1.3 → opaque-4xx — тихий провал).
  - [x] В сообщении об ошибке `repr` мусорного **counter_id** допустим (не секрет, помогает диагностике). **Токен** в сообщения/логи не попадает никогда.
  - [x] Собрать и вернуть `MetricaCredentials(token=..., counter_id=...)`.
- [x] **Task 5 — Тесты offline `tests/test_env_reader.py` (AC: #1–#7)** — _см. Dev Notes → «Тестирование»_
  - [x] `from __future__ import annotations`; использовать `monkeypatch.setenv/delenv` и `tmp_path` — **без реального `.env`** и **без сети**.
  - [x] **Изоляция окружения — fixture (autouse), обязательна для детерминизма:**
    - `monkeypatch.delenv` для `YANDEX_METRICA_TOKEN`/`YANDEX_METRICA_COUNTER_ID`/`YANDEX_DIRECT_TOKEN`/`GDAU_DATA_ROOT` (`raising=False`);
    - **`monkeypatch.chdir(tmp_path)`** — увести cwd в чистый каталог, иначе `load_dotenv()` (walk-up по cwd/родителям) подхватит реальный `.env` из корня dev-репо/родителя → тест AC #7 станет флапающим (зелёным/красным по машине). `delenv` сам по себе НЕ глушит walk-up — нужен chdir. _Это ловушка project-context «зелёный/красный зависит от машины»._
  - [x] Кейсы (минимум, по одному тесту на ветку):
    - оба значения заданы в окружении → `MetricaCredentials(token, counter_id)` и `isinstance(counter_id, int)` (AC #1);
    - нет `YANDEX_METRICA_TOKEN` → `ValueError`, подстрока `"YANDEX_METRICA_TOKEN"` в тексте (AC #2);
    - нет `YANDEX_METRICA_COUNTER_ID` → `ValueError`, подстрока `"YANDEX_METRICA_COUNTER_ID"` (AC #2);
    - задан только `YANDEX_DIRECT_TOKEN`, METRICA нет → `ValueError`, подстрока `"YANDEX_METRICA_TOKEN"`, **Direct не используется** (AC #3);
    - токен `""` и токен `"   "` → `ValueError` (отсутствие) (AC #5);
    - `COUNTER_ID="abc"` → `ValueError`, подстрока `"целым"` (AC #6);
    - `COUNTER_ID="-5"` и `COUNTER_ID="0"` → `ValueError`, подстрока `"положительным"` (AC #6, знак/ноль);
    - `.env` в `tmp_path` (записать токен+counter), `monkeypatch.setenv("GDAU_DATA_ROOT", str(tmp_path))`, переменных в окружении нет → читаются из файла (AC #1, #7 happy);
    - `GDAU_DATA_ROOT` не задан, `.env` нет, переменных нет → `ValueError` с упоминанием `.env` (AC #7);
    - `GDAU_DATA_ROOT=str(tmp_path/"нет-такого")` (несуществующий), переменных нет → `ValueError` с упоминанием `.env`/`GDAU_DATA_ROOT` (AC #7, битый путь);
    - токен с обрамляющими пробелами в `.env` (`"  tok123  "`) → возвращается `"tok123"` (фиксируем, что `.strip()` чистит только обрамление, не значащие символы).
  - [x] **Тест AC #4 (анти-зависимости) — НЕ по голой подстроке (ловушка):** модульный docstring сам содержит слово `auth_manager` (Task 1) → наивный `assert "auth_manager" not in source` даст **ложный красный**. Проверять реальные импорты, не текст: либо распарсить `ast` модуля и убедиться, что в `Import`/`ImportFrom`-узлах нет `config_manager`/`auth_manager`/`tapi_yandex*`; либо после `import scripts.utils.env_reader` проверить `"tapi_yandex_direct" not in sys.modules` (и т.п.) при чистом старте. AST-вариант надёжнее и не зависит от порядка тестов.
  - [x] **Тест «импорт без side-effects» (Task 2-дисциплина):** снять снимок `os.environ`, выполнить `import scripts.utils.env_reader` (через `importlib.reload`/в подпроцессе для чистоты), убедиться, что `os.environ` не изменился и импорт не требует переменных (не падает). Защищает от регресса `load_dotenv` на уровне модуля.
- [x] **Task 6 — Спека компонента `docs/creds.md` (DoD, project-context)**
  - [x] Завести **выделенный** `docs/creds.md` (компонент «креды и окружение») и описать человеческим языком: что читает (токен + счётчик), откуда (`.env` хранилища через `GDAU_DATA_ROOT` / процесс-окружение), что обещает (fail-loud до сети, без Direct-fallback), имена переменных, приоритет источников (`override=False`). _Без актуальной спеки компонент не считается «готовым» (project-context)._
  - [x] **Осознанное отличие от карты компонентов архитектуры:** карта относила креды к `ingestion.md` (приём = client+p81+creds), но по решению Шефа (2026-05-24) у env-ридера — **отдельная** спека `docs/creds.md` (креды — самостоятельный сквозной примитив, не часть оркестратора). Спеки `metrica_client`/`p81` будут в своих компонентах (1.3/2.7) и сошлются на `creds.md`.
  - [x] ~~Синхронизировать имя переменной `DATA_ROOT` → `GDAU_DATA_ROOT`~~ — **уже сделано на этапе ревью** (2026-05-24): обновлены `project-context.md`, `architecture.md` (2 места), `epics.md` (AC 1.2 + AC 2.1). Dev-агенту переделывать не нужно; просто использовать `GDAU_DATA_ROOT`. _См. Dev Notes → «⚠️ Синхронизация имени»._
- [x] **Task 7 — Гейты верификации (обязательны перед закрытием)**
  - [x] `uv run mypy scripts` → зелёно (strict; модуль полностью типизирован, без `Any`-дыр). _См. Dev Notes → «mypy и dotenv»._
  - [x] `uv run pytest` → зелёно (новые тесты + смоук 1.1 проходят).
  - [x] Прогнать чек-лист соответствия из Dev Notes → «Definition of Done».

### Review Findings

_Code review 2026-05-24 (bmad-code-review: Blind Hunter + Edge Case Hunter + Acceptance Auditor). Acceptance Auditor: все 7 AC и DoD удовлетворены. Гейты подтверждены на ревью: `uv run mypy scripts` — чисто, `uv run pytest` — 20 passed. 14 находок отброшены как шум/осознанно-документированное поведение._

**Decision (резолвлено 2026-05-24, Шеф делегировал → выбран вариант (a) cwd-relative):**

- [x] [Review][Decision] cwd-`.env` fallback резолвится от каталога модуля, а не от cwd — расходится с `docs/creds.md` — `load_dotenv()` без пути (`scripts/utils/env_reader.py:79`) при `usecwd=False` (дефолт) и в обычном прогоне pytest/проде делает walk-up **от каталога файла-вызывателя** (`scripts/utils/`), а НЕ от cwd. Подтверждено зондом + исходником `python-dotenv` 1.2.2. Следствия: (1) `docs/creds.md` (источник №3 — «.env рядом с местом запуска, в текущем каталоге или выше по дереву») фактически неверен: поведение module-relative, не cwd-relative; (2) в установленном wheel walk-up стартует из `site-packages/` → `.env` рядом с оператором так не находится (работают только `GDAU_DATA_ROOT/.env` + процесс-окружение); (3) изоляция тестов через `chdir(tmp_path)` walk-up не глушит (см. Patch ниже). Решение Шефа: a) сделать cwd-relative (`load_dotenv(find_dotenv(usecwd=True), …)`) под обещание доков; b) оставить module-relative и переписать `docs/creds.md`; c) убрать bare-`load_dotenv()` walk-up вовсе (полагаться на `GDAU_DATA_ROOT/.env` + процесс-окружение). Выбор определяет и подход к Patch-изоляции. **→ Применён вариант (a):** `_load_env` теперь зовёт `load_dotenv(find_dotenv(usecwd=True), …)` — поиск cwd-relative, как и обещает `docs/creds.md` (правка доков не понадобилась), и корректно в установленном wheel.

**Patch (применены 2026-05-24):**

- [x] [Review][Patch] Изоляция тестов не герметична: `chdir(tmp_path)` не блокирует walk-up `load_dotenv()`; `test_import_has_no_side_effects` — слабый сторож; docstring фикстуры неточен [tests/test_env_reader.py:30] — тесты «нет .env» (`test_no_env_no_vars_fails_mentioning_env` и соседние) зелёные лишь потому, что в цепочке `scripts/utils/ → корень диска` сейчас нет `.env`; при появлении `.env` где-либо выше по дереву — флапнут (ровно ловушка project-context «зелёный/красный зависит от машины»). Нейтрализовать walk-up в фикстуре (мокнуть `env_reader.load_dotenv`/`find_dotenv` на `tmp_path`); усилить side-effect-тест (assert, что `load_dotenv` НЕ вызван при импорте, а не сравнение `os.environ` в очищенном окружении — текущий тест даёт ложную уверенность); исправить docstring фикстуры (chdir НЕ глушит walk-up; изоляцию даёт `delenv` на setup каждого теста, а не teardown-откат). Подход зависит от резолюции Decision выше. **→ Сделано:** стаб `find_dotenv` (cwd-only) в autouse-фикстуре; reload-тест заменён детерминированной AST-проверкой `test_dotenv_loaded_only_inside_functions`; docstring фикстуры переписан; добавлен позитивный cwd-тест `test_reads_from_cwd_env_file`.
- [x] [Review][Patch] `MetricaCredentials` дефолтный `repr` светит токен — нарушает NFR-5 «не в repr» [scripts/utils/env_reader.py:40] — `@dataclass(frozen, slots)` с `token: str` без `field(repr=False)`: `repr(creds)` / traceback / `logger.debug("%r", creds)` выведут токен в открытом виде. Спека (Task 3) явно требует «креды не логировать … ни в `repr`». Фикс: `from dataclasses import field`; `token: str = field(repr=False)` (порядок полей не ломается — `field(repr=False)` без default остаётся обязательным). **→ Сделано** (подтверждено: `repr(creds)` = `MetricaCredentials(counter_id=42)`, токен не светится).

_Гейты после патчей: `uv run mypy scripts` — чисто; `uv run pytest` — 21 passed._

## Dev Notes

### Развязка шва — что заменяем (источник вендоринга)

Источник: `G:\git\directaiq\scripts\utils\auth_manager.py` (NB: ссылки `D:/git/directaiq` в истории 1.1 устарели — репо лежит на `G:\`).

directaiq `AuthManager.get_metrica_credentials()` (строки 376–393):
```python
metrica_token = AuthManager._get_token_with_fallback("YANDEX_METRICA_TOKEN", "metrika:read")  # ← FALLBACK на YANDEX_DIRECT_TOKEN
counter_id = os.getenv("YANDEX_METRICA_COUNTER_ID")
if not counter_id:
    raise ValueError("YANDEX_METRICA_COUNTER_ID not found ...")
return metrica_token, counter_id   # counter_id — СТРОКОЙ
```
Что **выбрасываем** при переносе:
- `_get_token_with_fallback` → **fallback на `YANDEX_DIRECT_TOKEN` (AC #3 запрещает)**. Просто не читаем Direct-токен вовсе.
- зависимость на `ConfigManager`/`tapi_yandex_direct`/multi-account (`get_accounts`, `resolve_credentials_for_login`, `project_config.yaml`) — ничего этого в нашем ридере нет (AC #4).
- `counter_id` directaiq возвращает строкой и использует в URL-путях (`/counter/{self.counter_id}/...`). Мы **валидируем как int** (AC #6) и возвращаем `int`; f-строка в клиенте (1.3) сериализует корректно.

Что **сохраняем по духу**: имена переменных `YANDEX_METRICA_TOKEN` / `YANDEX_METRICA_COUNTER_ID` (контракт с Logs API и с будущим `.env.example` Epic 4) и принцип fail-loud до сети.

### Куда инжектятся креды дальше (контракт с 1.3)

`MetricaClient.__init__` в directaiq (`metrica_client.py:134–147`) сам зовёт `AuthManager.get_metrica_credentials()` внутри. В нашей 1.3 этот вызов **изнутри убирается**, конструктор принимает готовые `token`/`counter_id` инъекцией. То есть поверхность 1.2 — это ровно то, что 1.3 будет передавать в клиент, а 1.6 (CLI `create`) — связывать: `creds = read_metrica_credentials()` → `MetricaClient(token=creds.token, counter_id=creds.counter_id)`. Делать сам клиент здесь не нужно (это 1.3) — только ридер.

### Имя DATA_ROOT и загрузка `.env` (AC #7) — осознанное минимальное решение

directaiq резолвит хранилище через `DIRECTAIQ_DATA_ROOT` и грузит `.env` в `paths.py` (`_load_env_with_fallback`: `load_dotenv()` из cwd/родителей, затем `load_dotenv(external_root/".env", override=True)`). Наш аналог переменной — **`GDAU_DATA_ROOT`** (решение Шефа 2026-05-24; зафиксировано в [[gdau-env-contract]]).

> **⚠️ Синхронизация имени (выполнена на ревью 2026-05-24):** архитектура и project-context называли переменную обобщённо `DATA_ROOT` — конкретное имя там не было зафиксировано. Имя `GDAU_DATA_ROOT` теперь синхронизировано во всех контрактных документах: `project-context.md` (раздел «Границы и каналы»), `architecture.md` (дерево `paths.py` + раздел границ), `epics.md` (AC #7 story 1.2 + AC story 2.1 `paths.py`). **Story 2.1 обязана использовать ровно `GDAU_DATA_ROOT`.** Исторические документы (implementation-readiness-report, prd/brief addendum) намеренно не трогались.

**Граница со story 2.1:** полный `scripts/utils/paths.py` (резолюция `data/raw/{source}/{date}.parquet`, `gdau.duckdb`, `.writer.lock`) — это история 2.1. Здесь реализуем **только** локализацию `.env` (best-effort) — минимально необходимое для AC #7. Когда 2.1 принесёт `paths.py`, `_load_env()` можно будет рефакторить на него; сейчас прямой `os.environ.get("GDAU_DATA_ROOT")` достаточен и не создаёт преждевременной связанности.

**Precedence (важно):** грузим с `override=False`, чтобы реальное процесс-окружение (CI; режим `uv --env-file .env`) имело приоритет над `.env`-файлом. Это противоположно directaiq (`override=True` для external `.env`), и это осознанно: у нас креды могут прийти прямо в окружение, и `.env`-файл не должен их затирать.

> **Следствие precedence (зафиксировать, не баг):** если переменная задана в процесс-окружении ПУСТОЙ (`""` — например CI-секрет не подставился, или `uv --env-file` с пустым значением), `override=False` НЕ даст валидному `.env` её перезаписать → `_require` увидит `""` → fail-loud (AC #5). Для безопасности это правильнее тихого override (пустой секрет = явная проблема конфигурации). При отладке «в `.env` же есть токен, почему падает» — причина здесь: проверь, нет ли пустой одноимённой переменной в окружении.

**interpolate (критично — тихое искажение токена):** `load_dotenv(..., interpolate=False)` ВЕЗДЕ. По умолчанию `python-dotenv` интерполирует `${VAR}`/`$VAR` внутри значений: токен вида `abc${x}def` молча станет `abcdef`, `.strip()` это пропустит, и клиент (1.3) упадёт позже на opaque-4xx — ровно «тихий провал», против которого AC #5/#7. OAuth-токены обычно alphanumeric, но `$` контрактом не запрещён → отключаем интерполяцию для всех вызовов.

**Память [[mcp-env-delivery]]:** Claude Code НЕ грузит `.env` сам; секреты доставляются как `uv --env-file .env` (голые имена в окружении). Поэтому ридер обязан работать и когда `.env`-файла нет, но переменные уже в `os.environ` — отсюда best-effort загрузка + `override=False`, а не «нет файла → сразу fail». Fail по AC #7 наступает только когда кредов нет **и** `.env` не найден.

### mypy и dotenv

`python-dotenv` (≥1.0, в зависимостях) поставляет inline-типы (`py.typed`), `from dotenv import load_dotenv` под `mypy --strict` проходит без оверрайдов. Если внезапно всплывёт `import-untyped` — добавить в `pyproject.toml` (по образцу, заложенному в 1.1 Dev Notes для duckdb/mcp):
```toml
[[tool.mypy.overrides]]
module = ["dotenv", "dotenv.*"]
ignore_missing_imports = true
```
но по умолчанию это не нужно — сперва проверить, что ошибка реально есть. Все функции (включая приватные `_load_env`/`_require`/`_coerce_counter_id`) аннотируются полностью; стаб `main()` тут не нужен — модуль не CLI.

`@dataclass(frozen=True, slots=True)` корректен на Python 3.13 и mypy strict. Валидацию counter_id держать **в `_coerce_counter_id` (снаружи), НЕ в `__post_init__`** — dataclass остаётся «глупым» контейнером, валидация — в функции-ридере. Не соблазняться переносить проверки в `__post_init__` (с `slots=True` + `frozen=True` это к тому же требует `object.__setattr__`-плясок).

### Project Structure Notes

- Модуль кладётся в `scripts/utils/env_reader.py` — ровно как в карте соответствия архитектуры (`auth_manager.py` → `env_reader.py`). Каталог `scripts/utils/` уже существует (регулярный пакет с `__init__.py` из 1.1) → `from scripts.utils.env_reader import read_metrica_credentials` резолвится.
- Импорты — абсолютные от корня пакета (`from scripts.utils.env_reader import ...`), не относительные. Инвариант проекта.
- Имена snake_case (модуль/функции/переменные), класс `MetricaCredentials` — CapWords. Type hints обязательны.
- `tests/` зеркалит `scripts/`: `tests/test_env_reader.py` (под `scripts/utils/env_reader.py`).
- Конфликтов со структурой нет; новых каталогов не создаётся (кроме `docs/` — заводится впервые в этом проекте, Task 6).

### Testing Requirements

- **Offline-набор (обязателен, в CI):** `tests/test_env_reader.py`, моки окружения через `monkeypatch`, `.env` — через `tmp_path`. Без сети, без реального `.env`. Изоляция окружения в fixture (чистить relevant env-vars), иначе тест зелёный/красный зависит от машины разработчика — кросс-платформенный CI (ubuntu+windows) это поймает.
- **Live-smoke — N/A для 1.2 (осознанно).** Правило project-context «тесты внешнего API → обязателен live-smoke» относится к компонентам, которые ходят в Logs API (`metrica_client` 1.3, оркестратор 2.7). `env_reader` — чистый локальный I/O по окружению, внешнего API-контракта у него нет. Поэтому отдельного `@pytest.mark.live` здесь не требуется; не трактовать его отсутствие как пробел при ревью.
- Покрыть **дисциплину**, не только happy path: пусто/пробелы (AC #5), мусорный counter (AC #6), отсутствие `.env` (AC #7), запрет Direct-fallback (AC #3), отсутствие тяжёлых импортов (AC #4).
- Запуск: `uv run pytest`, `uv run mypy scripts`.

### Definition of Done — чек-лист самопроверки

1. `scripts/utils/env_reader.py` создан; `from __future__ import annotations` первой строкой; модульный docstring на русском. (инвариант)
2. `read_metrica_credentials()` возвращает `MetricaCredentials(token: str, counter_id: int)` при валидных кредах. (AC #1)
3. Отсутствие любой переменной → `ValueError` с **именем** недостающей переменной, до сети. (AC #2)
4. Direct-fallback отсутствует: только `YANDEX_DIRECT_TOKEN`, без METRICA → ошибка про METRICA. (AC #3)
5. Нет импортов `ConfigManager`/`AuthManager`/`tapi_yandex_*`; зависимости — `python-dotenv` + stdlib. (AC #4)
6. Пустая строка/пробелы трактуются как отсутствие → fail-loud. (AC #5)
7. Нечисловой `YANDEX_METRICA_COUNTER_ID` → ошибка про целочисленность; `<= 0` → ошибка про положительность. (AC #6)
8. Нет `.env` и нет кредов → fail-loud с упоминанием `.env`/`GDAU_DATA_ROOT`; битый/несуществующий `GDAU_DATA_ROOT` + нет кредов → тоже fail-loud (трактуется как «`.env` не найден», без отдельной проверки каталога — граница с 2.1); при наличии кредов в процесс-окружении (без файла) — работает. (AC #7)
9. Все `load_dotenv` вызваны с `override=False` И `interpolate=False`; флаг `env_found` = OR двух вызовов; модуль импортируется без side-effects (тест). 
10. Креды не логируются (NFR-5; токен не в сообщениях/логах/`repr`); диагностика через stdlib `logging`, не `print`.
11. `docs/creds.md` заведён (выделенная спека компонента «креды и окружение»). (DoD project-context)
12. ✅ Имя `GDAU_DATA_ROOT` синхронизировано в `project-context.md`/`architecture.md`/`epics.md` (выполнено на ревью 2026-05-24) — для 2.1 единое имя.
13. `uv run mypy scripts` и `uv run pytest` — зелёные.

### Latest Tech Information

- `python-dotenv` ≥1.0 (в `uv.lock` — 1.2.2 на 2026-05-23, см. 1.1 Completion Notes); API `load_dotenv(dotenv_path=None, *, override=False)` стабилен. `override=False` (дефолт) = существующее `os.environ` не перезаписывается — это и нужно (precedence процесс-окружения).
- Отдельный web-ресёрч не требуется: версии зафиксированы локом, API ридера тривиален.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 1.2] — user story + 7 AC (усилены edge-case hunter).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-4] — env-ридер: `YANDEX_METRICA_TOKEN` + `_COUNTER_ID`; отсутствие → ошибка до сети; без Direct-fallback; `.env` во внешнем хранилище.
- [Source: _bmad-output/planning-artifacts/architecture.md#Authentication & Security (креды)] — тонкий env-ридер, инъекция в `MetricaClient` (развязка шва `AuthManager`), `.env` в per-game storage, fail-loud до сети.
- [Source: _bmad-output/planning-artifacts/architecture.md#directaiq → наш проект (карта соответствия)] — `auth_manager.py` → `env_reader.py` (тонкий ридер вместо AuthManager).
- [Source: _bmad-output/planning-artifacts/architecture.md#Швы вендоринга] — шов №1: конструктор `MetricaClient` зовёт `AuthManager` → инжектировать готовые креды.
- [Source: _bmad-output/project-context.md#Logs API и креды] — `env_reader`: `YANDEX_METRICA_TOKEN` + `_COUNTER_ID`, без Direct-fallback, инжект в клиент; нет токена/счётчика → понятная ошибка ДО сетевых вызовов; креды не логировать.
- [Source: _bmad-output/project-context.md#Language-Specific Rules] — `from __future__ import annotations`, type hints везде, абсолютные импорты, stdlib `logging` не `print`, docstrings на русском.
- [Source: G:/git/directaiq/scripts/utils/auth_manager.py#get_metrica_credentials] — оригинал с Direct-fallback + ConfigManager (что НЕ переносим); строки 376–393, 327–354.
- [Source: G:/git/directaiq/scripts/utils/metrica_client.py:134-147] — конструктор directaiq зовёт `AuthManager` изнутри (шов, развязываемый в 1.3).
- [Source: G:/git/directaiq/scripts/utils/paths.py:38-58] — паттерн загрузки `.env` (`load_dotenv` cwd + external `DIRECTAIQ_DATA_ROOT/.env`); наш аналог — `GDAU_DATA_ROOT`, `override=False`.
- [Source: _bmad-output/implementation-artifacts/1-1-uv-каркас-раскладка-проекта-и-ci.md#Testing Requirements] — mypy strict, `explicit_package_bases`, паттерн mypy-overrides для untyped-импортов.
- [Memory: directaiq-vendor-source] — `G:\git\directaiq` источник вендоринга; `auth_manager.py` связан с `tapi_yandex_direct`/`ConfigManager` → вендорить тонкий ридер вместо него.
- [Memory: mcp-env-delivery] — Claude Code не грузит `.env`; доставка через `uv --env-file .env` (голые имена в окружении) → ридер должен работать и без файла, если переменные в окружении.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Claude Code, dev-story workflow)

### Debug Log References

- `uv run python -c "from importlib.metadata import version; ...; print(version('python-dotenv'))"` — подтвердил `python-dotenv` 1.2.2 и сигнатуру `load_dotenv(..., override=False, interpolate=True, ...) -> bool` ДО написания кода (правило `interpolate=False` опирается на дефолт `True`).
- `uv run pytest tests/test_env_reader.py` — RED подтверждён (`ModuleNotFoundError: scripts.utils.env_reader`) до реализации → GREEN (18 passed) после.
- `uv run mypy scripts` → `Success: no issues found in 8 source files` (strict, без оверрайдов для `dotenv` — `py.typed` присутствует, доп. секция в `pyproject.toml` не понадобилась).
- `uv run pytest` (полный) → 20 passed (18 новых + 2 смоук 1.1), регрессий нет.

### Completion Notes List

- **Реализован тонкий env-ридер** `scripts/utils/env_reader.py`: `read_metrica_credentials() -> MetricaCredentials(token: str, counter_id: int)`. Зависимости — только stdlib (`os`/`pathlib`/`dataclasses`/`logging`) + `dotenv.load_dotenv`. Direct-fallback и `ConfigManager`/`AuthManager`/`tapi_yandex_*` отсутствуют (AC #3, #4).
- **Все `load_dotenv` вызваны с `override=False` и `interpolate=False`**; `_load_env()` возвращает OR двух флагов (storage `.env` + walk-up cwd) для диагностики AC #7. Никаких side-effects на уровне модуля (проверено тестом через `importlib.reload`).
- **Валидация дисциплины:** пусто/пробелы = отсутствие (AC #5); `counter_id` → строго положительный `int`, иначе понятный `ValueError` (AC #6, ловит и `int("abc")`, и `-5`/`0`); сообщение `_require` зависит от `env_found` и упоминает `.env`/`GDAU_DATA_ROOT` (AC #7). Тип исключения — `ValueError` (контракт с 1.6). Токен не логируется и не попадает в сообщения (NFR-5); `repr` counter_id допущен.
- **Тесты** `tests/test_env_reader.py` — 18 кейсов, autouse-fixture с `delenv` + `monkeypatch.chdir(tmp_path)` (защита от подхвата реального `.env` walk-up'ом). Тест AC #4 — через `ast`-разбор import-узлов (не подстрока — docstring содержит слово `auth_manager`). Тест «импорт без side-effects» через `importlib.reload`. Live-smoke для 1.2 не требуется (чистый локальный I/O, без внешнего API-контракта).
- **Спека компонента** `docs/creds.md` заведена впервые в проекте (выделенный компонент «креды и окружение» по решению Шефа 2026-05-24 — не часть `ingestion.md`).
- **Синхронизация имени `GDAU_DATA_ROOT`** уже была выполнена на этапе ревью (2026-05-24) — код использует ровно это имя; переделок не требовалось.
- Велась в ветке `story/1.2-env-reader-creds` (новая история → новая ветка).

### File List

- `scripts/utils/env_reader.py` (новый) — модуль env-ридера.
- `tests/test_env_reader.py` (новый) — offline-тесты (18 кейсов, AC #1–#7).
- `docs/creds.md` (новый) — спека компонента «креды и окружение».
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (изменён) — статус 1-2 → in-progress → review.
- `_bmad-output/implementation-artifacts/1-2-env-ридер-кредов-метрики.md` (изменён) — чекбоксы, Dev Agent Record, статус.

## Change Log

| Дата | Изменение |
|---|---|
| 2026-05-24 | Реализована story 1.2: `env_reader.py` (тонкий ридер кредов Метрики, без Direct-fallback и тяжёлых зависимостей), offline-тесты `test_env_reader.py` (18 кейсов, AC #1–#7), спека `docs/creds.md`. mypy strict + pytest зелёные (20 passed). Статус → review. |
| 2026-05-24 | Code review (3 слоя): AC/DoD удовлетворены. Применены патчи — cwd-поиск `.env` переведён на `find_dotenv(usecwd=True)` (cwd-relative, как обещает `creds.md`, и корректно в wheel); герметизация тестов (стаб `find_dotenv`, AST-проверка module-level dotenv вместо reload, +cwd-тест); `token=field(repr=False)` (NFR-5). mypy чисто, pytest 21 passed. Статус → done. |
