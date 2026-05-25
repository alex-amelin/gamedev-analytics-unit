# Story 4.1: Декларативный симлинк-контракт и preflight

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a оператор юнита,
I want декларативный контракт симлинков и preflight-проверку способности их создавать,
so that хранилище ссылалось на инструменты dev-репо по явному списку, а обновления инструмента были видны всем играм — и чтобы непригодная платформа падала громко ДО частичного разворачивания.

**Контекст эпика.** ПЕРВАЯ история **Epic 4 «Развёртывание рабочего пространства игры (init)»** (FR-19/20/21). Эпик разворачивает per-game хранилище одной командой `gdau-init {game}`: проверка имени → копирование шаблона (4.2) → **симлинки по контракту (4.1, эта история) + preflight** → `.env` → `uv sync` → `gdau.duckdb` + view'ы из каталога (2.6) → `git init` (4.3). Истории Epic 4 — упорядоченные «снизу вверх»: **4.1 — декларативный контракт симлинков + механизм их создания/preflight** (примитив), 4.2 — шаблон хранилища + `PROJECT.md`, 4.3 — оркестратор `gdau-init`, склеивающий 4.1+4.2 в полный init. 4.1 покрывает **FR-20** (контракт симлинков задан декларативно, не разбросан по коду; обновление в dev-репо автоматически видно всем хранилищам); обслуживает **NFR-2** (переносимость Win↔Linux копированием папки) и границу двух-репо.

**Зачем симлинки, а не копии (корень требования).** Хранилище игры физически содержит **только данные игры** (`data/`, `.env`, `PROJECT.md`, `gdau.duckdb`), а общая инфраструктура юнита (код `scripts/`, каталог схемы `development-docs/`, справочники `yandex-docs/`, конфиг MCP, `pyproject.toml`, настройки агента `.claude/`) подключается **симлинками** на dev-репо (architecture.md:498–503, project-context.md:122/168). Это даёт: **(1)** один источник истины — фикс бага в `scripts/utils/*.py` или правка каталога долетают до всех игр через `git pull` в dev-репо, без копий и миграций; **(2)** ноль дубликации; **(3)** структурную (а не процедурную) согласованность инструмента у всех игр. Это осознанное архитектурное решение, унаследованное от directaiq ([[directaiq-vendor-source]], спека `directaiq/specs/20260515-external-storage-symlink-model.md`).

**Кто это потребляет (проектируй API под них).**
- **`gdau-init` / `init_project.py` (4.3)** — единственный прямой потребитель. Сейчас `scripts/init/init_project.py` — **стаб** (печатает «not yet implemented», exit 0; заведён в 1.1). 4.3 вызовет примитивы 4.1 в шаге «симлинки»: `preflight_symlink_capability()` → `create_symlinks(dev_repo_root=…, storage_root=…)`. Проектируй модуль **переиспользуемым и инъектируемым** (корни dev-репо и хранилища приходят параметрами — тесты гоняют на `tmp_path`, прод даёт реальные пути), как швы 2.x.
- **4.3 (откат частичного init)** — AC 4.3 (epics.md:482) требует отката всего хранилища при сбое посреди init. 4.1 даёт **свой** локальный откат набора симлинков (AC #9); 4.3 оборачивает его в полный откат хранилища. Не дублируй полный откат хранилища в 4.1 — только симлинки, созданные этим вызовом.
- **НЕ потребители:** приём (Epic 2), MCP-чтение (Epic 3). 4.1 — чисто файловая операция разворачивания; в сеть не ходит, БД не открывает, `.writer.lock` не берёт.

**Это НЕ вендоринг — новый модуль + новый контракт-файл.** В дереве архитектуры init помечен `scripts/init/init_project.py` (строка 465) и контракт `templates/paths-to-symlink.csv` (строка 472); тест — `test_init_symlinks.py` (строка 485). У directaiq логика симлинков жила в **nushell** (`scripts/nushell/init_project.nu`, функция `create-symlinks`, строки 54–93) — мы её **не вендорим построчно**, а переписываем на **Python** (architecture.md:427 «init на Python, не nushell») с двумя осознанными расхождениями (см. риски №2 и №4 ниже). Контракт-CSV — та же **форма** (`templates/paths-to-symlink.csv`, колонки `path,comment`), наполнение — своё под геймдев.

**Раскладка артефактов истории (3 файла + контракт):**
- `templates/paths-to-symlink.csv` — **новый** декларативный контракт (форма directaiq: колонки `path,comment`).
- `scripts/init/symlinks.py` — **новый** модуль: загрузка/валидация контракта + preflight + создание симлинков с откатом. (`tests/test_init_symlinks.py` — его естественное зеркало по имени из architecture.md:485; модуль кладём в `scripts/init/`, т.к. симлинки создаёт только init.)
- `docs/init-and-storage.md` — **новая** человекочитаемая спека компонента init/симлинки/два-репо (project-context.md:64 называет её в карте компонентов; 4.1 — первая init-история → заводит файл, 4.2/4.3 дополнят).
- `tests/test_init_symlinks.py` — offline-набор.

### Главные риски / решения (читать до кода)

> **Делегирование.** Шеф делегирует решения этой истории по принципу [[feedback-decide-and-apply]] («реши сам, главное чтобы работало надёжно»). Все семь решений ниже **зафиксированы** и должны быть реализованы как описано; в спорной точке выбран более строгий/переносимый вариант (project-context: «в спорной ситуации — более строгий»). Уточняющие (не блокирующие) вопросы — в конце файла.

1. **Модуль — `scripts/init/symlinks.py` (новый, не вендоринг; решение D1).** Симлинки создаёт только init, поэтому модуль живёт в `scripts/init/` (рядом с `init_project.py`), а не в `utils/` (там — примитивы пути записи/чтения данных). Имя теста в архитектуре — `test_init_symlinks.py` (строка 485) → модуль `symlinks.py` его прямое зеркало. Зависит **только** от stdlib (`csv`, `os`, `pathlib`, `logging`, `tempfile`); **НЕ** импортирует `paths.py`/`database_manager`/`duckdb`/`metrica_client` и т.п. — 4.1 работает с **инъектируемыми** `dev_repo_root`/`storage_root`, а не с `GDAU_DATA_ROOT` (резолюцию корня хранилища при init делает 4.3 до вызова 4.1). `from __future__ import annotations` первой строкой; русский модульный docstring (роль + границы); идентификаторы английские, docstrings русские; type hints везде (mypy strict).

2. **Цели симлинков — ОТНОСИТЕЛЬНЫЕ, не абсолютные (AC #6, критично для переносимости; осознанное расхождение с directaiq; решение D2).** directaiq делал **абсолютные** цели (`init_project.nu:86`: `let target = $"($project_root)/($path)"`). Для нас это **дефект переносимости**: NFR-2 требует перенос хранилища копированием папки между Windows и Linux (и между машинами) — абсолютная цель `G:\git\gamedev-analytics-unit\scripts` после копирования на другую машину/в другой путь **повиснет**. Цель **относительна каталогу самого линка**: `os.path.relpath(dev_repo_root / rel_path, start=(storage_root / rel_path).parent)`. Для типовой раскладки (хранилище — сосед dev-репо: `dev_repo_root.parent / {game}`) линк `{game}/scripts` укажет на `../gamedev-analytics-unit/scripts`. Пока копируют **пару каталогов вместе** (dev-репо + хранилище-сосед), относительные ссылки переживают перенос. `os.path.relpath` — **чистая строковая операция** (ФС не трогает) → тестируется без способности создавать симлинки.

3. **Preflight — проба РЕАЛЬНОГО симлинка в temp, fail-loud ДО разворачивания (AC #4; project-context.md:193; решение D3).** На Windows создание симлинка непривилегированным пользователем требует **Developer Mode** (или прав админа); иначе `os.symlink` бросает `OSError` с `winerror == 1314` (`ERROR_PRIVILEGE_NOT_HELD`). Preflight `preflight_symlink_capability(probe_dir=None)`: в свежем временном каталоге (`tempfile.mkdtemp`, по умолчанию; инъектируется для тестов) пробует создать симлинк на временную цель, сразу удаляет пробу (и каталог) в `finally`; успех → `return None`; `OSError`/`NotImplementedError` → **`SymlinkPreflightError`** с понятной инструкцией («включи Developer Mode: Параметры → Конфиденциальность и безопасность → Для разработчиков → Режим разработчика; или запусти от админа»). 4.3 зовёт preflight **первым шагом разворачивания симлинков**, ДО создания первого симлинка — чтобы непригодная платформа падала «насухо», не оставив частичный набор (AC #4 «ДО частичного разворачивания»). Linux создаёт симлинки нативно — там preflight всегда зелёный. Этот же приём закрывает defer истории 1.5 («битый симлинк / Dev Mode — риск флака на Windows CI»): тесты, которым нужен реальный симлинк, **гейтятся** результатом этой же пробы (см. риск №7).

4. **Существующий путь по адресу линка: СИМЛИНК → идемпотентная замена; РЕАЛЬНЫЙ файл/каталог → fail-loud (AC #7; осознанное расхождение с directaiq; решение D4).** directaiq на любом существующем не-симлинке делал `rm -rf` (`init_project.nu:73–75`) и `ln -sf` (force). Для нас `rm -rf` реального файла/каталога **опасен** (можно снести данные/правки) и противоречит этике «не сломать». Решение:
   - путь — **симлинк** (`os.path.islink` True): **идемпотентная замена** — снять (`os.unlink`) и создать заново с актуальной целью (повторный init / смена контракта не падает `FileExistsError`, ссылка приводится к контракту);
   - путь существует и **НЕ симлинк** (реальный файл/каталог): **fail-loud** `SymlinkError` («по пути линка лежит реальный файл/каталог, не симлинк — отказ удалять; разбери вручную») — НЕ `rm -rf`. На свежем хранилище (4.3 копирует шаблон, в котором этих путей нет) ветка не срабатывает; срабатывает только при ручном вмешательстве/legacy — и правильно останавливает.
   - путь отсутствует: создать (типовой путь свежего init).

5. **Откат частичного набора при сбое (TOCTOU; AC #9; решение D6).** Preflight доказал способность, но создание **конкретного** симлинка может упасть (гонка, права, исчезнувшая цель — TOCTOU). `create_symlinks` ведёт список **созданных/заменённых ЭТИМ вызовом** линков; при исключении в цикле — в `finally`/`except` снимает их (`os.unlink`, под `suppress(OSError)`) и **пробрасывает** исходную ошибку. Так не остаётся полу-связанного хранилища. **Граница:** откат 4.1 снимает только симлинки **этого вызова**; пред-существующие симлинки/каталоги не трогает; полный откат всего хранилища (скопированный шаблон, `.env`, БД) — забота 4.3 (epics.md:482), не дублировать здесь. Промежуточные родительские каталоги, созданные под вложенные записи контракта (если появятся), при откате не критичны (пустой каталог безвреден); фокус отката — симлинки.

6. **Контракт-CSV: SSOT, валидируется fail-loud; наполнение — стабильные существующие цели сейчас, расширение в 4.3 (AC #1/#2/#5/#8; решение D5).** Формат — RFC4180 CSV, колонки `path,comment` (форма directaiq), парсинг через `csv.DictReader` **как `catalog.py`** (НЕ `str.split(",")` — описания в `comment` могут содержать запятые). Валидация: заголовок == `("path", "comment")`; пустой контракт (ноль записей) → ошибка; дубли `path` → ошибка (коллизия); пустой/пробельный `path` → ошибка. **Наполнение shipped-файла сейчас** = только **существующие стабильные** цели dev-репо: `scripts`, `development-docs`, `yandex-docs`, `pyproject.toml`. `.mcp.json` приходит в **Epic 3** (его сейчас нет в dev-репо — проверено), `.claude/*` рантайм-записи агента (settings/hooks/commands/skills) — это **финализация состава при сборке init** (architecture.md:599 «точный состав `paths-to-symlink.csv` — финализировать при сборке init»), их дописывает 4.3 по мере появления артефактов. **AC #5 — страж:** если контракт называет цель, отсутствующую в dev-репо, `create_symlinks` падает понятной ошибкой ДО создания (битый контракт). Поэтому shipped-CSV не содержит ещё-несуществующих целей — иначе `gdau-init` 4.3 упал бы; механизм же 4.1 умеет любые записи (тестируется фикстурами).

7. **Тесты — чистая логика offline + реальные симлинки ПОД capability-skip; ветки сбоя через `monkeypatch` (кросс-платформенно, без флака на Windows-CI; решение D7).** CI гоняет ubuntu **и** windows; на windows-раннере способность создавать симлинки **не гарантирована** (Dev Mode), и история 1.5 уже отложила симлинк-тест именно из-за этого (deferred-work.md:23). Стратегия:
   - **чистые тесты (без симлинков, всегда идут):** `load_symlink_contract` (валидный / пустой / дубли / битый заголовок / отсутствие файла), относительная-цель-хелпер (чистый `os.path.relpath`), детекция отсутствующей цели, fail-loud-сообщения;
   - **ветка provala preflight (детерминированно, любая ОС):** `monkeypatch` `os.symlink` → `OSError(winerror=1314)`/`OSError` → `preflight_symlink_capability()` поднимает `SymlinkPreflightError`, сообщение содержит «Developer Mode»; проба за собой не оставляет файлов;
   - **реальное создание симлинков (gated):** фикстура зовёт `preflight_symlink_capability()`; не способна → `pytest.skip("нет способности создавать симлинки (Windows без Developer Mode)")`. Под гейтом: создание набора по контракту → проверка, что линки реальные (`is_symlink()`) и **относительные** (`os.readlink` не абсолютен); идемпотентность (повторный вызов на готовом наборе не падает, ссылки те же); замена существующего симлинка; fail-loud на реальном не-симлинке; **откат** — `monkeypatch` обёртка `os.symlink` (passthrough для первых K, `OSError` на K+1-м) → проверить, что созданные до сбоя сняты, ошибка проброшена;
   - **live-тест НЕ нужен** (нет внешнего API; правило opt-in live — только для Logs API, [[realapi-smoke-tests]]) — зафиксировать в Dev Agent Record, как 2.1–2.6.

## Acceptance Criteria

1. **Given** `templates/paths-to-symlink.csv`, **When** читается контракт, **Then** набор симлинкуемых путей задан декларативно (а не разбросан по коду init).
2. **Given** контракт, **When** создаются симлинки, **Then** хранилище ссылается на `scripts`, `development-docs`, `yandex-docs`, `.mcp.json`, `pyproject.toml`, `.claude/…` реальными symlink'ами.
3. **Given** обновление инструмента/каталога в dev-репо, **When** оно сделано, **Then** автоматически видно во всех хранилищах без копирования.
4. **Given** платформа без способности создавать symlink (Windows без Developer Mode), **When** выполняется preflight, **Then** fail-loud с инструкцией, ДО частичного разворачивания.
5. **Given** цель симлинка отсутствует в dev-репо (битый контракт), **When** создаётся симлинк, **Then** понятная ошибка.
6. **Given** перенос хранилища копированием папки Win↔Linux (NFR-2), **When** создаются симлинки, **Then** цели **относительные** (relative к корню хранилища), а не абсолютные — чтобы ссылки не ломались после копирования/переноса. _[edge-case: абсолютные цели ломают переносимость]_
7. **Given** симлинк по пути уже существует (повторный init / остаток), **When** он создаётся, **Then** случай обрабатывается явно (замена/skip), а не падение `FileExistsError`. _[edge-case: существующий симлинк]_
8. **Given** контракт CSV пустой/битый/с дублями, **When** он валидируется, **Then** понятная ошибка/дедуп, без полу-связанного хранилища. _[edge-case: битый контракт]_
9. **Given** preflight прошёл, но создание конкретного симлинка упало (TOCTOU), **When** это случилось, **Then** уже созданные симлинки откатываются (не остаётся частичного набора). _[edge-case: частичный набор симлинков]_

> **Примечание к AC #2 (состав целей).** Полный список целей AC #2 — это **итоговый** состав контракта к моменту сборки `gdau-init` (4.3). На момент 4.1 в dev-репо **уже есть** `scripts`/`development-docs`/`yandex-docs`/`pyproject.toml` — их shipped-CSV и перечисляет; `.mcp.json` (Epic 3) и `.claude/*` рантайм-записи дописываются в контракт по мере появления (architecture.md:599, решение D5). **Механизм** 4.1 (загрузка/preflight/создание/откат) работает с любым составом контракта — это и проверяют тесты (фикстуры с произвольными записями). AC #5 гарантирует, что отсутствующая в dev-репо цель → fail-loud, поэтому преждевременных записей в shipped-CSV нет.

## Tasks / Subtasks

- [ ] **Task 1 — `templates/paths-to-symlink.csv`: декларативный контракт (AC: #1, #2)**
  - [ ] Создать файл с заголовком `path,comment` (форма directaiq `templates/paths-to-symlink.csv`). Колонка `path` — путь относительно корня (и dev-репо, и хранилища одинаков); `comment` — человекочитаемое назначение (может содержать запятые → RFC4180-парсинг обязателен).
  - [ ] Наполнение — **только существующие стабильные цели dev-репо** (решение D5, проверено: существуют): `scripts`, `development-docs`, `yandex-docs`, `pyproject.toml`. **НЕ** включать `.mcp.json` (нет в dev-репо до Epic 3) и `.claude/*` рантайм-записи — иначе `create_symlinks` (AC #5) падал бы на отсутствующей цели; их дописывает 4.3 при финализации состава (architecture.md:599). В `comment` каждой записи — кратко «зачем» (напр. `scripts,код юнита (приём/MCP/init) — один источник на все игры`).
  - [ ] Файл коммитится (это контракт/шаблон, не данные). LF-окончания **обеспечены существующим `.gitattributes`** (`*.csv text eol=lf` + `* text=auto eol=lf`) — отдельных действий с `.gitattributes` не требуется (проверено).
  - [ ] **Хвостовой перевод строки допустим:** LF-нормализация обычно добавляет финальный `\n`; `csv.DictReader` пустую хвостовую строку **не отдаёт** как запись (см. Task 2 — валидатор «пустой path» не должен спотыкаться о trailing newline; покрыть тестом, Task 4).
- [ ] **Task 2 — `scripts/init/symlinks.py`: загрузка контракта + preflight + создание с откатом (AC: #1, #4–#9)**
  - [ ] `from __future__ import annotations` первой строкой. Русский модульный docstring: роль (декларативный контракт симлинков dev-репо↔хранилище + preflight способности + создание относительных симлинков с откатом частичного набора); границы: корни **инъектируются** (резолюция `GDAU_DATA_ROOT` — 4.3, не здесь), копирование шаблона — 4.2, оркестрация полного init и откат всего хранилища — 4.3, `.env`/БД/`git init` — 4.3. Импорты: `csv`, `logging`, `os`, `shutil`, `tempfile`, `from contextlib import suppress`, `from pathlib import Path`. **НЕ** импортировать `paths`/`database_manager`/`duckdb`/`metrica_client`/`parquet_store` (риск №1). `logger = logging.getLogger(__name__)`. `__all__` с публичными именами. Константы: `CONTRACT_COLUMNS = ("path", "comment")`; `_RESTKEY` (sentinel для `csv.DictReader`, как `catalog._RESTKEY` — ловить лишние незакавыченные колонки fail-loud); `DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "templates" / "paths-to-symlink.csv"` (как `catalog.DEFAULT_CATALOG_PATH`: `symlinks.py → init → scripts → корень`; резолвится сквозь симлинк хранилища в dev-репо).
  - [ ] **Иерархия исключений (fail-loud):**
    `class SymlinkContractError(ValueError): ...` — дефект контракта-CSV (пустой/битый заголовок/дубли/пустой path/отсутствие файла) → дефект данных → `ValueError` (как `catalog`).
    `class SymlinkTargetMissingError(SymlinkContractError): ...` — цель из контракта отсутствует в dev-репо (битый контракт, AC #5).
    `class SymlinkPreflightError(RuntimeError): ...` — платформа не умеет создавать симлинки (Windows без Dev Mode, AC #4) → инцидент окружения → `RuntimeError`; сообщение с инструкцией включить Developer Mode.
    `class SymlinkError(RuntimeError): ...` — сбой создания/замены конкретного симлинка (обёртка сырого `OSError`; реальный не-симлинк по пути — AC #7-fail; сбой в цикле — AC #9). **Никогда** сырой `OSError` наружу.
  - [ ] **`load_symlink_contract(contract_path: Path | None = None) -> list[str]` (AC #1, #8):** `None` → `DEFAULT_CONTRACT_PATH`. `not path.is_file()` (нет файла/битый симлинк) → `SymlinkContractError` с путём. Открыть `newline="", encoding="utf-8"`; `csv.DictReader(handle, restkey=_RESTKEY)` (как `catalog.py` — лишняя незакавыченная колонка → `row[_RESTKEY]` непуст → `SymlinkContractError`); `reader.fieldnames != list(CONTRACT_COLUMNS)` → `SymlinkContractError` (дрейф заголовка). Собрать `path`-значения: пустой/пробельный `path` → ошибка с номером строки; дубль `path` → `SymlinkContractError` (коллизия). Ноль валидных записей → `SymlinkContractError` («пустой контракт»). Вернуть упорядоченный `list[str]` (порядок файла — детерминизм). **Без `str.split(",")`** (риск №6). **Хвостовую пустую строку** `DictReader` не отдаёт записью → trailing newline (типовой для LF-файла) не должен валить «пустой path»; не «чинить» это вручную (`splitlines`/фильтры) — полагаться на `DictReader`.
  - [ ] **`preflight_symlink_capability(probe_dir: Path | None = None) -> None` (AC #4):** в `probe_dir` (по умолчанию свежий `tempfile.mkdtemp()`) создать временную цель-файл и попытаться `os.symlink(target, link)`; **успех** → `return None`; `OSError`/`NotImplementedError` → `raise SymlinkPreflightError(<инструкция Dev Mode>)`. Пробу (линк, цель, временный каталог) **удалить в `finally`** под `suppress(OSError)` — не оставлять мусора (даже на провале). Не зависеть от `os.name`/`sys.platform` в логике пробы — **проверяется реальной попыткой** (надёжнее флага платформы); инструкция в сообщении упоминает Windows Developer Mode (полезно там, где обычно и падает).
  - [ ] **`create_symlinks(*, dev_repo_root: Path, storage_root: Path, contract_path: Path | None = None, run_preflight: bool = True) -> list[Path]` (AC #2, #5, #6, #7, #9):**
    - если `run_preflight` → `preflight_symlink_capability()` первым делом (4.3 может звать preflight отдельно и передать `run_preflight=False`, но дефолт — самодостаточный fail-loud ДО создания, AC #4);
    - `rel_paths = load_symlink_contract(contract_path)`;
    - **предвалидация ВСЕХ целей** (AC #5, до создания первого линка): для каждого `rel` цель `target = dev_repo_root / rel`; `not target.exists()` (учесть `follow_symlinks`) → `SymlinkTargetMissingError(f"Цель контракта отсутствует в dev-репо: {target}")`. Падать «насухо», не создав ничего;
    - `created: list[Path] = []`; в `try`-блоке по каждому `rel` по порядку:
      - `link = storage_root / rel`; при вложенном `rel` создать родителей `link.parent.mkdir(parents=True, exist_ok=True)`;
      - **относительная цель (AC #6):** `rel_target = os.path.relpath(dev_repo_root / rel, start=link.parent)` (чистая строка; см. хелпер ниже);
      - **существующий путь (AC #7):** `link.is_symlink()` → `os.unlink(link)` (идемпотентная замена); `link.exists()` и НЕ символ → `SymlinkError("по пути линка реальный файл/каталог, не симлинк — отказ удалять")` (риск №4, НЕ `rm -rf`);
      - `target_is_dir = (dev_repo_root / rel).is_dir()`; `os.symlink(rel_target, link, target_is_directory=target_is_dir)` (на Windows `target_is_directory` обязателен для корректного dir-симлинка); `created.append(link)`;
    - **откат (AC #9):** обернуть цикл `try/except BaseException`; в `except` — для каждого `p` в `reversed(created)`: `with suppress(OSError): os.unlink(p)`; затем `raise` (проброс исходной ошибки). Сырой `OSError` от `os.symlink` в теле — завернуть в `SymlinkError` с путём перед откатом.
    - вернуть `created` (список созданных линков — для лога/диагностики 4.3).
  - [ ] **Чистый хелпер относительной цели (тестируемость, риск №2):** выделить `_relative_target(dev_repo_root: Path, storage_root: Path, rel: str) -> str` = `os.path.relpath(dev_repo_root / rel, start=(storage_root / rel).parent)`. Чистая строковая операция (ФС не трогает) → тест без способности создавать симлинки.
  - [ ] **НЕ делать:** абсолютные цели (риск №2 — рвут переносимость); `rm -rf`/удаление реального не-симлинка (риск №4); `os.symlink` без `target_is_directory` на dir-целях (битый dir-симлинк на Windows); preflight через флаг `os.name` вместо реальной пробы (риск №3); резолюцию `GDAU_DATA_ROOT`/импорт `paths` (риск №1 — корни инъектируются); сырой `OSError`/`csv`-исключение наружу (обернуть); `str.split(",")` для CSV (риск №6); полный откат хранилища (шаблон/`.env`/БД — 4.3, риск №5); сетевые вызовы/открытие `gdau.duckdb`.
- [ ] **Task 3 — Спека компонента `docs/init-and-storage.md` (новая, часть DoD)**
  - [ ] Завести `docs/init-and-storage.md` (project-context.md:64 — логический компонент «init + симлинки + два-репо»; 4.1 первая → создаёт файл, 4.2/4.3 дополнят). Человеческим языком, без жаргона/сигнатур. Раздел **«симлинк-контракт и preflight»** на три вопроса: **(1) Что делает** — при разворачивании игры связывает её папку с инструментами юнита **по явному списку** (`paths-to-symlink.csv`): код, каталог схемы, справочники, конфиги — это **ссылки** на dev-репо, а не копии; перед связыванием **проверяет**, что система вообще умеет создавать такие ссылки, и если нет — **сразу останавливается с понятной инструкцией**, ничего не разворачивая наполовину; **(2) Зачем** — чтобы исправление/обновление инструмента в одном месте (dev-репо) **сразу видели все игры** без копирования и рассинхрона; чтобы папку игры можно было **перенести на другую машину/ОС** копированием — ссылки заданы **относительно** и не рвутся; **(3) Контракт** — список ссылок — единый источник (правится в одном файле, не разбросан по коду); существующая ссылка при повторном развороте **обновляется** (не падает), а реальный файл/каталог на месте ссылки **останавливает** разворот (данные не удаляются); если в списке указана цель, которой нет в dev-репо, — **понятная ошибка**; если связать удалось частично и что-то сорвалось — уже созданные ссылки **снимаются** (нет полусвязанного состояния). **Границы:** копирование шаблона хранилища и файл описания игры — 4.2; полный разворот `gdau-init` (имя → шаблон → ссылки → `.env` → окружение → БД → git) и откат всего хранилища — 4.3; данные и `.env` живут в хранилище, инструмент — в dev-репо.
- [ ] **Task 4 — Offline-тесты `tests/test_init_symlinks.py` (AC: #1, #4–#9; решение D7)**
  - [ ] `from __future__ import annotations`; зеркалит `scripts/init/symlinks.py` → `tests/test_init_symlinks.py` (имя из architecture.md:485). Кросс-платформенно (`tmp_path`/`pathlib`); CI ubuntu + windows.
  - [ ] **Чистые тесты контракта (AC #1, #8 — всегда идут):** валидный CSV (`tmp_path`-фикстура) → ожидаемый `list[str]` в порядке файла; `comment` с запятой (закавыченной) не рвёт парсинг (RFC4180); **хвостовая пустая строка** (файл оканчивается `\n` — типовой LF-случай) парсится **без** ошибки (DictReader её не отдаёт); **лишняя незакавыченная колонка** (строка с двумя запятыми) → `SymlinkContractError` (restkey-страж); пустой контракт → `SymlinkContractError`; дубли `path` → `SymlinkContractError`; битый заголовок (напр. `path,note`) → `SymlinkContractError`; отсутствие файла / битый симлинк-путь → `SymlinkContractError` с путём; пустой/пробельный `path` → ошибка.
  - [ ] **Чистый тест относительной цели (AC #6):** `_relative_target(dev_repo_root, storage_root, "scripts")` при `storage_root = dev_repo_root.parent / "game1"` → `os.path.join("..", "gamedev-analytics-unit", "scripts")` (или эквивалент через `Path`); утверждать, что результат **не абсолютен** (`not os.path.isabs(...)`). Без создания симлинков.
  - [ ] **Ветка provala preflight (AC #4 — детерминированно, любая ОС):** `monkeypatch.setattr(os, "symlink", <raises OSError>)` → `preflight_symlink_capability(probe_dir=tmp_path)` → `pytest.raises(SymlinkPreflightError)`; сообщение содержит «Developer Mode» (или «Режим разработчика»); после вызова в `tmp_path` не осталось пробных файлов (проба убрана в `finally`).
  - [ ] **Детекция отсутствующей цели (AC #5):** контракт с `path`, которого нет в `dev_repo_root` → `create_symlinks(...)` → `pytest.raises(SymlinkTargetMissingError)`; в `storage_root` **не создано ни одного** симлинка (предвалидация до создания).
  - [ ] **Capability-gated фикстура:** фикстура зовёт `preflight_symlink_capability()`; `SymlinkPreflightError` → `pytest.skip("нет способности создавать симлинки (Windows без Developer Mode)")`. Все тесты ниже — под ней.
  - [ ] **Реальное создание + относительность (AC #2, #6, gated):** `dev_repo_root` с реальными `scripts/`(каталог)+`pyproject.toml`(файл) на `tmp_path`; `storage_root = tmp_path/"game"`; `create_symlinks(dev_repo_root=…, storage_root=…, contract_path=<фикстура>)` → для каждой записи `(storage_root/rel).is_symlink()` True; **проверять относительность как `not os.path.isabs(os.readlink(link))`** (НЕ сравнение строк — `\`/`/` и Windows-префиксы разъедут; `os.readlink` для относительной цели возвращает относительный путь — проверено); переход по ссылке резолвится в цель dev-репо; чтение содержимого через линк отдаёт файл dev-репо (AC #3 — правка в dev-репо видна через ссылку).
  - [ ] **AC #3 — осознанно без always-run теста:** единственная проверка AC #3 (read-through) — под capability-фикстурой (на Windows-CI без Dev Mode скипается). Реальное покрытие даёт ubuntu-прогон (симлинки нативны). Это by-design (как defer 1.5) — зафиксировать в Dev Agent Record, чтобы Acceptance Auditor не счёл дырой.
  - [ ] **Идемпотентность + замена существующего симлинка (AC #7, gated):** повторный `create_symlinks(...)` на готовом наборе **не падает** (`FileExistsError` не летит), ссылки на месте; предварительно подложить симлинк с «неправильной» целью → после вызова он указывает на правильную (замена).
  - [ ] **Реальный не-симлинк по пути → fail-loud (AC #7-fail, gated):** в `storage_root` создать реальный файл по адресу будущего линка → `create_symlinks(...)` → `pytest.raises(SymlinkError)`; реальный файл **не удалён** (риск №4).
  - [ ] **Откат частичного набора (AC #9, gated):** **вызвать `create_symlinks(..., run_preflight=False)`** (КРИТИЧНО — иначе проба `preflight_symlink_capability` сама зовёт `os.symlink` и съест первый расход счётчика обёртки, сдвинув арифметику «первых K»); обёртка `os.symlink` через `monkeypatch` — passthrough для первых K **контрактных** записей, `OSError` на (K+1)-й; `create_symlinks(...)` → `pytest.raises((SymlinkError, OSError))`; созданные до сбоя линки **сняты** (`not (storage_root/rel).exists()` для всех) — частичного набора нет.
  - [ ] **Анти-зависимость (через `ast`, по import-узлам):** в `scripts/init/symlinks.py` нет top-level импорта `pandas`/`polars`/`numpy`/`pyarrow`, directaiq-инфры `config_manager`/`base_script`, и нет `duckdb`/`scripts.utils.paths`/`scripts.utils.database_manager` (риск №1 — модуль независим, знает только ФС + контракт). Приём — `tests/test_parquet_store.py` (ast по import-узлам, не подстрока).
  - [ ] **Live-тест НЕ нужен** (и не заводить): 4.1 — ФС-операции, без внешнего API. Зафиксировать в Dev Agent Record (как 2.1–2.6), чтобы отсутствие live не сочли упущением.
- [ ] **Task 5 — Гейты верификации (обязательны перед закрытием)**
  - [ ] `uv run mypy scripts` → зелено (strict; `list[str]`/`list[Path]`; `os.symlink` с `target_is_directory`; без `Any`-дыр). Прогнать **win32 + `--platform linux`** (как 2.5/2.6/2.7 — кросс-OS strict).
  - [ ] `uv run pytest` → зелено (новый offline-набор + регрессия 1.x/2.x; live отсеян `addopts="-m 'not live'"`). Реальные симлинк-тесты идут или скипаются (Dev Mode) — **в логе убедиться, что не упали**; на ubuntu CI они выполняются (там симлинки нативны) — реальное покрытие AC #2/#6/#7/#9 обеспечивает Linux-прогон.
  - [ ] Новых зависимостей нет (`csv`/`os`/`tempfile`/`contextlib`/`pathlib`/`logging` — stdlib) → **`uv.lock` не меняется**.
  - [ ] `data/`-артефактов (`*.parquet`/`*.duckdb`/`.writer.lock`) в dev-репо не создано (4.1 их вообще не трогает). `templates/paths-to-symlink.csv` — новый коммитируемый файл.
  - [ ] Прогнать чек-лист «Definition of Done» из Dev Notes.

## Dev Notes

### Рекомендуемый контракт `symlinks.py` (финализируй под init_project.py 4.3)

| Имя | Сигнатура | Смысл |
|---|---|---|
| `SymlinkContractError` | `class(ValueError)` | дефект контракта-CSV (пустой/битый/дубли/нет файла) |
| `SymlinkTargetMissingError` | `class(SymlinkContractError)` | цель из контракта отсутствует в dev-репо (AC #5) |
| `SymlinkPreflightError` | `class(RuntimeError)` | платформа не умеет симлинки (Windows без Dev Mode, AC #4); сообщение с инструкцией |
| `SymlinkError` | `class(RuntimeError)` | сбой создания/замены симлинка (обёртка `OSError`; реальный не-симлинк; сбой в цикле AC #9) |
| `load_symlink_contract` | `(contract_path: Path \| None = None) -> list[str]` | RFC4180-CSV → упорядоченный список `path` (валидация fail-loud) |
| `preflight_symlink_capability` | `(probe_dir: Path \| None = None) -> None` | проба реального симлинка в temp; не умеет → `SymlinkPreflightError` |
| `create_symlinks` | `(*, dev_repo_root: Path, storage_root: Path, contract_path: Path \| None = None, run_preflight: bool = True) -> list[Path]` | относительные симлинки по контракту; предвалидация целей; откат при сбое |

**Использование (init_project.py, 4.3):**
```python
# после copy-template (4.2), до .env/uv sync/БД:
preflight_symlink_capability()            # fail-loud ДО любых мутаций симлинков (AC #4)
created = create_symlinks(                 # относительные цели, предвалидация, откат
    dev_repo_root=dev_repo_root,
    storage_root=storage_root,
    run_preflight=False,                   # уже сделан выше
)
# при сбое create_symlinks сам откатил свои симлинки; 4.3 откатывает остальное хранилище
```

### Образец загрузки контракта (зеркало `catalog.load_catalog`, риск №6)

```python
def load_symlink_contract(contract_path: Path | None = None) -> list[str]:
    resolved = contract_path if contract_path is not None else DEFAULT_CONTRACT_PATH
    if not resolved.is_file():
        raise SymlinkContractError(f"Контракт симлинков не найден (нет файла/битый симлинк): {resolved}")
    with resolved.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, restkey=_RESTKEY)
        if reader.fieldnames != list(CONTRACT_COLUMNS):
            raise SymlinkContractError(
                f"Заголовок контракта не соответствует ожидаемому. "
                f"Ожидалось {list(CONTRACT_COLUMNS)}, получено {reader.fieldnames}"
            )
        paths: list[str] = []
        seen: set[str] = set()
        for i, row in enumerate(reader, start=2):  # строка 1 — заголовок
            if row.get(_RESTKEY):  # лишняя незакавыченная колонка (сдвиг полей)
                raise SymlinkContractError(f"Лишние колонки в контракте (строка {i})")
            value = (row.get("path") or "").strip()
            if not value:
                raise SymlinkContractError(f"Пустой path в контракте (строка {i})")
            if value in seen:
                raise SymlinkContractError(f"Дубль path в контракте: {value!r} (строка {i})")
            seen.add(value)
            paths.append(value)
    if not paths:
        raise SymlinkContractError(f"Контракт симлинков пуст: {resolved}")
    return paths
```

### Механизм preflight (вариант реальной пробы, риск №3)

```python
def preflight_symlink_capability(probe_dir: Path | None = None) -> None:
    base = Path(tempfile.mkdtemp()) if probe_dir is None else probe_dir
    target = base / "_gdau_symlink_probe_target"
    link = base / "_gdau_symlink_probe_link"
    try:
        target.write_text("probe", encoding="utf-8")
        os.symlink(target, link)                     # OSError winerror=1314 без Dev Mode
    except (OSError, NotImplementedError) as exc:
        raise SymlinkPreflightError(
            "Система не умеет создавать символические ссылки. На Windows включи "
            "Developer Mode (Параметры → Конфиденциальность и безопасность → Для "
            "разработчиков → Режим разработчика) либо запусти от администратора. "
            f"Причина: {exc}"
        ) from exc
    finally:
        if probe_dir is None:
            shutil.rmtree(base, ignore_errors=True)  # свой temp — убрать целиком, без протечки
        else:
            with suppress(OSError):
                if link.is_symlink() or link.exists():
                    os.unlink(link)
            with suppress(OSError):
                target.unlink()
```

### Расхождения с directaiq (осознанные; трассируемость)

| Аспект | directaiq (`init_project.nu`) | gdau (4.1) | Почему |
|---|---|---|---|
| Язык | nushell | Python | architecture.md:427 (кросс-платформенно, без nushell) |
| Цель симлинка | **абсолютная** (`$project_root/$path`, строка 86) | **относительная** (`os.path.relpath`) | AC #6 / NFR-2 — перенос папки не рвёт ссылки |
| Существующий не-симлинк | `rm -rf` (строки 73–75) | **fail-loud**, не удаляем | этика «не сломать»; не сносить данные (риск №4) |
| Preflight Dev Mode | нет (POSIX `ln`) | **есть** (проба + fail-loud) | Windows-цель; project-context.md:193 |
| Откат частичного набора | нет | **есть** (AC #9) | целостность разворота, TOCTOU |
| Парсинг CSV | `from csv` (nu) | `csv.DictReader` (RFC4180) | как `catalog.py`, риск запятых в `comment` |

То, что **сохраняем** от directaiq: форма контракта `templates/paths-to-symlink.csv` (колонки `path,comment`), идея «один декларативный список — источник истины», создание родительских каталогов под вложенные записи, идемпотентная замена существующего симлинка.

### Паттерны от историй 1.x/2.x (соблюдать — снижают цикл ревью)

- `from __future__ import annotations` первой строкой; русский модульный docstring (роль + границы); идентификаторы английские, docstrings русские; type hints везде, `mypy --strict` по `scripts`, без `Any`-дыр; абсолютные импорты от корня пакета; `logger = logging.getLogger(__name__)` напрямую (НЕ заводить `logging_utils.py`).
- **Загрузчик-CSV — зеркало `catalog.load_catalog`:** `csv.DictReader` + `newline=""` + `encoding="utf-8"` + валидация заголовка кортежем + fail-loud с номером строки + дефолтный путь `Path(__file__).resolve().parents[2]/...` (артефакт dev-репо, не данные — резолвится сквозь симлинк хранилища).
- **Инъекция швов** (`dev_repo_root`/`storage_root`/`contract_path`/`probe_dir` — параметры, дефолты прод) — как `conn`/`lock_path`/`path` в 2.1–2.6; тесты на `tmp_path` без сети/БД.
- **Fail-loud русским сообщением + путь;** никогда сырой `OSError`/`csv`-исключение наружу (обернуть в доменный `*Error`-подкласс, паттерн ревью 2.1).
- **Анти-зависимость через `ast`** (import-узлы, не подстрока) — паттерн `tests/test_parquet_store.py`.
- **Live-набор осознанно отсутствует** (нет внешнего API) — зафиксировать, как 2.1–2.6.

### Границы 4.1 (не выходить)

- Артефакты: `templates/paths-to-symlink.csv` (новый), `scripts/init/symlinks.py` (новый), `docs/init-and-storage.md` (новый), `tests/test_init_symlinks.py` (новый). **Не** реализуем: проверку имени игры/резолюцию `../{game}` (4.3), копирование шаблона `external_storage/` и `PROJECT.md` (4.2), генерацию `.env`/`uv sync`/создание `gdau.duckdb`+view'ы/`git init`/полный откат хранилища (4.3), резолюцию `GDAU_DATA_ROOT` (4.3 даёт корни в 4.1 параметрами).
- `symlinks.py` в сеть не ходит, **не открывает `gdau.duckdb`**, не пишет данные/партиции, не берёт `.writer.lock`, не парсит TSV. Только: прочитать контракт, проверить способность, создать относительные симлинки с откатом.
- `init_project.py` остаётся **стабом** в 4.1 (его наполняет 4.3) — 4.1 не трогает `init_project.py`, кроме, при желании, импорта-заглушки нет; интеграция — в 4.3.

### Project Structure Notes

- Модуль — `scripts/init/symlinks.py` (architecture.md:464–465 называет `init/`; тест-зеркало `test_init_symlinks.py` строка 485). `scripts/init/` — регулярный пакет (`__init__.py` из 1.1). Имена snake_case; type hints обязательны.
- Контракт — `templates/paths-to-symlink.csv` (architecture.md:472 «декларативный симлинк-контракт (FR-20)»; форма directaiq строка 429). `templates/` сейчас содержит только `.gitkeep` — добавляем первый реальный артефакт.
- `docs/init-and-storage.md` — **новая** спека (project-context.md:64 — компонент «init + симлинки + два-репо»). Существующие спеки: `catalog.md`/`cli.md`/`creds.md`/`ingestion.md`/`metrica-client.md`/`working-layer.md` — не трогаем.
- `tests/` зеркалит `scripts/`: `tests/test_init_symlinks.py`. Конфиг pytest (`markers`/`addopts`) есть (1.3/1.6); `conftest.py` нет — `tmp_path`/`monkeypatch` напрямую.
- Не переводить на src-layout, не переименовывать пакет `scripts` (hatchling `packages=["scripts"]`). `uv.lock` не трогаем — всё stdlib.
- Симлинки в **хранилище** игнорируются git хранилища (`.gitignore` шаблона, 4.2) — их абсолютные/относительные пути не коммитятся в storage git; это забота 4.2, не 4.1. В **dev-репо** 4.1 коммитит только контракт-CSV + модуль + спеку + тест.

### Definition of Done — чек-лист самопроверки

1. `templates/paths-to-symlink.csv` создан: заголовок `path,comment`; записи — существующие стабильные цели (`scripts`/`development-docs`/`yandex-docs`/`pyproject.toml`); `.mcp.json`/`.claude/*` отложены до 4.3 (решение D5). (AC #1, #2)
2. `scripts/init/symlinks.py`: `SymlinkContractError`/`SymlinkTargetMissingError`/`SymlinkPreflightError`/`SymlinkError` + `load_symlink_contract`/`preflight_symlink_capability`/`create_symlinks` + чистый `_relative_target`. Корни инъектируются; НЕ импортирует `paths`/`database_manager`/`duckdb`. (AC #1, #4–#9)
3. Цели симлинков **относительные** (`os.path.relpath` от родителя линка), не абсолютные — `os.readlink` не абсолютен. (AC #6)
4. Preflight — реальная проба симлинка в temp; не умеет → `SymlinkPreflightError` с инструкцией Developer Mode; ДО разворачивания; проба убирается в `finally`. (AC #4)
5. Цель из контракта отсутствует в dev-репо → `SymlinkTargetMissingError` ДО создания первого линка. (AC #5)
6. Существующий **симлинк** → идемпотентная замена (не `FileExistsError`); существующий **реальный** файл/каталог → fail-loud `SymlinkError` (НЕ `rm -rf`). (AC #7)
7. Контракт пустой/битый заголовок/дубли/нет файла → `SymlinkContractError`; парсинг RFC4180 (`csv.DictReader`), не `split`. (AC #1, #8)
8. Сбой создания конкретного симлинка → откат созданных **этим вызовом** линков, проброс ошибки; пред-существующие не трогаются; полный откат хранилища — 4.3. (AC #9)
9. `docs/init-and-storage.md` заведён (3 вопроса простыми словами; границы 4.2/4.3 названы; «обновление в dev-репо видно всем играм»; «перенос не рвёт ссылки»). (Task 3)
10. Offline-тесты покрывают AC #1/#4–#9 + чистую логику + ветку provala preflight (monkeypatch) + capability-gated реальные симлинки + ast-анти-зависимость. Live осознанно отсутствует. (Task 4)
11. `uv run mypy scripts` (win32 + `--platform linux`) и `uv run pytest` — зелёные; реальные симлинк-тесты на ubuntu идут (нативно), на windows идут/скипаются (Dev Mode) — не падают; `uv.lock` не менялся; `data/`-артефактов в dev-репо нет. (Task 5)
12. Велась в отдельной ветке `story/4.1-symlink-contract-preflight` (новая история → новая ветка); merge в `main` только после зелёного CI на обеих ОС. PR в `main`.

### Latest Tech Information

- **`os.symlink(src, dst, target_is_directory=False)` (Python 3.13):** на POSIX `target_is_directory` игнорируется; **на Windows** определяет тип создаваемого симлинка (file vs directory) — для каталога-цели обязателен `True`, иначе ссылка на каталог битая. Без привилегий/Dev Mode на Windows бросает `OSError` с `winerror == 1314` (`ERROR_PRIVILEGE_NOT_HELD`). На очень старых/урезанных платформах возможен `NotImplementedError` (ловим оба в preflight).
- **`os.path.relpath(path, start)`** — чистая строковая операция (ФС не трогает, символы не резолвит) → тестируется детерминированно без создания симлинков и без реальных целей. Возвращает путь с разделителями текущей ОС; для записи в симлинк это корректно (линк создаётся на той же ОС, где будет читаться). Кросс-OS перенос относительной ссылки работает, пока структура каталогов сохраняется (NFR-2: копируют пару dev-репо+хранилище).
- **Windows Developer Mode и CI:** GitHub `windows-latest` раннеры **обычно** позволяют создавать симлинки (Dev Mode/привилегия включены для runner-пользователя), но это **не гарантия контракта** — отсюда capability-gated тесты со `skip` (надёжнее, чем рассчитывать на среду; перекликается с defer 1.5). Реальное покрытие AC #2/#6/#7/#9 даёт Linux-прогон (симлинки нативны).
- **`csv.DictReader` + `newline=""`** — как в `catalog.py`: без `newline=""` встроенные переводы строк в кавычках `comment` разобьются; `encoding="utf-8"` для кириллицы в комментариях.
- **Web-ресёрч не требуется:** stdlib стабилен и зафиксирован локом; внешнего сетевого контракта в истории нет (live-smoke неприменим, как 2.1–2.6).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 4.1] (строки 436–452) — user story + 9 AC (включая edge-cases: относительные цели, существующий симлинк, битый контракт, частичный набор).
- [Source: _bmad-output/planning-artifacts/epics.md#FR-20] (строка 54) — контракт симлинков задан декларативно (не разбросан по коду); обновление инструмента видно всем хранилищам.
- [Source: _bmad-output/planning-artifacts/epics.md#Epic 4] (строки 432–434) — место 4.1 в цепочке 4.1→4.3; разворот `gdau-init` одной командой.
- [Source: _bmad-output/planning-artifacts/epics.md#Additional Requirements] (строка 77) — «Реальные symlinks + preflight: контракт `templates/paths-to-symlink.csv`; init делает preflight Dev Mode, fail-loud с инструкцией».
- [Source: _bmad-output/planning-artifacts/epics.md#Story 4.3 AC] (строки 477, 482) — потребитель: `gdau-init` зовёт симлинки по контракту (4.1) + preflight; откат всего хранилища при сбое (граница 4.1↔4.3).
- [Source: _bmad-output/planning-artifacts/architecture.md#Directory Structure] (строки 464–465, 472, 485) — `scripts/init/init_project.py`; `templates/paths-to-symlink.csv` «декларативный симлинк-контракт (FR-20)»; `tests/test_init_symlinks.py`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Per-game storage tree] (строки 488–504) — состав симлинков хранилища: `scripts`, `development-docs`, `yandex-docs`, `.mcp.json`, `pyproject.toml`, `.claude/` → dev-репо.
- [Source: _bmad-output/planning-artifacts/architecture.md#Mapping table] (строки 427, 429) — init на Python (не nushell); `paths-to-symlink.csv` — тот же CSV-контракт.
- [Source: _bmad-output/planning-artifacts/architecture.md#FR→структура] (строка 532) — FR-19/20/21 → `init_project.py`, `paths-to-symlink.csv`, `PROJECT.md`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Integration & Data Flow] (строки 541–542) — init-поток: имя → шаблон → симлинки по CSV (+ preflight Dev Mode) → `.env` → `uv sync` → БД → `git init`.
- [Source: _bmad-output/planning-artifacts/architecture.md#Coherence] (строка 557) — symlinks совместимы с переносимостью: Linux нативно, Windows — Dev Mode + preflight.
- [Source: _bmad-output/planning-artifacts/architecture.md#Gap Analysis] (строки 599, 602) — «точный состав `paths-to-symlink.csv` финализировать при сборке init» (решение D5); «Windows Dev Mode не проверен — поймает preflight init».
- [Source: _bmad-output/project-context.md#Границы и каналы] (строка 122) — граница dev-репо↔хранилище: код/каталог/справочники приходят симлинками; данные/`.env` — в хранилище; в dev-репо данные не пишутся.
- [Source: _bmad-output/project-context.md#Workflow] (строка 168) — два репозитория раздельны: dev-репо (этот) versus per-game хранилище со своим `git init` (создаётся `gdau-init`).
- [Source: _bmad-output/project-context.md#Edge-кейсы] (строка 193) — «Preflight симлинков (Windows Dev Mode) — fail-loud с инструкцией».
- [Source: _bmad-output/project-context.md#Документация компонентов] (строки 52–77, 64) — на компонент `init-and-storage` нужна спека `docs/init-and-storage.md` (часть DoD).
- [Source: scripts/utils/catalog.py:41–48, 187–216] — образец загрузчика-CSV: `DEFAULT_*_PATH = Path(__file__).resolve().parents[2]/...`, `csv.DictReader` + `newline=""` + валидация заголовка кортежем + fail-loud с номером строки. Зеркало для `load_symlink_contract`.
- [Source: scripts/init/init_project.py:1–16] — текущий стаб `gdau-init` (печатает «not yet implemented», exit 0); наполняет 4.3, потребитель `symlinks.py`.
- [Source: tests/test_parquet_store.py] — паттерн: инъекция швов на `tmp_path`; анти-зависимость через `ast` (import-узлы). Зеркало для `test_init_symlinks.py`.
- [Source: _bmad-output/implementation-artifacts/deferred-work.md:23] — defer 1.5: «битый симлинк (AC#8) не покрыт тестом; кросс-OS создание симлинков на Windows требует Dev Mode (риск флака)» — решён capability-gated подходом 4.1 (риск №7).
- [Source: G:/git/directaiq/templates/paths-to-symlink.csv] — источник формы контракта (колонки `path,comment`).
- [Source: G:/git/directaiq/scripts/nushell/init_project.nu:54–93] — directaiq `create-symlinks` (nushell): абсолютные цели + `rm -rf` + `ln -sf` — переписываем на Python с расхождениями D2/D4 (см. таблицу).
- [Source: G:/git/directaiq/specs/20260515-external-storage-symlink-model.md] — модель симлинков external storage: зачем симлинки (один источник истины, обновление долетает до всех), контракт как SSOT, односторонность ссылок.
- [Memory: directaiq-vendor-source] — directaiq как источник формы/паттернов. [[feedback-decide-and-apply]] — Шеф делегирует решения D1–D7 («реши сам, надёжно»). [[realapi-smoke-tests]] — live применим только к внешнему API → в 4.1 не нужен. [[gdau-env-contract]] — `GDAU_DATA_ROOT` резолвит 4.3, не 4.1.

## Dev Agent Record

### Agent Model Used

_(заполняется dev-story)_

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-05-25 — **Независимое ревью (свежий контекст) — правки применены.** Вердикт «да, с правками». Применено: **К1** (critical) — тест отката AC #9 зовёт `create_symlinks(run_preflight=False)`, иначе проба preflight (`os.symlink`) съедает первый расход monkeypatch-счётчика и сдвигает арифметику «первых K»; **У1** — относительность проверять `not os.path.isabs(os.readlink(link))`, не сравнением строк; **У2** — `.gitattributes` уже покрывает `*.csv text eol=lf`, отдельных действий не нужно (убрана ложная задача); **У3** — хвостовая пустая строка LF-файла не должна валить валидатор (DictReader её не отдаёт) + тест-кейс; **У5** — AC #3 осознанно без always-run теста (только gated, покрытие — ubuntu), зафиксировать в Dev Agent Record; **У6** — очистка пробы preflight через `shutil.rmtree(ignore_errors=True)` при `probe_dir=None` (без протечки temp) + импорт `shutil`; **О1** — `csv.DictReader(restkey=_RESTKEY)` как `catalog.py` (лишняя незакавыченная колонка → fail-loud). Ревьюер перепроверил исполнением: формула относительной цели D2, порядок проверок D4 (битый симлинк→замена), winerror 1314, наполнение shipped-CSV (4 цели существуют, `.mcp.json`/`.claude/*` верно отложены), цитаты первоисточников и directaiq — всё корректно.
- 2026-05-25 — Story 4.1 создана (create-story): декларативный симлинк-контракт + preflight (FR-20) — первая история Epic 4 (init). Артефакты: `templates/paths-to-symlink.csv` (новый, форма directaiq `path,comment`), `scripts/init/symlinks.py` (новый: `load_symlink_contract` RFC4180 как `catalog.py` / `preflight_symlink_capability` проба реального симлинка в temp → fail-loud с инструкцией Dev Mode ДО разворачивания / `create_symlinks` относительные цели `os.path.relpath` + предвалидация целей + откат созданных этим вызовом при сбое), `docs/init-and-storage.md` (новая спека компонента), `tests/test_init_symlinks.py` (offline). Потребитель — `init_project.py` 4.3 (сейчас стаб); корни dev-репо/хранилища инъектируются (резолюция `GDAU_DATA_ROOT` — 4.3). **РЕШЕНИЯ D1–D7 зафиксированы** (Шеф делегировал, [[feedback-decide-and-apply]]): D1 модуль в `scripts/init/` (не `utils/`); D2 цели **относительные** (AC #6, перенос Win↔Linux — расхождение с directaiq абсолютными целями); D3 preflight = реальная проба + откат пробы, `winerror 1314`→`SymlinkPreflightError`; D4 существующий симлинк→замена, реальный файл/каталог→fail-loud (НЕ `rm -rf` directaiq); D5 shipped-CSV = существующие стабильные цели, `.mcp.json`(Epic 3)/`.claude/*` дописываются в 4.3 (architecture.md:599), AC #5 — страж missing-target; D6 откат только своих симлинков, полный откат хранилища — 4.3; D7 тесты: чистая логика + capability-gated реальные симлинки (GH windows без Dev Mode не валит) + ветка provala preflight через `monkeypatch` детерминированно (закрывает defer 1.5). Зависимостей нет (всё stdlib); live неприменим (ФС, без API). Epic 4 backlog → in-progress (первая история эпика). Статус → ready-for-dev.
