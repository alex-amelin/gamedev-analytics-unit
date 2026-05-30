---
title: 'Суффикс -analytics к имени папки хранилища при gdau-init'
type: 'feature'
created: '2026-05-31'
status: 'done'
baseline_commit: 'c5bc775bc3a1b4c9be87e0a55d745d7ac9282498'
context: ['{project-root}/_bmad-output/project-context.md', '{project-root}/docs/init-and-storage.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `gdau-init {game}` разворачивает хранилище в папке `../{game}` — имя папки совпадает с именем игры, рядом с dev-репо это не отличает аналитический сторадж от других каталогов игры.

**Approach:** Добавить фиксированный суффикс `-analytics` к **имени папки/пути** хранилища: целевой путь становится `../{game}-analytics`. Суффикс применяется ровно в одной точке сборки пути (`_resolve_storage_root`), откуда естественно распространяется на проверку занятости имени, откат, генерацию `.env` (`GDAU_DATA_ROOT`), создание БД и финальные сообщения. Имя игры (`game`) в логах и git-коммите остаётся **без** суффикса.

## Boundaries & Constraints

**Always:**
- Суффикс задаётся одной модульной константой (напр. `STORAGE_NAME_SUFFIX = "-analytics"`), не литералом по коду.
- Применять суффикс только при сборке пути в `_resolve_storage_root`; не дублировать в других местах.
- Валидация имени игры (`_validate_game_name`) и текст git-коммита (`init: ... игры {game}`) оперируют именем **без** суффикса.
- Docstrings, упоминающие `../{game}`, и человекочитаемая спека `docs/init-and-storage.md` обновляются в этом же изменении (DoD: контракт ↔ спека не расходятся).

**Ask First:**
- Нет открытых решений. (Двойной суффикс при имени игры, уже оканчивающемся на `-analytics`, — намеренно не защищаем: append буквальный, простота-первой.)

**Never:**
- Не менять сигнатуру/швы `init_storage`/`_resolve_storage_root` (`dev_repo_root`/`storage_parent`/`runner`) и порядок шагов разворота.
- Не трогать резолюцию `paths.py`/`GDAU_DATA_ROOT` (она от абсолютного пути, а не от имени игры — суффикс попадает автоматически).
- Не делать суффикс настраиваемым флагом CLI — фиксированный.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Happy path | `gdau-init mygame`, путь свободен | Хранилище развёрнуто в `../mygame-analytics`; `GDAU_DATA_ROOT` указывает туда; коммит `init: ... игры mygame` | N/A |
| Имя занято | `../mygame-analytics` уже существует | Fail-loud «Имя занято» ДО мутаций; данные владельца нетронуты | StorageInitError, exit 1 |
| Явный parent | `storage_parent=P`, игра `g` | Путь `P/g-analytics` | N/A |
| Игра уже `*-analytics` | `gdau-init foo-analytics` | Папка `foo-analytics-analytics` (буквальный append, допустимо) | N/A |

</frozen-after-approval>

## Code Map

- `scripts/init/init_project.py` -- `_resolve_storage_root` (стр. 156) — единственная точка сборки `parent / game`; сюда добавляется суффикс + константа. Также module-docstring (стр. 4) и docstring функции упоминают `../{game}`.
- `tests/test_init_project.py` -- ассерты ожидаемого пути и предсоздаваемые «занятые» папки используют `parent/"mygame"` (стр. 168, 171, 178, 186, 202, 228, 247, 362, 384) — должны стать суффиксными.
- `docs/init-and-storage.md` -- человекочитаемая спека: раздел про разворот/именование папки (≈ стр. 138–166) — отметить суффикс `-analytics`.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/init/init_project.py` -- ввести `STORAGE_NAME_SUFFIX = "-analytics"`; в `_resolve_storage_root` вернуть `parent / f"{game}{STORAGE_NAME_SUFFIX}"`; обновить docstring функции и module-docstring (`../{game}` → `../{game}-analytics`) -- единая точка изменения пути.
- [x] `tests/test_init_project.py` -- обновить ожидаемые пути на суффиксные; в тестах «имя занято» предсоздавать **суффиксную** папку (иначе тест перестаёт ловить занятость); добавить явный `test_resolve_storage_root_appends_analytics_suffix` -- зафиксировать поведение для DoD.
- [x] `docs/init-and-storage.md` -- зафиксировать словами, что папка хранилища именуется `{game}-analytics` (имя игры в сообщениях — без суффикса) -- спека ↔ контракт.

**Acceptance Criteria:**
- Given свободный путь, when `init_storage("mygame", storage_parent=P)`, then возвращается `P/"mygame-analytics"` и каталог создан там же.
- Given существующий `P/"mygame-analytics"`, when `init_storage("mygame", storage_parent=P)`, then fail-loud `StorageInitError` («Имя занято») ДО мутаций.
- Given успешный разворот, when читается `.env`, then `GDAU_DATA_ROOT` указывает на `...-analytics`, а git-коммит содержит имя игры **без** суффикса.
- Given весь набор, when `uv run pytest` и `uv run mypy scripts`, then зелено на обеих ОС.

## Verification

**Commands:**
- `uv run pytest tests/test_init_project.py` -- expected: все тесты зелёные, включая новый суффикс-тест
- `uv run pytest` -- expected: полный offline-набор зелёный (нет регресса в scaffold/symlinks)
- `uv run mypy scripts` -- expected: strict без ошибок

## Suggested Review Order

**Суть изменения (точка входа)**

- Единственная точка сборки пути: суффикс через константу, не литерал.
  [`init_project.py:176`](../../scripts/init/init_project.py#L176)
- Источник истины суффикса + обоснование (только имя папки, не имя игры).
  [`init_project.py:82`](../../scripts/init/init_project.py#L82)

**Контракт и документация**

- Человекочитаемая спека: папка `{game}-analytics`, имя игры в коммите без суффикса.
  [`init-and-storage.md:159`](../../docs/init-and-storage.md#L159)

**Тесты (последними)**

- Явный тест суффикса + краевой случай двойного `-analytics`.
  [`test_init_project.py:181`](../../tests/test_init_project.py#L181)
- Закрепление AC-3: коммит несёт имя игры без суффикса.
  [`test_init_project.py:307`](../../tests/test_init_project.py#L307)
