# Story 4.3: Init-команда `gdau-init` разворачивает хранилище

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want одну команду `gdau-init {game}`, разворачивающую per-game хранилище за один проход,
so that новая игра поднималась за минуты без ручной возни — готова к первой выгрузке, кроме токена/счётчика в `.env`.

**Контекст эпика.** ТРЕТЬЯ (финальная) история **Epic 4 «Развёртывание рабочего пространства игры (init)»** (FR-19/20/21) — **оркестратор**, склеивающий примитивы 4.1 и 4.2 в полный init. Эпик разворачивает per-game хранилище одной командой: проверка имени → копирование шаблона (**4.2**, `scaffold.copy_storage_template`) → симлинки по контракту + preflight (**4.1**, `symlinks.preflight_symlink_capability`/`create_symlinks`) → генерация `.env` → `uv sync` → создание `gdau.duckdb` + view'ы из каталога (**2.6**, `views.create_views`) → `git init` + initial commit. Истории Epic 4 — упорядочены «снизу вверх»: 4.1 — декларативный симлинк-контракт + механизм (примитив, **в `review`**), 4.2 — статический шаблон хранилища + `PROJECT.md` + примитив копирования (**в `ready-for-dev`**), **4.3 — оркестратор `gdau-init` (эта история)**. 4.3 покрывает **FR-19** (разворот одной командой из шаблона: имя → шаблон → симлинки → `.env` → окружение → БД+схема → `git init`; имя занято → fail-loud) и замыкает Epic 4 (SM-3 «новая игра одной командой», UJ-1).

**Что именно даёт 4.3.** 4.3 наполняет **стаб** `scripts/init/init_project.py` (заведён 1.1, печатает «not yet implemented», exit 0) **тонким оркестратором**: argparse-CLI `gdau-init {game}` + последовательность шагов поверх готовых примитивов. 4.3 **НЕ переписывает** механику симлинков (4.1), копирования (4.2), DDL-view'ов (2.6) — она их **вызывает** в правильном порядке, добавляя ровно то, что есть только на уровне оркестрации: (1) **валидацию имени игры** строгим шаблоном (AC #7); (2) **резолюцию пути** `../{game}` от корня dev-репо, не от cwd (AC #11/edge); (3) **генерацию `.env`** из `.env.example` (4.2) с подстановкой `GDAU_DATA_ROOT` (AC #1); (4) **`uv sync`** в хранилище (AC #1, #9); (5) **создание `gdau.duckdb` + view'ы** (AC #1, AC #14); (6) **`git init` + initial commit** без `.env` (AC #1, #4, #8, #13); (7) **полный откат всего хранилища** при сбое любого шага (AC #6) — граница, которую 4.1/4.2 примитивы намеренно НЕ делают. Это форма directaiq (`scripts/nushell/init_project.nu`), **переписанная на Python** (architecture.md:427, кросс-платформенно Win↔Linux, AC #5) с осознанными расхождениями (нет миграций/`activate.sh`/shared-venv/`SKIP_AUTO_MIGRATE`; схема = view'ы из каталога, не `migrate.py`; цели симлинков относительные; полный откат).

**Кто это потребляет.** `gdau-init` — **листовая** команда: её запускает оператор/владелец при заведении новой игры (entry point `gdau-init = scripts.init.init_project:main`, pyproject.toml:20). Прямых программных потребителей у `main()` нет. Сама 4.3 — **верхний потребитель** примитивов 4.1/4.2/2.6/2.1 (database_manager). После `gdau-init` хранилище читают/пишут приём (Epic 2, `gdau-logs update` из хранилища) и MCP-чтение (Epic 3) — но это уже рантайм, не init.

### ⚠️ Жёсткая зависимость порядка (прочитать ПЕРВЫМ — иначе dev 4.3 не соберётся)

4.3 **импортирует и вызывает** код 4.1 и 4.2. На момент создания этой истории:

- **4.1 (`scripts/init/symlinks.py`)** — статус `review`, существует в рабочем дереве ветки `story/4.1-...`, **НЕ слита в `main`**. 4.3 импортирует `preflight_symlink_capability`, `create_symlinks` (+ `SymlinkPreflightError`/`SymlinkContractError`/`SymlinkError`).
- **4.2 (`scripts/init/scaffold.py` + `templates/external_storage/`)** — статус `ready-for-dev`, **код ещё НЕ написан** (только файл истории). 4.3 импортирует `copy_storage_template` (+ `StorageTemplateError`) и копирует `templates/external_storage/` (4 файла).

**Следствие:** 4.3 нельзя начинать, пока **4.1 и 4.2 не реализованы и не доступны в рабочем дереве** ветки 4.3 (в идеале — слиты в `main`, и ветка 4.3 ответвлена от обновлённого `main`). Если на старте dev-story 4.3 `scripts/init/scaffold.py` или `templates/external_storage/` отсутствуют — **СТОП**: сначала закрыть 4.2 (и 4.1), затем вернуться к 4.3. Не дублировать копирование/симлинки внутри 4.3 — это нарушит границы и DoD эпика.

### Главные риски / решения (читать до кода)

> **Делегирование.** Шеф делегирует решения этой истории по принципу [[feedback-decide-and-apply]] («реши сам, главное чтобы работало надёжно»). Решения D1–D12 ниже **зафиксированы** и реализуются как описано; в спорной точке выбран более строгий/переносимый вариант (project-context: «в спорной ситуации — более строгий»). Уточняющие (не блокирующие) вопросы — в конце файла.

1. **D1 — 4.3 = ТОНКИЙ оркестратор в `init_project.py`; примитивы не дублируются.** `main()` + класс/функции оркестрации в `scripts/init/init_project.py` (заменяет стаб). Вся механика — в вызовах: `scaffold.copy_storage_template` (4.2), `symlinks.preflight_symlink_capability`/`create_symlinks` (4.1), `database_manager.DatabaseManager.connection` (2.1), `views.create_views` (2.6). 4.3 добавляет ТОЛЬКО оркестрацию: имя→путь→preflight→копия→симлинки→`.env`→`uv sync`→БД+view'ы→git, **полный откат** при сбое. CLI — stdlib `argparse` форма directaiq (класс `InitCLI` или функции + `_create_parser` + `main`), как `logs_api_cli.py`; единственный позиционный аргумент `game`. Type hints везде (mypy strict, без `Any`-дыр); `from __future__ import annotations` первой строкой; русский модульный docstring (роль + границы + расхождения с directaiq); идентификаторы английские, docstrings русские; `logger = logging.getLogger(__name__)`. Сообщения о шагах — через `logging` (INFO), не `print` (стаб печатал — это убрать). Успех → exit `0`; любой fail → ненулевой код + понятное сообщение (как `logs_api_cli.main`).

2. **D2 — Резолюция пути `../{game}` ОТ КОРНЯ dev-репо, не от cwd (AC edge epics.md:487).** `dev_repo_root = Path(__file__).resolve().parents[2]` (`init_project.py → init → scripts → корень dev-репо`; `.resolve()` проходит сквозь симлинк, как `symlinks.DEFAULT_CONTRACT_PATH`/`catalog.DEFAULT_CATALOG_PATH`). `storage_root = dev_repo_root.parent / game`. **Никогда** не резолвить от `os.getcwd()` — иначе запуск из произвольного каталога увёл бы хранилище не туда. Это «сосед dev-репо» (`../{game}`), как в directaiq (`$"../($name)"`).

3. **D3 — Валидация имени игры строгим шаблоном ДО любых действий (AC #7).** Отвергнуть fail-loud: пустое/пробельное; содержащее разделители пути (`/`, `\`, `os.sep`/`os.altsep`) или `..`/`.`; ведущую точку; пробелы; спецсимволы (разрешены `[A-Za-z0-9_-]`); зарезервированные Windows-имена (case-insensitive: `CON`, `PRN`, `AUX`, `NUL`, `COM1..9`, `LPT1..9`); слишком длинное (> 64 символов — запас под путь). Рекомендуемый шаблон: `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` + отдельная проверка набора reserved-имён. Имя — это и имя каталога, и (через `.gitignore`/симлинки) часть путей → строгость защищает ФС обеих ОС. Понятное сообщение с указанием допустимого формата. (Сверять с `storage_name` snake_case-духом, но имена игр могут быть не-snake — главное безопасность ФС.)

4. **D4 — Имя занято → fail-loud БЕЗ перезаписи (AC #2).** Если `storage_root` (`../{game}`) уже существует (файл/каталог/симлинк) → `StorageInitError` ДО любых мутаций (после валидации имени, до preflight-проб и копирования). Никогда не перезаписывать чужие данные (directaiq: `path exists → error make`). Это делает ветку «повторный init поверх заполненного `PROJECT.md`» (4.2 AC #6) **не-триггеримой** в штатном потоке 4.3 — согласовано с примечанием 4.2 (C1): AC #6 примитива защищает refresh/resume, не штатный init.

5. **D5 — ПОЛНЫЙ откат всего хранилища при сбое любого шага (AC #6, epics.md:482/486/488).** 4.3 владеет откатом, которого примитивы 4.1/4.2 намеренно НЕ делают (их граница). Механика: запомнить `storage_root` и флаг «создан этим запуском»; любой сбой ПОСЛЕ создания каталога хранилища → `shutil.rmtree(storage_root, ...)` (снять всё: шаблон, симлинки, `.env`, `gdau.duckdb`, `.venv`, `.git`). **КРИТИЧНО (anti-disaster):** `shutil.rmtree` по симлинкам внутри дерева вызывает `os.unlink` (НЕ рекурсирует в цель) → инфра-симлинки на dev-репо снимаются, но **код/каталог/`yandex-docs` dev-репо НЕ удаляются**. Это нативное поведение `rmtree`, но проверить тестом (симлинк на tmp-«dev-репо» → rollback не трогает цель). Откат — best-effort под `suppress`/`ignore_errors=True` на под-сбоях, но если каталог не удалось снять полностью — лог WARNING + понятная ошибка «остаток `{path}`, удали вручную и повтори» (AC #11 resume). Поскольку D4 гарантирует, что `storage_root` создан ИМЕННО этим запуском (не пред-существовал), полное удаление безопасно — данных владельца там ещё нет (токен он вписывает ПОСЛЕ init, AC #3). Кросс-стори флаг 4.2 (не сносить заполненный `PROJECT.md`) в штатном 4.3 неактуален (D4), но **зафиксировать**: если когда-нибудь появится resume/refresh-поток, откат не должен удалять заполненный владельцем `PROJECT.md`.

6. **D6 — Порядок шагов: preflight'ы ДО создания хранилища (минимизировать откат).** Финальная последовательность `main`:
   1. **Валидация имени** (D3) — чистая, без ФС.
   2. **Резолюция `storage_root`** (D2); **проверка «имя свободно»** (D4, fail-loud если есть).
   3. **Preflight окружения** (epics.md:485): `shutil.which("git")` — нет → fail-loud с инструкцией установить git; `shutil.which("uv")` — нет → fail-loud (хотя при запуске `uv run gdau-init` uv заведомо есть; проверка для robustness/AC #9). **Preflight симлинков** (4.1 `preflight_symlink_capability()`) — Windows без Dev Mode → `SymlinkPreflightError` с инструкцией. **Все preflight'ы — ДО создания каталога** → непригодная платформа падает «насухо», откат не нужен.
   4. **Копирование шаблона** (4.2 `copy_storage_template(storage_root=storage_root)`) — создаёт `storage_root` + 4 файла. С этого момента включается откат (D5).
   5. **Симлинки** (4.1 `create_symlinks(dev_repo_root=dev_repo_root, storage_root=storage_root, run_preflight=False)` — preflight уже сделан на шаге 3, не повторять).
   6. **Генерация `.env`** (D7).
   7. **`uv sync`** (D8).
   8. **`gdau.duckdb` + view'ы** (D9).
   9. **`git init` + commit** (D10).
   - Любой сбой шагов 4–9 → откат (D5) + проброс понятной ошибки. Сбой шагов 1–3 → просто fail-loud (хранилища ещё нет).

7. **D7 — Генерация `.env` из `.env.example` + `GDAU_DATA_ROOT` (AC #1, #3).** Скопировать `storage_root/.env.example` → `storage_root/.env` (он уже скопирован шагом 4 в составе шаблона) и **дописать/задать** строку `GDAU_DATA_ROOT={abs storage_root}` (абсолютный путь — `paths.get_storage_root` требует absolute; машинно-специфичен, но `.env` не коммитится и не переносится — приемлемо, как directaiq `DIRECTAIQ_DATA_ROOT=<abs>`). Токен/счётчик в `.env` остаются **пустыми плейсхолдерами** из `.env.example` — владелец вписывает после init (AC #3 «без ручных правок, кроме токена/счётчика»). Реализация просто: прочитать `.env.example` (или взять скопированный), записать его содержимое + строку `GDAU_DATA_ROOT=...` в `.env`. **Не** генерировать токены, **не** логировать содержимое `.env`. `.env` НЕ коммитится (D10). Имена переменных — из `env_reader` (`TOKEN_ENV`/`COUNTER_ENV`/`DATA_ROOT_ENV`), не литералы (рассинхрон = тихий баг). _Замечание по переносимости (NFR-2): абсолютный `GDAU_DATA_ROOT` в `.env` ломается при переносе папки на другую машину; это документируется (re-run init или правка одной строки), как у directaiq — секреты/`.env` всё равно не переносят копированием._

8. **D8 — `uv sync --frozen` в хранилище; uv.lock ДОБАВЛЯЕТСЯ в симлинк-контракт (AC #1, #9; см. D11).** Хранилище — самостоятельное рабочее пространство: оператор запускает `gdau-logs`/MCP из него → нужен `.venv` в хранилище. `subprocess.run(["uv", "sync", "--frozen"], cwd=storage_root, ...)`. **`--frozen` обязателен**: (а) CI-консистентность (CLAUDE.md: «CI: `uv sync --frozen`»); (б) **uv.lock приходит СИМЛИНКОМ** на dev-репо (D11) → `uv sync` без `--frozen` попытался бы ПЕРЕЗАПИСАТЬ lock сквозь симлинк (испортив dev-репо или упав) — `--frozen` только читает. `uv sync` создаёт `storage/.venv` (игнорится `.gitignore` шаблона 4.2). Ненулевой код/нет сети/нет `uv` → захватить stdout+stderr, понятная ошибка + **откат** (D5, AC #9). Таймаут разумный (напр. 300s) против вечного зависания. _Для этого uv.lock ОБЯЗАН быть симлинкнут (см. D11) — без него `uv sync --frozen` упадёт «no lockfile»._

9. **D9 — `gdau.duckdb` + view'ы IN-PROCESS под `.writer.lock`; `GDAU_DATA_ROOT` инъектируется в `os.environ` (AC #1, #14).** Финальный шаг создаёт базу и типизированные view'ы из каталога. Реализация **в текущем процессе** (проще directaiq-subprocess; всё импортируемо):
   - **Инъекция корня:** `paths.get_storage_root` читает `GDAU_DATA_ROOT` ТОЛЬКО из `os.environ` (нет шва-параметра) → перед шагом установить `os.environ[env_reader.DATA_ROOT_ENV] = str(storage_root)`. Это единственный способ сказать `paths`/`database_manager`/`views`/`writer_lock`, где хранилище (они инъекции через параметр не принимают). `storage_root` уже существует и абсолютен → `get_storage_root().resolve()/is_dir()` пройдут.
   - **Лок:** `views.py` docstring требует, чтобы DDL (`CREATE OR REPLACE VIEW` пишет в каталог БД) шёл под `.writer.lock` (как p81 2.7). Обернуть создание БД+view'ов в `writer_lock.writer_lock()` (2.5; неблокирующий, на свежем хранилище заведомо свободен). _Строгий вариант ради инварианта «запись только под локом»; на брэнд-новом хранилище конкурента нет, но консистентность важнее (project-context)._
   - **Создание:** `with DatabaseManager.connection(read_only=False) as conn:` (2.1 — write-режим создаёт `data/duckdb/` родителя и сам файл `gdau.duckdb`) → `views.create_views(conn)` (2.6 — DDL из каталога; партиций нет → `has_partitions=False` → пустые типизированные view'ы `visits`/`hits`, AC #14 / epics.md:490 / 2.6 AC #6). Каталог `views`/`database_manager` берут из dev-репо (симлинк `development-docs`/`scripts`).
   - Сбой (битый каталог, ошибка DuckDB) → понятная ошибка + откат (D5). _Альтернатива subprocess (`uv run python -c …` в хранилище, как directaiq) НЕ выбрана: in-process проще, тестируемее, без зависимости от готовности `.venv` хранилища до этого шага (хотя `uv sync` уже прошёл — in-process использует интерпретатор dev-репо, что корректно: каталог/код те же)._

10. **D10 — `git init` + initial commit в хранилище, `.env` исключён (AC #1, #4, #8, #13).** `cwd=storage_root` (D2 — сосед dev-репо, НЕ вложен в его рабочее дерево; `git init` создаёт независимый `storage/.git` локально для каталога). Шаги (форма directaiq `init-git-repo`, на Python через `subprocess`):
    - Если `storage_root/.git` уже есть (resume) → пропустить `git init`, но всё равно add+commit (idempotent).
    - `git init` (cwd=storage); сбой → ошибка + откат.
    - `git add -A`.
    - **`git reset -- .env`** (или `git rm --cached --quiet .env` если попал) — `.env` НЕ в initial commit (AC #4; secrets fail-loud-видимы как «ждёт намеренного add», паттерн directaiq). `.env.example` остаётся staged. _`.gitignore` шаблона (4.2) и так игнорит `.env`, но явный reset — пояс-и-подтяжки против случайного коммита._
    - Проверить, что есть staged-файлы (`git diff --cached --quiet` → если ничего, не коммитить — но шаблон даёт 4 файла минус `.env` → коммит непуст, AC #13); `git commit -m "init: развёртывание хранилища игры {game}"` (Conventional-ish, русское описание — соблюсти стиль репо).
    - **Git-вложенность (AC #8):** хранилище — `../{game}` (сосед), не внутри dev-репо → `git init` изолирован. Если по какой-то причине `git rev-parse --show-toplevel` из `storage_root` вернул бы АНЦЕСТОРА (хранилище внутри чужого репо) — `git init` всё равно создаёт вложенный независимый репо в `{game}` (git это поддерживает); распознать «уже репо» по наличию `storage/.git`. Не путать рабочее дерево dev-репо с хранилищем.
    - **git preflight** уже на шаге D6.3 (`shutil.which("git")`).

11. **D11 — Финализация симлинк-контракта: ДОБАВИТЬ `uv.lock`; `.mcp.json`/`.claude` ОТЛОЖИТЬ (FR-20, architecture.md:599).** 4.1 (решение D5) отложило финализацию состава `templates/paths-to-symlink.csv` до 4.3 («`.mcp.json` [Epic 3] и `.claude/*` дописываются при появлении в 4.3»). Решение 4.3:
    - **ДОБАВИТЬ `uv.lock`** в контракт (новая строка `uv.lock,файл блокировки зависимостей (uv) — единый лок на все игры`). Обоснование: `uv sync --frozen` в хранилище (D8) ТРЕБУЕТ lock; `uv.lock` существует в dev-репо, стабилен, авторитетен (project-context: «uv.lock коммитится и авторитетен»). Без симлинка `uv sync --frozen` упал бы.
    - **НЕ добавлять `.mcp.json`**: артефакт Epic 3 (story 3.1 `ready-for-dev`, **не слит в `main`, отсутствует на этой ветке**). `create_symlinks` предвалидирует существование ВСЕХ целей (4.1: `SymlinkTargetMissingError` если цели нет) → добавление `.mcp.json` сейчас сделало бы `gdau-init` нерабочим (падал бы на отсутствующей цели). Допишется одной строкой в CSV, когда Epic 3 сольётся в `main` и `.mcp.json` появится в корне dev-репо — **без правки кода** (в этом суть декларативного FR-20).
    - **НЕ добавлять `.claude`** сейчас: финализировать вместе с `.mcp.json` как «рантайм агента» одной порцией, когда MCP-канал замкнётся (Epic 3). `.claude/` в dev-репо сейчас несёт лишь `skills/` (BMad-скилы dev-процесса, не рантайм игры) — преждевременно связывать.
    - **Синхрон `.gitignore` шаблона (4.2):** `.gitignore` шаблона (4.2 решение D3) перечисляет симлинк-пути инфры — **добавить туда `uv.lock`** (чтобы `git` хранилища не коммитил инфра-симлинк). Это кросс-стори правка файла 4.2; если 4.2 ещё не слита — синхронизировать при merge. Тест 4.1 `test_shipped_contract_loads_and_targets_exist` остаётся зелёным (`uv.lock` существует).
    - **Обновить `docs/init-and-storage.md`:** раздел про симлинки (4.1) сейчас пишет «`.mcp.json` и `.claude/…` добавятся… в 4.3»; уточнить: добавлен `uv.lock`; `.mcp.json`/`.claude` отложены до слияния Epic 3 (одна строка CSV, без кода).

12. **D12 — Тесты: оркестрация на `tmp_path` с инъекцией dev-репо-фикстуры; БЕЗ реального git/uv где можно; live НЕ нужен.** CI ubuntu + windows. Init трудно тестировать целиком (внешние `git`/`uv`), поэтому **проектировать `init_project.py` инъектируемым** (как примитивы): `dev_repo_root`/`storage_parent` — параметры функций оркестрации (дефолты — прод-резолюция), чтобы тест собрал мини-«dev-репо» на `tmp_path` (с минимальным `templates/external_storage/` + `templates/paths-to-symlink.csv` + целями контракта) и гонял шаги без записи в реальный dev-репо. Стратегия:
    - **Валидация имени (AC #7, чистая, всегда идёт):** таблица кейсов — валидные имена проходят; разделители/`..`/ведущая точка/пробел/спецсимвол/reserved (`CON`,`NUL`,`com1`…)/слишком длинное → `pytest.raises`. Без ФС.
    - **Резолюция пути (D2):** `storage_root == dev_repo_root.parent / game` (не от cwd) — проверить с разными cwd (`monkeypatch.chdir`).
    - **Имя занято (AC #2):** заранее создать `storage_root` → `gdau-init` fail-loud, существующее не тронуто.
    - **Полный откат (AC #6, D5):** замокать сбой на одном из шагов (напр. `uv sync` через инъекцию/`monkeypatch` подмены runner'а на бросающий) → `storage_root` удалён целиком; **критичный тест:** симлинк на tmp-«dev-репо»-цель внутри хранилища → после отката цель симлинка (содержимое «dev-репо») ЦЕЛА (`rmtree` снял ссылку, не цель).
    - **Симлинки/копирование/БД на `tmp_path`** (capability-gated для симлинков, как 4.1 — `pytest.skip` без Dev Mode на Windows; ubuntu даёт реальное покрытие): полный проход на мини-dev-репо → хранилище создано, 4 файла шаблона на месте, симлинки указывают в dev-репо, `.env` содержит `GDAU_DATA_ROOT` и пустой токен, `gdau.duckdb` создан, view'ы `visits`/`hits` существуют и пусты (типизированы). `git`/`uv` — либо реальные (если доступны в CI, проверять `shutil.which`), либо инъекция фейкового runner'а; **не** делать сетевых вызовов.
    - **`.env` без секретов:** сгенерированный `.env` содержит `GDAU_DATA_ROOT=<abs>` и `YANDEX_METRICA_TOKEN=`/`COUNTER_ID=` с пустыми значениями (не вписан реальный токен).
    - **git initial commit (AC #4, #13):** после прохода (если git доступен) — в `storage/.git` есть коммит; `git ls-files` НЕ содержит `.env`, содержит `.env.example`/`PROJECT.md`/`CLAUDE.md`/`.gitignore`; коммит непуст.
    - **Анти-зависимость (`ast`):** `init_project.py` не импортирует `pandas`/`polars`/`numpy`/`pyarrow`/directaiq-инфру (`config_manager`/`base_script`); импортирует ТОЛЬКО `scripts.init.scaffold`/`scripts.init.symlinks`/`scripts.utils.{database_manager,views,paths,env_reader,writer_lock}` + stdlib (`argparse`/`os`/`shutil`/`subprocess`/`re`/`logging`/`pathlib`). Приём — `tests/test_parquet_store.py` (ast по import-узлам).
    - **Live-тест НЕ нужен** (и не заводить): init — ФС/процессы (`git`/`uv`), без внешнего API; правило opt-in live — только для Logs API ([[realapi-smoke-tests]]). Зафиксировать в Dev Agent Record (как 2.1–2.6, 4.1, 4.2).
    - **Имя теста:** `tests/test_init_project.py` (зеркало `scripts/init/init_project.py`).

## Acceptance Criteria

1. **Given** `gdau-init {game}`, **When** выполняется, **Then** по шагам: проверка свободного имени → копирование шаблона (4.2) → симлинки по контракту (4.1, + preflight) → генерация `.env` → `uv sync` → создание `gdau.duckdb` + view'ы из каталога (2.6) → `git init` + initial commit.
2. **Given** имя занято / папка `../{game}` существует, **When** выполняется init, **Then** fail-loud без перезаписи чужих данных.
3. **Given** успешный init, **When** завершён, **Then** хранилище готово к первой выгрузке (FR-1) без ручных правок, кроме токена/счётчика в `.env`.
4. **Given** initial commit, **When** он делается, **Then** `.env` НЕ коммитится.
5. **Given** init на Python, **When** запускается на Windows и Linux, **Then** работает кросс-платформенно (без bash/nushell).
6. **Given** сбой посреди init (любой шаг), **When** он происходит, **Then** уже созданное хранилище/симлинки очищаются (откат), чтобы имя не оставалось «занятым» мусором и повтор был возможен. _[edge-case: частичный init → мусор]_
7. **Given** имя игры со спецсимволами/path-separator/пробелами/зарезервированными Windows-именами (`CON`,`NUL`,…)/ведущими точками/слишком длинное, **When** проверяется имя, **Then** валидация по строгому шаблону с понятным отказом. _[edge-case: опасное имя]_
8. **Given** dev-репо ещё не git-репо ИЛИ `{game}` оказывается вложенным в существующий git-репо, **When** делается `git init`, **Then** репо инициализируется только в `{game}`, вложенность/уже-репо распознаётся, без путаницы. _[edge-case: git-вложенность]_
9. **Given** `uv sync` падает / нет сети ИЛИ `git` не установлен, **When** это случилось, **Then** для `git` — preflight с инструкцией; для `uv sync` — отчёт + откат частичного хранилища. _[edge-case: провал зависимостей/git]_
10. **Given** повторный запуск init после частичного сбоя, **When** он стартует, **Then** поведение определено: чистый повтор (после очистки) либо явная ошибка с указанием очистить остаток. _[edge-case: resume/идемпотентность init]_
11. **Given** неоднозначность пути `../{game}` от cwd, **When** резолвится цель, **Then** она вычисляется относительно корня dev-репо (`dev_repo_root.parent / game`), а не от текущего каталога вызова. _[edge-case: резолюция пути от cwd]_
12. **Given** диск полон / нет прав на запись при копировании шаблона, **When** копирование падает, **Then** `OSError` ловится → очистка + понятная ошибка. _[edge-case: диск/права при копировании]_
13. **Given** initial commit (где `.env` исключён), **When** он делается, **Then** в индексе есть файлы шаблона (коммит не пустой) — нет ошибки «nothing to commit». _[edge-case: пустой initial commit]_
14. **Given** создание view'ов на свежем хранилище без данных, **When** init строит view'ы из каталога, **Then** они создаются, толерантно к нулю партиций (см. 2.6 пустой источник). _[edge-case: view'ы на пустом хранилище]_

> **Граница 4.3 с 4.1/4.2/2.6.** 4.3 НЕ реализует заново: механизм симлинков и preflight (4.1 `symlinks.py`), копирование шаблона и сохранение `PROJECT.md` (4.2 `scaffold.py`), DDL типизированных view'ов (2.6 `views.py`), открытие/закрытие БД (2.1 `database_manager.py`), захват лока (2.5 `writer_lock.py`). 4.3 ВЫЗЫВАЕТ их в правильном порядке и владеет: валидацией имени, резолюцией пути, генерацией `.env`, `uv sync`, `git init`, **полным откатом хранилища** и кодами возврата.

## Tasks / Subtasks

- [x] **Task 0 — Предусловие: убедиться, что 4.1 и 4.2 доступны (зависимость порядка)**
  - [x] Проверить наличие `scripts/init/symlinks.py` (4.1) и `scripts/init/scaffold.py` + `templates/external_storage/` (4 файла, 4.2) в рабочем дереве. Отсутствуют → **СТОП**, закрыть 4.2 (и слить 4.1) до начала 4.3. Не дублировать копирование/симлинки в 4.3.
  - [x] Ветка `story/4.3-init-command` от обновлённого `main` (где слиты 4.1/4.2). Новая история → новая ветка (project-context).
- [x] **Task 1 — `scripts/init/init_project.py`: оркестратор `gdau-init` (заменить стаб) (AC: #1, #3, #5)**
  - [x] `from __future__ import annotations` первой строкой. Русский модульный docstring: роль (оркестратор разворачивания per-game хранилища: имя → шаблон → симлинки → `.env` → `uv sync` → БД+view'ы → git); границы (примитивы 4.1/4.2/2.6/2.1/2.5 — вызываются, не дублируются; полный откат — здесь); расхождения с directaiq `init_project.nu` (Python не nushell; нет миграций/`activate.sh`/shared-venv/`SKIP_AUTO_MIGRATE`; схема = view'ы из каталога; цели симлинков относительные; полный откат хранилища). `logger = logging.getLogger(__name__)`. `__all__`.
  - [x] Импорты (D12 анти-зависимость): stdlib `argparse`/`logging`/`os`/`re`/`shutil`/`subprocess`/`from pathlib import Path`; `from scripts.init.scaffold import copy_storage_template, StorageTemplateError`; `from scripts.init.symlinks import preflight_symlink_capability, create_symlinks, SymlinkPreflightError, SymlinkContractError, SymlinkError`; `from scripts.utils.database_manager import DatabaseManager`; `from scripts.utils.views import create_views`; `from scripts.utils import env_reader` (для `DATA_ROOT_ENV`/`TOKEN_ENV`/`COUNTER_ENV`); `from scripts.utils.writer_lock import writer_lock`. **НЕ** импортировать `pandas`/`polars`/`numpy`/`pyarrow`/directaiq-инфру.
  - [x] Исключение: `class StorageInitError(RuntimeError): ...` — инцидент оркестрации init (имя занято/невалидное, сбой шага). Сырьё (`OSError`/`subprocess`-сбой/`duckdb.Error`) оборачивать в него с путём/контекстом; никогда «голый» наружу (паттерн 2.1/4.1/4.2).
  - [x] Константы: `RESERVED_WINDOWS_NAMES` (frozenset: `CON`,`PRN`,`AUX`,`NUL`,`COM1..9`,`LPT1..9`), `NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")`, `UV_SYNC_TIMEOUT = 300` (сек).
- [x] **Task 2 — Валидация имени игры (AC: #7)**
  - [x] `_validate_game_name(name: str) -> str`: trim; пустое/пробельное → `StorageInitError`; содержит `os.sep`/`os.altsep`/`/`/`\`/`..` → ошибка; не матчит `NAME_PATTERN` (ведущая точка/пробел/спецсимвол/длина) → ошибка; `name.upper()` в `RESERVED_WINDOWS_NAMES` (case-insensitive) → ошибка. Понятное сообщение с допустимым форматом. Вернуть валидное имя.
- [x] **Task 3 — Резолюция пути и проверка «имя свободно» (AC: #2, #11)**
  - [x] `_resolve_dev_repo_root() -> Path`: `Path(__file__).resolve().parents[2]` (инъекция в тестах — параметр с дефолтом).
  - [x] `storage_root = dev_repo_root.parent / game` (D2 — НЕ от cwd).
  - [x] `storage_root.exists()` (включая симлинк/файл) → `StorageInitError` «имя занято, выбери другое или удали `{path}`» (AC #2), ДО любых мутаций/preflight'ов.
- [x] **Task 4 — Preflight окружения и симлинков ДО создания хранилища (AC: #5, #8-git, #9)**
  - [x] `shutil.which("git")` is None → `StorageInitError` с инструкцией установить git (AC #9 git-ветка — preflight).
  - [x] `shutil.which("uv")` is None → `StorageInitError` (AC #9; robustness).
  - [x] `preflight_symlink_capability()` (4.1) — Windows без Dev Mode → `SymlinkPreflightError` (перехватить/пробросить как понятный fail; инструкция уже в сообщении 4.1). ДО создания каталога (откат не нужен на этой ветке).
- [x] **Task 5 — Копирование шаблона + симлинки + полный откат (AC: #1, #6, #12)**
  - [x] Реализовать контекст отката: после `copy_storage_template` запомнить `storage_root` как «создан этим запуском»; обернуть шаги 4.5–4.9 в `try/except`, на любом исключении → `_rollback(storage_root)` + проброс `StorageInitError` с контекстом.
  - [x] `copy_storage_template(storage_root=storage_root)` (4.2, дефолтный `template_root`) — создаёт каталог + 4 файла; `StorageTemplateError`/`OSError` (диск/права, AC #12) → откат + понятная ошибка.
  - [x] `create_symlinks(dev_repo_root=dev_repo_root, storage_root=storage_root, run_preflight=False)` (4.1 — preflight уже на Task 4, `run_preflight=False`). `SymlinkContractError`/`SymlinkError` → откат. _(create_symlinks сам откатывает свои симлинки, но полный откат хранилища — здесь.)_
  - [x] `_rollback(storage_root)`: `shutil.rmtree(storage_root, ignore_errors=False)` под `try`; **КРИТИЧНО** — rmtree снимает инфра-симлинки `os.unlink`'ом, не рекурсируя в цель (dev-репо цел); если rmtree упал — лог WARNING + не маскировать исходную ошибку, в финальном сообщении указать «остаток `{path}`, удали вручную» (AC #10).
- [x] **Task 6 — Генерация `.env` (AC: #1, #3)**
  - [x] `_write_env(storage_root)`: взять содержимое `storage_root/.env.example` (скопирован шаблоном), записать его в `storage_root/.env` + добавить строку `f"{env_reader.DATA_ROOT_ENV}={storage_root}"` (абсолютный путь). Токен/счётчик — пустые плейсхолдеры из `.env.example` (не заполнять). Не логировать содержимое. `OSError` → откат.
  - [x] Гарантировать, что `.env` НЕ перезаписывает уже заполненный (в штатном потоке его нет — D4; но при будущем resume — беречь). В штатном init `.env` создаётся впервые.
- [x] **Task 7 — `uv sync --frozen` в хранилище (AC: #1, #9)**
  - [x] `_uv_sync(storage_root)`: `subprocess.run(["uv", "sync", "--frozen"], cwd=storage_root, capture_output=True, text=True, timeout=UV_SYNC_TIMEOUT)`. Ненулевой код/`TimeoutExpired`/`FileNotFoundError` → захватить stderr → `StorageInitError` + откат (AC #9). _Требует симлинк `uv.lock` (Task 9, D11) — иначе `uv sync --frozen` упадёт «no lockfile»._
- [x] **Task 8 — Создание `gdau.duckdb` + view'ы под локом (AC: #1, #14)**
  - [x] `_create_database(storage_root)`: `os.environ[env_reader.DATA_ROOT_ENV] = str(storage_root)` (инъекция корня для `paths`/`database_manager`/`views`/`writer_lock` — читают из env). Затем `with writer_lock():` (2.5) → `with DatabaseManager.connection(read_only=False) as conn:` (2.1 — создаёт `data/duckdb/gdau.duckdb`) → `create_views(conn)` (2.6 — пустые типизированные view'ы, нет партиций, AC #14). `ValueError` (битый каталог)/`duckdb.Error`/`RuntimeError` → `StorageInitError` + откат.
  - [x] _Замечание: установка `os.environ` мутирует процесс-окружение одноразового CLI — приемлемо. Документировать как точку инъекции (paths не принимает параметр-шов)._
- [x] **Task 9 — Финализация симлинк-контракта: добавить `uv.lock` (AC: #1; D11, FR-20)**
  - [x] Дописать в `templates/paths-to-symlink.csv` строку: `uv.lock,файл блокировки зависимостей (uv) — единый лок на все игры` (RFC4180 — без лишних запятых в comment, или закавычить). `.mcp.json`/`.claude` НЕ добавлять (отсутствуют/Epic 3).
  - [x] Синхрон: добавить `/uv.lock` в симлинк-секцию `templates/external_storage/.gitignore` (артефакт 4.2; если 4.2 не слита — при merge). Комментарий «синхрон с `paths-to-symlink.csv`».
  - [x] Обновить `docs/init-and-storage.md` (раздел симлинков 4.1): уточнить, что добавлен `uv.lock`; `.mcp.json`/`.claude` отложены до слияния Epic 3 (одна строка CSV, без кода).
  - [x] Тест 4.1 `test_shipped_contract_loads_and_targets_exist` остаётся зелёным (`uv.lock` существует) — прогнать регрессию.
- [x] **Task 10 — `git init` + initial commit (AC: #1, #4, #8, #13)**
  - [x] `_git_init_commit(storage_root, game)` (cwd=storage_root через `subprocess.run(..., cwd=storage_root)`):
    - `storage_root/.git` уже есть → пропустить `git init` (resume), иначе `git init`.
    - `git add -A`.
    - `git reset -- .env` (исключить `.env` из индекса; AC #4) — даже если `.gitignore` его игнорит (пояс-и-подтяжки).
    - `git diff --cached --quiet` → если есть staged (шаблон минус `.env`, непусто, AC #13) → `git commit -m "init: развёртывание хранилища игры {game}"`.
    - Любой сбой `git` → `StorageInitError` + откат. Вложенность (AC #8): `git init` в `storage_root` изолирован (сосед dev-репо); распознать «уже репо» по `storage/.git`.
- [x] **Task 11 — `main()` + argparse CLI (AC: #1, #5)**
  - [x] `_create_parser() -> argparse.ArgumentParser`: единственный позиционный `game` (имя игры). Без `--format` (как `logs_api_cli`).
  - [x] `main() -> None`: распарсить → последовательность D6 (валидация → путь → preflight → копия → симлинки → `.env` → `uv sync` → БД+view'ы → git) с откатом → INFO-лог финального успеха («хранилище `{path}` готово; впиши токен/счётчик в `.env` и запусти `gdau-logs update`»). Любой `StorageInitError`/`Symlink*Error`/`StorageTemplateError` → лог ERROR + `raise SystemExit(1)` (ненулевой код, понятное сообщение; без «голого» трейсбека). `KeyboardInterrupt` → откат + `SystemExit(130)` (как 2.9). Успех → неявный exit 0. `if __name__ == "__main__": main()`.
- [x] **Task 12 — Спека `docs/init-and-storage.md` (дополнение раздела, часть DoD)**
  - [x] Добавить раздел `## Полный разворот командой `gdau-init`` (3-вопросный каркас, как соседние): **(1) Что делает** — одной командой `gdau-init {game}` создаёт рядом с dev-репо папку игры из шаблона, связывает её ссылками с инструментом, генерирует файл кредов с путём к хранилищу, ставит окружение, создаёт пустую базу с представлениями `visits`/`hits` и заводит для папки игры отдельный git; **(2) Зачем** — чтобы новая игра поднималась за минуты без ручной возни; готова к первой выгрузке, владельцу остаётся вписать токен и счётчик; **(3) Контракт** — имя проверяется строгим шаблоном; занятое имя → остановка без перезаписи; сбой на любом шаге → папка игры **полностью убирается** (имя снова свободно, повтор чистый); `.env` (с будущими секретами) в git игры не попадает. **Границы:** механизм симлинков — 4.1; шаблон/`PROJECT.md` — 4.2; этот раздел — про сборку всего вместе. Обновить вводную (строки 5–7) и раздел симлинков (D11: `uv.lock` добавлен, `.mcp.json`/`.claude` отложены). Человеческим языком, без жаргона/сигнатур.
- [x] **Task 13 — Offline-тесты `tests/test_init_project.py` (AC: #2, #4, #6, #7, #11, #13, #14; D12)**
  - [x] `from __future__ import annotations`; зеркалит `scripts/init/init_project.py`; кросс-платформенно (`tmp_path`/`pathlib`); CI ubuntu + windows. Инъекция `dev_repo_root`/`storage_parent` параметрами.
  - [x] Валидация имени (AC #7, всегда идёт): валидные ↔ невалидные (разделители/`..`/ведущая точка/пробел/спецсимвол/reserved case-insensitive/длинное).
  - [x] Резолюция пути (AC #11): `storage_root == dev_repo_root.parent / game` при разных cwd (`monkeypatch.chdir`).
  - [x] Имя занято (AC #2): пред-создать `storage_root` → fail-loud, не тронуто.
  - [x] Полный проход на мини-dev-репо (`tmp_path` c `templates/external_storage/` + `paths-to-symlink.csv` + цели контракта): хранилище создано, 4 файла, симлинки в dev-репо (capability-gated: `pytest.skip` без Dev Mode), `.env` с `GDAU_DATA_ROOT` и пустым токеном, `gdau.duckdb` создан, view'ы `visits`/`hits` пусты-типизированы (AC #14). `git`/`uv` — реальные при `shutil.which`, иначе инъекция фейкового runner'а; без сети.
  - [x] Полный откат (AC #6): инъекция сбоя на шаге (напр. fake `uv sync` бросает) → `storage_root` удалён целиком; **критичный тест**: цель инфра-симлинка («dev-репо»-tmp) ЦЕЛА после отката (`rmtree` снял ссылку, не цель).
  - [x] `.env` без секрета: токен/счётчик пустые, `GDAU_DATA_ROOT` = abs storage.
  - [x] git initial commit (AC #4, #13, если git доступен): коммит есть, непуст; `git ls-files` без `.env`, с `.env.example`/`PROJECT.md`.
  - [x] Анти-зависимость (`ast`, import-узлы): без `pandas`/`polars`/`numpy`/`pyarrow`/directaiq-инфры; только `scripts.init.*`/`scripts.utils.*` + stdlib (паттерн `tests/test_parquet_store.py`).
  - [x] Live НЕ заводить (ФС/процессы, без API) — зафиксировать в Dev Agent Record.
- [x] **Task 14 — Гейты верификации (обязательны перед закрытием)**
  - [x] `uv run mypy scripts` → зелено (strict; без `Any`-дыр). **win32 + `--platform linux`** (кросс-OS, как 2.5–2.7/4.1/4.2).
  - [x] `uv run pytest` → зелено (новый `test_init_project.py` + регрессия 1.x/2.x/3.1/4.1/4.2; live отсеян `addopts="-m 'not live'"`).
  - [x] Новых зависимостей нет (`argparse`/`os`/`re`/`shutil`/`subprocess`/`logging`/`pathlib` — stdlib; `duckdb` через `database_manager`) → **`uv.lock` не меняется**.
  - [x] `data/`-артефактов (`*.parquet`/`*.duckdb`/`.writer.lock`) в **dev-репо** не создано (init пишет только в хранилище-сосед на `tmp_path` в тестах). Новые/изменённые коммитируемые файлы: `scripts/init/init_project.py` (наполнен), `tests/test_init_project.py` (новый), `templates/paths-to-symlink.csv` (+ `uv.lock`), `templates/external_storage/.gitignore` (+ `/uv.lock`, синхрон 4.2), дополнение `docs/init-and-storage.md`.
  - [x] Прогнать чек-лист «Definition of Done» из Dev Notes.

## Dev Notes

### Рекомендуемый контракт `init_project.py` (финализируй при реализации)

| Имя | Сигнатура | Смысл |
|---|---|---|
| `StorageInitError` | `class(RuntimeError)` | сбой оркестрации init (имя занято/невалидное, сбой шага); обёртка сырья |
| `_validate_game_name` | `(name: str) -> str` | строгий шаблон + reserved + path-sep (AC #7) |
| `_create_parser` | `() -> argparse.ArgumentParser` | argparse, позиционный `game` (форма directaiq) |
| `main` | `() -> None` | оркестрация D6 + откат + коды возврата; entry point `gdau-init` |

**Использование примитивов (порядок D6):**
```python
game = _validate_game_name(args.game)
dev_repo_root = Path(__file__).resolve().parents[2]
storage_root = dev_repo_root.parent / game
if storage_root.exists():                       # AC #2
    raise StorageInitError(f"Имя занято: {storage_root}")
# preflight ДО создания (откат не нужен):
if shutil.which("git") is None: raise StorageInitError("git не установлен …")  # AC #9
if shutil.which("uv") is None:  raise StorageInitError("uv не установлен …")
preflight_symlink_capability()                  # 4.1, AC #5 (Dev Mode)
try:
    copy_storage_template(storage_root=storage_root)                                   # 4.2
    create_symlinks(dev_repo_root=dev_repo_root, storage_root=storage_root,
                    run_preflight=False)                                               # 4.1
    _write_env(storage_root)                                                           # .env + GDAU_DATA_ROOT
    _uv_sync(storage_root)                                                             # uv sync --frozen
    os.environ[env_reader.DATA_ROOT_ENV] = str(storage_root)                           # инъекция корня
    with writer_lock():                                                                # 2.5
        with DatabaseManager.connection(read_only=False) as conn:                      # 2.1
            create_views(conn)                                                         # 2.6 (пустые view'ы)
    _git_init_commit(storage_root, game)                                               # git, .env исключён
except BaseException:
    _rollback(storage_root)   # shutil.rmtree — снимает симлинки os.unlink'ом, dev-репо цел
    raise
```

### Расхождения с directaiq `init_project.nu` (осознанные; трассируемость)

| Аспект | directaiq (`scripts/nushell/init_project.nu`) | gdau (4.3) | Почему |
|---|---|---|---|
| Язык | nushell + bash-обёртки (`source activate.sh`) | **Python** (`subprocess` для git/uv) | architecture.md:427; кросс-платформенно Win↔Linux (AC #5); NFR-2 (без bash/nushell) |
| venv | shared `../shared_python_env/.venv` | **per-storage `.venv`** через `uv sync --frozen` в хранилище | простота; storage самодостаточен; CI-консистентный `--frozen` |
| Схема БД | `migrate.py` + система миграций + `.migrations_applied` | **view'ы из каталога** (`create_views`, 2.6) | gdau не тащит миграции (NFR-6); рабочий слой = view'ы (OQ#3) |
| `activate.sh`/`SKIP_AUTO_MIGRATE` | нужны для автомиграции | **нет** — `database_manager` создаёт файл, `create_views` ставит схему | вырезана инфра directaiq (NFR-6) |
| Симлинки | абсолютные цели + `rm -rf` существующего + `ln -sf` | относительные цели (4.1), реальный файл → fail-loud (НЕ `rm -rf`) | NFR-2 переносимость; не удалять данные (4.1 D2/D4) |
| `.env` | `DIRECTAIQ_DATA_ROOT=<abs>`, комментирование старых строк | `.env` из `.env.example` + `GDAU_DATA_ROOT=<abs>`, токен пуст | gdau-контракт env (1.2); владелец вписывает токен (AC #3) |
| Откат | нет (частичный мусор остаётся) | **полный откат хранилища** при сбое любого шага | AC #6 (epics:482/486/488); чистый повтор (AC #10) |
| git commit | `.env` через `git reset HEAD -- .env` | то же (`git reset -- .env`), русское сообщение коммита | AC #4; стиль репо (Conventional, русский) |

Сохраняем от directaiq: общую последовательность шагов (имя→шаблон→симлинки→env→deps→БД→git); идею «`.env` вне initial commit через reset» (fail-loud для случайных секретов); распознавание «уже git-репо».

### Паттерны от историй 1.x/2.x/4.1/4.2 (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` первой строкой; русский модульный docstring (роль + границы + расхождения); идентификаторы английские, docstrings русские; type hints везде, `mypy --strict` по `scripts` (win32 + `--platform linux`), без `Any`-дыр; абсолютные импорты от корня пакета; `logger = logging.getLogger(__name__)`.
- **Дефолтный путь артефакта dev-репо** — `Path(__file__).resolve().parents[2]/...` (как `catalog.DEFAULT_CATALOG_PATH`, `symlinks.DEFAULT_CONTRACT_PATH`, `scaffold.DEFAULT_TEMPLATE_ROOT`): резолвится сквозь симлинк хранилища в dev-репо. Для 4.3 `parents[2]` = корень dev-репо.
- **Инъекция швов** (`dev_repo_root`/`storage_parent` — параметры функций оркестрации, дефолты прод) — как `conn`/`storage_root`/`dev_repo_root` в 2.x/4.1/4.2; тесты на `tmp_path` без сети.
- **Валидация/preflight fail-loud ДО мутаций + русское сообщение + путь;** никогда сырой `OSError`/`subprocess`/`duckdb`-сбой наружу (обернуть в `StorageInitError`, паттерн ревью 2.1/4.1/4.2).
- **CLI-форма directaiq:** класс/функции + `_create_parser` + `main`; коды возврата (`SystemExit(1)` на fail, `130` на SIGINT — как 2.9); вывод человекочитаемым текстом/логом (без `--format`).
- **Анти-зависимость через `ast`** (import-узлы, не подстрока) — паттерн `tests/test_parquet_store.py`.
- **Live-набор осознанно отсутствует** (нет внешнего API) — зафиксировать, как 2.1–2.6/4.1/4.2.
- **Capability-gated симлинк-тесты** (4.1): реальные симлинки под `pytest.skip` без Dev Mode (GH windows-runner не валит); покрытие — ubuntu (нативно).

### Границы 4.3 (не выходить)

- Артефакты: `scripts/init/init_project.py` (наполнить стаб — оркестратор), `tests/test_init_project.py` (новый), `templates/paths-to-symlink.csv` (+ `uv.lock`), `templates/external_storage/.gitignore` (+ `/uv.lock` синхрон, файл 4.2), дополнение `docs/init-and-storage.md`. **Не** реализуем заново: симлинки/preflight (4.1), копирование/`PROJECT.md` (4.2), DDL view'ов (2.6), открытие БД (2.1), лок (2.5).
- `init_project.py` ходит к ФС и подпроцессам (`git`/`uv`), но **НЕ** в сеть (нет Logs API в init), **НЕ** парсит TSV, **НЕ** пишет данные/партиции (только пустую БД + view'ы), **НЕ** реализует retry/rate-limit.
- **Не** добавлять `.mcp.json`/`.claude` в контракт (D11 — отсутствуют/Epic 3); **не** трогать примитивы 4.1/4.2/2.x (кроме строки в CSV/`.gitignore` 4.2 — синхрон `uv.lock`).
- **Не** переводить на src-layout, не переименовывать пакет `scripts` (hatchling `packages=["scripts"]`). `uv.lock` не трогаем (всё stdlib + duckdb через `database_manager`).

### Project Structure Notes

- Оркестратор — `scripts/init/init_project.py` (architecture.md:465 «init-команда (Python): разворачивание per-game хранилища»; entry point `gdau-init = scripts.init.init_project:main`, pyproject.toml:20). Сейчас — стаб (1.1). Рядом: `symlinks.py` (4.1), `scaffold.py` (4.2). `scripts/init/` — регулярный пакет (`__init__.py` из 1.1).
- `templates/paths-to-symlink.csv` — контракт (4.1); 4.3 финализирует, добавив `uv.lock` (D11). `templates/external_storage/.gitignore` — файл 4.2; 4.3 синхронизирует симлинк-секцию (`uv.lock`).
- `docs/init-and-storage.md` — компонент «init + симлинки + два-репо» (project-context.md:64); заводит 4.1, дополняют 4.2 (шаблон) и 4.3 (полный разворот). Существующие спеки не трогаем.
- `tests/` зеркалит `scripts/`: `tests/test_init_project.py`. Конфиг pytest (`markers`/`addopts`) есть (1.3/1.6); `conftest.py` нет — `tmp_path`/`monkeypatch` напрямую.
- **Зависимость порядка (повтор — критично):** 4.3 идёт ПОСЛЕ 4.1 (`review`) и 4.2 (`ready-for-dev`, код ещё не написан). 4.3 импортирует `scaffold.copy_storage_template` (4.2) и `symlinks.*` (4.1) → оба должны быть в рабочем дереве/`main` до старта 4.3 (Task 0).
- **Per-game хранилище НЕ в dev-репо git:** init создаёт `../{game}` (сосед dev-репо) со своим `git init`; в dev-репо `gdau-init` не пишет данные (в тестах хранилище — на `tmp_path`).

### Что 4.3 наследует от примитивов (НЕ перепроверять заново)

- **4.1 `create_symlinks`:** относительные цели, идемпотентная замена симлинка, fail-loud на реальном файле, предвалидация целей (битый контракт → `SymlinkTargetMissingError` ДО первого линка), откат СВОИХ симлинков. 4.3 даёт `run_preflight=False` (preflight отдельно на Task 4).
- **4.2 `copy_storage_template`:** валидация шаблона fail-loud ДО мутаций (AC #5 4.2), сохранение заполненного `PROJECT.md` (AC #6 4.2 — в штатном 4.3 не триггерится, D4), обёртка `OSError` в `StorageTemplateError`. 4.3 ловит `StorageTemplateError` и откатывает хранилище (AC #12).
- **2.1 `DatabaseManager.connection(read_only=False)`:** создаёт `data/duckdb/` родителя и файл `gdau.duckdb`, гарантированное закрытие в `finally`. Требует `GDAU_DATA_ROOT` (инъекция через `os.environ`, D9).
- **2.6 `create_views(conn)`:** DDL из каталога; нет партиций → `has_partitions=False` → пустые типизированные view'ы (`CAST(NULL AS type) … WHERE false`, AC #14 / 2.6 AC #6). `conn` инъектируется; лок берёт вызывающий (4.3, D9).
- **2.5 `writer_lock()`:** неблокирующий контекст-менеджер; на свежем хранилище свободен; освобождается в `finally`.
- **1.2 `env_reader`:** имена `TOKEN_ENV`/`COUNTER_ENV`/`DATA_ROOT_ENV` — использовать константы (не литералы). `.env` хранилища грузится по `GDAU_DATA_ROOT` или cwd walk-up при рантайме `gdau-logs`/MCP (не забота 4.3, кроме генерации корректного `.env`).

### Latest Tech Information

- **`subprocess.run([...], cwd=..., capture_output=True, text=True, timeout=...)`** — кросс-платформенный запуск `git`/`uv` (без shell, без bash-обёртки; AC #5). `check=False` + ручная проверка `returncode` → понятная ошибка вместо `CalledProcessError`-трейсбека. `FileNotFoundError` если бинарь не найден (но preflight `shutil.which` ловит раньше). `TimeoutExpired` на зависшем `uv sync`.
- **`shutil.rmtree(path)` и симлинки:** для симлинков ВНУТРИ дерева `rmtree` вызывает `os.unlink` (НЕ рекурсирует в цель) → инфра-симлинки на dev-репо снимаются, цель (код dev-репо) цела. Top-level `path` не должен быть симлинком (у нас `storage_root` — реальный каталог). Это нативное безопасное поведение; **проверить тестом** (D12).
- **`uv sync --frozen`** — ставит окружение строго по `uv.lock`, **не перезаписывая** lock (критично: lock приходит симлинком на dev-репо, D8/D11). Без `--frozen` uv мог бы попытаться пере-резолвить и записать lock сквозь симлинк.
- **`os.symlink` target_is_directory (Windows)** — забота 4.1 (`create_symlinks` уже передаёт `target_is_directory`); 4.3 это наследует, не реализует.
- **`re.compile` для имени** — строгий шаблон компилируется один раз (константа модуля). Reserved Windows-имена — отдельный frozenset (regex их не покрывает: `CON` матчит `[A-Za-z0-9_-]+`).
- **Web-ресёрч не требуется:** stdlib + `git`/`uv` CLI стабильны; внешнего сетевого контракта в истории нет (live-smoke неприменим, как 2.1–2.6/4.1/4.2).

### Definition of Done — чек-лист самопроверки

1. `scripts/init/init_project.py` наполнен оркестратором: `_validate_game_name` + резолюция пути от dev-репо + preflight (git/uv/симлинки) + `copy_storage_template` (4.2) + `create_symlinks` (4.1) + `.env` + `uv sync --frozen` + `gdau.duckdb`+view'ы (2.6) под локом + `git init`+commit + полный откат + `_create_parser`/`main`. Стаб-`print` убран. (AC #1, #3, #5)
2. Имя занято / `../{game}` существует → fail-loud без перезаписи, ДО мутаций. (AC #2)
3. Имя валидируется строгим шаблоном (path-sep/`..`/ведущая точка/пробел/спецсимвол/reserved Windows/длина) → понятный отказ. (AC #7)
4. Путь `../{game}` резолвится от `dev_repo_root.parent`, НЕ от cwd. (AC #11)
5. Сбой любого шага (копия/симлинки/`.env`/`uv sync`/БД/git) → **полный откат** хранилища (`rmtree`), имя снова свободно; `rmtree` не трогает цели инфра-симлинков (dev-репо цел). (AC #6, #12)
6. `git` не установлен → preflight с инструкцией; `uv sync` упал/нет сети → отчёт + откат. (AC #9)
7. `.env` сгенерирован из `.env.example` + `GDAU_DATA_ROOT=<abs>`; токен/счётчик пустые (владелец вписывает); `.env` НЕ в initial commit (`git reset`). (AC #1, #3, #4)
8. `git init` изолирован в `{game}` (сосед dev-репо); «уже репо» распознаётся; initial commit непуст (файлы шаблона минус `.env`). (AC #8, #13)
9. `gdau.duckdb` создан, view'ы `visits`/`hits` построены из каталога толерантно к нулю партиций (пустые типизированные). (AC #1, #14)
10. Симлинк-контракт финализирован: `uv.lock` добавлен; `.mcp.json`/`.claude` отложены до Epic 3 (D11); синхрон `.gitignore` шаблона; `docs/init-and-storage.md` обновлён. Тест 4.1 shipped-contract зелёный.
11. Resume после частичного сбоя определён: чистый повтор после отката, либо явная ошибка «остаток, удали вручную» если откат не дочистил. (AC #10)
12. Offline-тесты покрывают AC #2/#4/#6/#7/#11/#13/#14 + полный проход (capability-gated симлинки) + откат-не-трогает-цель + `.env`-без-секрета + ast-анти-зависимость. Live осознанно отсутствует. (Task 13)
13. `uv run mypy scripts` (win32 + `--platform linux`) и `uv run pytest` — зелёные; `uv.lock` не менялся; `data/`-артефактов в dev-репо нет; в тестах хранилище на `tmp_path`. (Task 14)
14. Велась в отдельной ветке `story/4.3-init-command` от обновлённого `main` (после merge 4.1/4.2); merge в `main` только после зелёного CI на обеих ОС. PR в `main`.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 4.3] (строки 469–490) — user story + 14 AC (включая edge-cases: опасное имя, git-вложенность, провал uv/git, resume, резолюция от cwd, диск/права, пустой commit, view'ы на пустом хранилище).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-19] (строка 53) — init разворачивает хранилище из шаблона одной командой; на выходе готово к первой выгрузке без ручных правок кроме токена/счётчика; имя занято → fail-loud.
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 4] (строки 432–434) — место 4.3 в цепочке 4.1→4.2→4.3; разворот `gdau-init` одной командой (SM-3, UJ-1).
- [Source: _bmad-output/planning-artifacts/architecture.md#Init на Python] (строки 251–253) — init на Python: имя → шаблон → симлинки → `.env` → `uv` → DuckDB+view'ы из каталога → `git init`; имя занято → fail-loud.
- [Source: _bmad-output/planning-artifacts/architecture.md#Integration & Data Flow] (строки 541–542) — init-поток: имя → шаблон → симлинки по CSV (+ preflight Dev Mode) → `.env` → `uv sync` → БД → `git init`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Per-game storage tree] (строки 488–504) — `../{game}/` несёт `.env`/`PROJECT.md`/`data/`/`gdau.duckdb` (физически) + симлинки (`scripts`/`development-docs`/`yandex-docs`/`.mcp.json`/`pyproject.toml`/`.claude`) на dev-репо.
- [Source: _bmad-output/planning-artifacts/architecture.md#Mapping table] (строки 427, 429) — `init_project.nu` → `init_project.py` (Python, не nushell); `paths-to-symlink.csv` — тот же CSV-контракт.
- [Source: _bmad-output/planning-artifacts/architecture.md#Gap Analysis] (строки 599, 602) — «точный состав `paths-to-symlink.csv` финализировать при сборке init» (D11); «Windows Dev Mode не проверен — поймает preflight init».
- [Source: _bmad-output/planning-artifacts/architecture.md#FR→структура] (строка 532) — FR-19/20/21 → `init_project.py`, `paths-to-symlink.csv`, `external_storage/PROJECT.md`.
- [Source: scripts/init/init_project.py:1–16] — текущий стаб `gdau-init` (наполняет 4.3).
- [Source: scripts/init/symlinks.py:171–299] — 4.1: `preflight_symlink_capability` (проба + Dev Mode), `create_symlinks(*, dev_repo_root, storage_root, contract_path=None, run_preflight=True)` (относительные цели, предвалидация, откат своих симлинков). 4.3 зовёт с `run_preflight=False`.
- [Source: scripts/init/scaffold.py (4.2, ready-for-dev)] — `copy_storage_template(*, storage_root, template_root=None) -> list[Path]` + `StorageTemplateError`; валидация шаблона fail-loud ДО мутаций; сохранение `PROJECT.md`; без отката (граница 4.3). См. файл истории 4.2.
- [Source: scripts/utils/database_manager.py:39–98] — `DatabaseManager.connection(read_only=False)`: write-режим создаёт `data/duckdb/` + файл; гарантированное закрытие; требует `GDAU_DATA_ROOT` (через `get_db_path`→`get_storage_root`).
- [Source: scripts/utils/views.py:117–183] — `create_views(conn, *, catalog=None, sources=VALID_SOURCES)`: DDL из каталога; нет партиций → пустые типизированные view'ы; лок берёт вызывающий (init 4.3). `conn` инъектируется.
- [Source: scripts/utils/paths.py:49–127] — `get_storage_root()` читает `GDAU_DATA_ROOT` (абсолютный, существующий каталог, fail-loud); `get_db_path`/`get_raw_source_dir`/`get_writer_lock_path` чистые, без `mkdir`. Резолюцию корня делает init (4.3) — установкой env.
- [Source: scripts/utils/env_reader.py:25–28] — константы `TOKEN_ENV`/`COUNTER_ENV`/`DATA_ROOT_ENV`; `.env` хранилища грузится по `GDAU_DATA_ROOT` или cwd walk-up.
- [Source: scripts/utils/writer_lock.py] — `writer_lock()` контекст-менеджер (2.5): неблокирующий, освобождение в `finally`; обернуть DDL view'ов (как p81 2.7).
- [Source: scripts/tools/logs_api_cli.py:36–90] — паттерн argparse-CLI: класс + `_create_parser` + `main`; коды возврата; вывод текстом (без `--format`); `KeyboardInterrupt`→`SystemExit(130)`.
- [Source: templates/paths-to-symlink.csv] — контракт 4 целей (`scripts`/`development-docs`/`yandex-docs`/`pyproject.toml`); 4.3 добавляет `uv.lock` (D11).
- [Source: docs/init-and-storage.md:44–73] — раздел симлинков (4.1): «`.mcp.json`/`.claude/…` добавятся… в 4.3» → 4.3 уточняет (uv.lock добавлен; `.mcp.json`/`.claude` отложены до Epic 3); границы 4.2/4.3.
- [Source: _bmad-output/implementation-artifacts/4-1-...preflight.md] — паттерны 4.1 (инъекция корней, fail-loud, ast-анти-зависимость, capability/live-границы, относительные цели); решения D1–D7.
- [Source: _bmad-output/implementation-artifacts/4-2-...проекта.md] — 4.2: `scaffold.copy_storage_template` контракт; шаблон 4 файла; `.gitignore` симлинк-секция; кросс-стори флаг (откат 4.3 не сносит заполненный `PROJECT.md`); D6 (примитив без отката — 4.3 откатывает).
- [Source: G:/git/directaiq/scripts/nushell/init_project.nu] — источник формы потока init (check-name → copy → symlinks → .env → python/venv → deps → DB → git); переписываем на Python с расхождениями (таблица выше).
- [Source: _bmad-output/project-context.md#Границы и каналы] (строки 119–124) — граница dev-репо↔хранилище; данные/`.env` — в хранилище; в dev-репо данные не пишутся.
- [Source: _bmad-output/project-context.md#Development Workflow] (строки 161–168) — новая история → новая ветка; uv.lock авторитетен (`uv sync --frozen`); секреты не коммитятся; два репо раздельны (хранилище со своим `git init`).
- [Source: _bmad-output/project-context.md#Edge-кейсы] (строка 193) — preflight симлинков (Windows Dev Mode) fail-loud с инструкцией.
- [Memory: directaiq-vendor-source] — directaiq как источник формы init. [[feedback-decide-and-apply]] — Шеф делегирует D1–D12 («реши сам, надёжно»). [[realapi-smoke-tests]] — live только для внешнего API → в 4.3 не нужен. [[gdau-env-contract]] — `.env` контракт (`GDAU_DATA_ROOT`/токен/счётчик), резолюцию `GDAU_DATA_ROOT` делает init 4.3. [[parallel-epic3-epic4-worktrees]] — worktree epic4; стык `.mcp.json`→4.3 (отложен: Epic 3 не слит, D11). [[flowctl-python-invocation]] — на Windows `python`, не `python3` (Store-заглушка) — учесть в подсказках/доках.

## Уточняющие вопросы (не блокирующие — реализацию не задерживают)

1. **Финализация симлинк-контракта (D11).** Решено: добавить `uv.lock` (нужен для `uv sync --frozen` в хранилище); `.mcp.json` (Epic 3, отсутствует на `main`) и `.claude` (рантайм агента) **отложить** до слияния Epic 3 — допишутся одной строкой CSV без правки кода (суть FR-20). Если хочешь добавить `.claude` уже сейчас (он есть в dev-репо, несёт `skills/`) — скажи, впишу строку и страж в тест. `.mcp.json` сейчас добавить нельзя (`create_symlinks` упадёт на отсутствующей цели).
2. **Per-storage `.venv` vs запуск из dev-репо (D8).** Решено: хранилище самодостаточно — `uv sync --frozen` создаёт `.venv` в хранилище (оператор/агент работает из папки игры; `uv.lock` симлинкуется). Альтернатива — не ставить `.venv` в хранилище, а запускать `gdau-logs`/MCP из dev-репо с `GDAU_DATA_ROOT` на хранилище (тогда `uv sync` в init не нужен, `uv.lock` не симлинкуется). Выбран самодостаточный вариант (соответствует architecture.md:252 и UJ «агент работает в хранилище»). Если предпочитаешь «запуск из dev-репо» — упростим init (убрать `uv sync`-шаг).
3. **Создание БД in-process vs subprocess (D9).** Решено: `gdau.duckdb` + view'ы создаются **в процессе** init (через `os.environ[GDAU_DATA_ROOT]` + `DatabaseManager`/`create_views`), не отдельным `uv run python -c …` в хранилище (как directaiq). Проще и тестируемее; интерпретатор dev-репо использует тот же каталог/код. Если хочешь, чтобы создание БД шло именно окружением хранилища (изоляция) — переключу на subprocess после `uv sync`.
4. **`GDAU_DATA_ROOT` в `.env` хранилища (D7) и переносимость.** Решено: init пишет в `.env` абсолютный `GDAU_DATA_ROOT` (как directaiq) — `.env` не коммитится и не переносится копированием, при переносе папки на другую машину строку правят/re-run init. Если хочешь иную схему доставки корня (напр. `uv run --env-file` без записи в `.env`, или запуск только из каталога хранилища с cwd-резолюцией) — обсудим.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7[1m] (Claude Opus 4.7, 1M context) — dev-story workflow.

### Debug Log References

- `uv run mypy scripts` (win32) → Success: no issues found in 24 source files.
- `uv run mypy scripts --platform linux` → Success: no issues found in 24 source files.
- `uv run pytest` → 474 passed, 7 skipped (capability-gated симлинки без Dev Mode), 8 deselected (live). Было 447 → +27 тестов.
- `uv run pytest tests/test_init_project.py tests/test_init_symlinks.py -v` → 44 passed, 7 skipped (3 новых capability-gated 4.3 + 4 из 4.1).
- Разовая локальная проверка ядра оркестрации на Windows (throwaway-скрипт, удалён): с заглушкой ТОЛЬКО symlink-syscall (`preflight_symlink_capability`/`create_symlinks`) прогнаны реальные `copy_storage_template`+`_write_env`+`_create_database`+`_git_init_commit` → `gdau.duckdb` создан, view'ы `visits`/`hits` пусты и типизированы (`visit_id`/`watch_ids`/`date`/`date_time`…), `.env` несёт `GDAU_DATA_ROOT` + пустой токен, git initial commit «init: развёртывание хранилища игры mygame», в индексе только 4 файла шаблона (`.env` исключён), `GDAU_DATA_ROOT` в окружении восстановлен в `None` (без утечки).
- Проверено: `data/`-артефактов (`*.duckdb`/`*.parquet`) в dev-репо нет; `uv.lock` не менялся.

### Completion Notes List

Оркестратор `gdau-init` реализован **тонким** — механику не дублирует, вызывает примитивы 4.1/4.2/2.6/2.1/2.5 в порядке D6; своё: валидация имени, резолюция пути от dev-репо, `.env`, `uv sync`, `git init`, **полный откат хранилища**. Все 14 AC закрыты; 14 пунктов DoD выполнены.

- **Все 14 задач выполнены.** Стаб `init_project.py` заменён оркестратором: `StorageInitError`, `_validate_game_name` (AC #7), `_resolve_dev_repo_root`/`_resolve_storage_root` (AC #11, D2 — не от cwd), `_preflight_environment` (git/uv, AC #9), `_write_env` (AC #1/#3), `_uv_sync` (`--frozen`, AC #1/#9), `_create_database` (под `writer_lock`, пустые view'ы, AC #1/#14), `_git_init_commit` (`.env` исключён, AC #4/#8/#13), `_rollback` (полный `rmtree`, AC #6/#10/#12), `init_storage`-оркестратор + `_create_parser`/`main`.
- **🔷 РАТИФИЦИРОВАННОЕ ОТКЛОНЕНИЕ от D11 (Task 9) — предпосылка изменилась.** D11 предписывал добавить ТОЛЬКО `uv.lock`, отложив `.mcp.json`/`.claude` «пока Epic 3 не слит / `.mcp.json` отсутствует». На момент dev-story **Epic 3 уже влит** (PR #19), `.mcp.json` существует и git-tracked → блокирующая причина исчезла, сработала собственная контингентность D11 («допишется когда Epic 3 сольётся»). **Решение Шефа (2026-05-26):** добавить `uv.lock` + `.mcp.json` (канал чтения игры `duckdb_query`, per-game tree architecture.md:488–504); `.claude` **отложить** (несёт dev-скилы BMad + `settings.local.json`, не рантайм игры). Контракт = 6 записей. Это смена контракта 4→6, не регресс.
- **Швы инъекции (D12):** `init_storage(game, *, dev_repo_root, storage_parent, runner)` — дефолты прод-резолюция, тесты дают `tmp_path`; `runner` (Protocol `CommandRunner`) подменяет запуск `git`/`uv` (фейк-uv/реальный-git в тестах, без сети). `template_root`/`contract_path` выводятся из `dev_repo_root` (в проде == `DEFAULT_TEMPLATE_ROOT`/`DEFAULT_CONTRACT_PATH`).
- **Улучшение vs D8/D9-прескрипции:** `_create_database` **восстанавливает** `os.environ[GDAU_DATA_ROOT]` после шага (а не просто оставляет мутацию) — не пачкает процесс-окружение/соседние тесты; задокументировано в докстринге.
- **Анти-зависимость:** `duckdb` напрямую НЕ импортируется (через `database_manager`) — `_create_database` ловит `Exception` узко в шаге создания БД (иначе сырой `duckdb.Error` из `conn.execute(ddl)` улетел бы мимо обёртки; импорт `duckdb` нарушил бы ast-страж). Имя валидируется `NAME_PATTERN.fullmatch` (не `match` — иначе `$` пропустил бы хвостовой `\n`).
- **Регрессия 4.1:** `test_shipped_contract_loads_and_targets_exist` обновлён под контракт 4→6 записей (смена контракта, не регресс) — остаётся зелёным.
- **Кросс-стори синхрон (артефакты 4.2):** `templates/external_storage/.gitignore` дополнен `/uv.lock` в симлинк-секции (`.mcp.json`/`.claude` там уже были).
- **Live-набор осознанно НЕ заводится** (как 2.1–2.6/4.1/4.2): 4.3 — ФС + процессы `git`/`uv`, без внешнего API; правило opt-in live — только для Logs API ([[realapi-smoke-tests]]).
- **Capability-gated (3 теста):** полный проход / откат / битый шаблон требуют реального symlink-syscall → `skip` без Dev Mode (Windows), реальное покрытие даёт ubuntu CI. Ядро (БД/view/git/.env) дополнительно проверено локально throwaway-скриптом на Windows (см. Debug Log).

### File List

- `scripts/init/init_project.py` — наполнен (стаб → оркестратор `gdau-init`).
- `tests/test_init_project.py` — новый (30 тестов: 27 passed + 3 capability-gated skip).
- `templates/paths-to-symlink.csv` — + `uv.lock`, + `.mcp.json` (D11, решение Шефа).
- `templates/external_storage/.gitignore` — + `/uv.lock` в симлинк-секцию (синхрон с CSV; артефакт 4.2).
- `tests/test_init_symlinks.py` — `test_shipped_contract_loads_and_targets_exist` обновлён под контракт 4→6 записей (регрессия 4.1).
- `docs/init-and-storage.md` — вводная + раздел симлинков (D11) обновлены; добавлен раздел «Полный разворот командой `gdau-init`».
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — статус 4-3 ready-for-dev → in-progress → review + трекинг-комментарий.

## Change Log

- 2026-05-26 — Story 4.3 реализована (dev-story): оркестратор `gdau-init` разворачивает per-game хранилище (FR-19), ФИНАЛЬНАЯ история Epic 4. Все 14 AC + 14 DoD закрыты. Тонкий оркестратор поверх примитивов 4.1/4.2/2.6/2.1/2.5: валидация имени (строгий шаблон + reserved Windows + path-sep/`..`, AC #7) → резолюция `../{game}` от dev-репо не cwd (AC #11) → «имя свободно» (AC #2) → preflight git/uv/симлинки ДО мутаций (AC #5/#9) → copy_storage_template (4.2) → create_symlinks (4.1, `run_preflight=False`) → `.env` из `.env.example` + `GDAU_DATA_ROOT` abs (AC #1/#3) → `uv sync --frozen` (AC #1/#9) → `gdau.duckdb`+view'ы под `writer_lock` (2.5/2.1/2.6, пустые типизированные, AC #1/#14) → `git init`+commit, `.env` исключён (AC #4/#8/#13) → **полный откат `rmtree`** при сбое любого шага, цели инфра-симлинков целы (AC #6/#10/#12). Инъекция швов `dev_repo_root`/`storage_parent`/`runner` (D12). **🔷 Решение Шефа по D11 (предпосылка изменилась — Epic 3 влит):** симлинк-контракт = 4 стабильные цели + `uv.lock` + `.mcp.json`; `.claude` отложен. Гейты зелёные: mypy strict win32+linux 24 файла, pytest 474 passed (+27) / 7 skipped (capability-gated) / 8 deselected; `uv.lock` не менялся; `data/`-артефактов в dev-репо нет; live неприменим (ФС/процессы, без API). Изменения НЕ закоммичены — ветка `story/4.3-init-command` ждёт code-review + merge в `main`. **EPIC 4 ЗАКРЫТ** (все 3 истории done/review). Статус → review.

- 2026-05-25 — Story 4.3 создана (create-story): оркестратор `gdau-init` разворачивает per-game хранилище (FR-19) — ТРЕТЬЯ (финальная) история Epic 4 (init). Наполняет стаб `scripts/init/init_project.py` тонким оркестратором поверх примитивов 4.1 (`symlinks`)/4.2 (`scaffold`)/2.6 (`views`)/2.1 (`database_manager`)/2.5 (`writer_lock`). Артефакты: `scripts/init/init_project.py` (наполнен), `tests/test_init_project.py` (новый), `templates/paths-to-symlink.csv` (+ `uv.lock`), синхрон `templates/external_storage/.gitignore` (4.2), дополнение `docs/init-and-storage.md`. **РЕШЕНИЯ D1–D12 зафиксированы** (Шеф делегирует, [[feedback-decide-and-apply]]): D1 тонкий оркестратор (примитивы не дублируются); D2 путь `../{game}` от `dev_repo_root.parent`, не cwd (AC #11); D3 строгая валидация имени (path-sep/reserved Windows/длина, AC #7); D4 имя занято → fail-loud (AC #2); D5 ПОЛНЫЙ откат хранилища `rmtree` при сбое (AC #6, rmtree не трогает цели симлинков); D6 порядок: preflight'ы (git/uv/симлинки) ДО создания хранилища; D7 `.env` из `.env.example` + `GDAU_DATA_ROOT` abs, токен пуст (AC #3); D8 `uv sync --frozen` в хранилище (нужен симлинк `uv.lock`); D9 БД+view'ы in-process под `.writer.lock`, `GDAU_DATA_ROOT` через `os.environ` (AC #14); D10 `git init`+commit, `.env` исключён `git reset` (AC #4, #8, #13); D11 контракт финализирован — `uv.lock` добавлен, `.mcp.json`/`.claude` отложены до слияния Epic 3 (FR-20: одна строка CSV без кода); D12 тесты оркестрации на `tmp_path` с инъекцией dev-репо, capability-gated симлинки, ast-анти-зависимость, live неприменим. **⚠️ Жёсткая зависимость порядка:** 4.3 импортирует код 4.1 (`review`) и 4.2 (`ready-for-dev`, ещё не реализована) — оба должны быть в рабочем дереве/`main` до старта 4.3 (Task 0). Расхождения с directaiq `init_project.nu`: Python не nushell, per-storage `.venv` (не shared), схема = view'ы (не миграции), относительные симлинки, полный откат. Зависимостей нет (stdlib + duckdb через `database_manager`); live неприменим (ФС/процессы, без API). Уточняющие (не блокирующие) вопросы: финализация контракта (`.claude` сейчас?), per-storage venv vs dev-репо, in-process vs subprocess БД, `GDAU_DATA_ROOT` в `.env`. Статус → ready-for-dev.

## Review Findings (code-review 2026-05-26)

Adversarial 3-слойное ревью (Blind Hunter / Edge Case Hunter / Acceptance Auditor, Opus 4.7). **Acceptance Auditor: 14/14 AC PASS, 0 FAIL.** Триаж: **0 decision-needed, 4 patch, 0 defer, ~23 dismissed.**

### Patches

- [x] [Review][Patch] Сырой не-`FileNotFoundError` `OSError` от subprocess `git`/`uv` утекает трейсбеком мимо `main()` [scripts/init/init_project.py:_uv_sync / _git_init_commit] — `_uv_sync` ловит лишь `FileNotFoundError`+`TimeoutExpired`, `_git_init_commit` лишь `FileNotFoundError`; `PermissionError`/WinError 740 при запуске найденного бинаря пролетит мимо except-кортежа `main()` → голый трейсбек (нарушение инварианта «никогда сырой stdlib наружу»; хранилище при этом откатывается). Расширить перехват до `except OSError`.
- [x] [Review][Patch] git-тесты не изолированы от global/system git-конфига [tests/test_init_project.py:git_identity] — фикстура задаёт только идентичность; `commit.gpgsign=true`/`core.hooksPath`/`init.templateDir` в чужом окружении уронят/подвесят `git commit`. Добавить `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`=`os.devnull` + `GIT_CONFIG_NOSYSTEM=1`.
- [x] [Review][Patch] Тавтологичный `pytest.raises(Exception)` [tests/test_init_project.py:test_broken_template_propagates_and_leaves_no_storage] — слишком широко, замаскирует посторонний сбой. Сузить до `StorageTemplateError` (импортировать из `scaffold`).
- [x] [Review][Patch] Устаревший докстринг примитива `symlinks.py` (перечисляет 4 цели контракта) [scripts/init/symlinks.py:4-7] — после финализации контракт = 6 целей (+`uv.lock`/`.mcp.json`); прозаический текст разошёлся с CSV-SSOT и `docs/init-and-storage.md`. Синхронизировать перечисление.

### Dismissed (с обоснованием — для справки)

- **Resume-ветка `_git_init_commit`** (`.git` уже есть / gitlink-файл) и сценарий «`.env` уже закоммичен» — **недостижимы**: AC #2 (`init_storage`) падает «имя занято» ещё до создания, если `storage_root` существует.
- **`_rollback` на симлинк-`storage_root`** — недостижимо (AC #2 блокирует симлинк до создания).
- **Гонка `os.environ[GDAU_DATA_ROOT]` / TOCTOU `exists()→mkdir`** — вне модели «один оператор» (листовой CLI); env восстанавливается в `finally`.
- **`git diff --cached --quiet` returncode>1** трактуется как «есть staged» — недостижимо после валидированных `init`+`add`; реальный сбой ловит шаг `commit`.
- **Неполный набор reserved-имён** (`CLOCK$`/`CONIN$`/трейлинг-точка) — `NAME_PATTERN` уже режет `$`/точку/пробел; набор покрывает все matchable-reserved.
- **Реальный каталог схемы в тесте / host-интерпретатор для БД** — by-design (D9 in-process; `create_views` резолвит собственный каталог; пустые view'ы не зависят от содержимого каталога).
- **`except Exception` в `_create_database`** — осознанный компромисс под ast-анти-зависимость (`duckdb.Error` ⊄ `RuntimeError`); `BaseException` (KeyboardInterrupt) не проглатывается.
- **`.env.example` с активным секретом / non-UTF-8** — курируемый UTF-8-артефакт репо, не внешний ввод; пустота токена покрыта тестом AC #3.
- **D11 `.mcp.json` добавлен** — **ратифицированное** отклонение Шефа (Epic 3 влит PR #19, `.mcp.json` git-tracked); все артефакты синхронизированы, штатный разворот не ломается (предвалидация целей проходит).
- **Прочее** (избыточная проверка `separators`, `parents[2]`, отсутствие git-таймаута, покрытие отката одним шагом, ast-страж импортов) — by-design / паттерн репо / несущественно.
