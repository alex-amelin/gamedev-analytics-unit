---
title: 'Шаблон хранилища: PROJECT.md → gdd.md + новый EVENTS.md'
type: 'chore'
created: '2026-05-31'
status: 'done'
baseline_commit: 'e40a6cb1c35c4437369290847215364c55f31166'
context: ['{project-root}/_bmad-output/project-context.md', '{project-root}/docs/init-and-storage.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Шаблон per-game хранилища несёт единственный файл описания игры `PROJECT.md`, где смешаны общий контекст игры и каталог целей/событий Метрики. Владельцу нужен отдельный файл под события аналитики, а имя `PROJECT.md` хочется заменить на `gdd.md` (game design document).

**Approach:** Переименовать файл-болванку `PROJECT.md` → `gdd.md`, вынести пункт про цели/события в новый структурированный `EVENTS.md`, расширить контракт шаблона (`REQUIRED_TEMPLATE_FILES` + `PRESERVE_ON_REPEAT`) и синхронно обновить тесты, докстринги и живую спеку. Логику копирования (`copy_storage_template`) не трогаем — она уже терпима к составу.

## Boundaries & Constraints

**Always:** Состав шаблона — единственный источник истины через `REQUIRED_TEMPLATE_FILES` и `PRESERVE_ON_REPEAT` в `scaffold.py`; менять имена только там и в синхронных тестах/спеке. `gdd.md` и `EVENTS.md` — оба в `PRESERVE_ON_REPEAT` (повторный `gdau-init` не затирает заполненное владельцем). Болванки несут маркер-плейсхолдер (`заполни`/`<!--`) и остаются урезанными (без `.claude/` и directaiq/marketing-маркеров). `git mv` для переименования (сохранить историю). Докстринги — на русском, «почему» а не «что».

**Ask First:** Любое расширение охвата за пределы живых артефактов (правка планировочных `epics.md`/`architecture.md` или story 4-2/4-3) — Шеф решил их НЕ трогать.

**Never:** Не менять сигнатуру/логику `copy_storage_template`, `init_project.py`, `symlinks.py` (кроме докстринг-упоминания). Не переименовывать пакет/модули. Не добавлять зависимостей. Не трогать историю BMAD и `sprint-status.yaml`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Свежий разворот | пустой `storage_root`, валидный шаблон (5 файлов) | копируются все 5, включая `gdd.md` и `EVENTS.md`; `copied` = 5 | N/A |
| Повторный разворот, оба заполнены | `gdd.md` и `EVENTS.md` уже есть в `storage_root` | оба сохранены (текст владельца не тронут); служебные 3 обновлены; `copied` = 3 | N/A |
| Повторный разворот, заполнен только gdd | `gdd.md` есть, `EVENTS.md` нет | `gdd.md` сохранён, `EVENTS.md` создан из шаблона; `copied` = 4 | N/A |
| Битый шаблон | в шаблоне нет `gdd.md` (или `EVENTS.md`) | `StorageTemplateError` ДО мутаций; `storage_root` не создан | fail-loud, путь в сообщении |

</frozen-after-approval>

## Code Map

- `scripts/init/scaffold.py` -- контракт шаблона: `REQUIRED_TEMPLATE_FILES`, `PRESERVE_ON_REPEAT`, докстринги/комментарии с упоминанием `PROJECT.md`
- `scripts/init/symlinks.py` -- докстринг (стр. 21) упоминает `PROJECT.md` как заботу 4.2
- `templates/external_storage/PROJECT.md` -- болванка описания игры → переименовать в `gdd.md`, убрать пункт про события
- `templates/external_storage/EVENTS.md` -- НОВЫЙ файл: структурированная болванка каталога событий
- `templates/external_storage/CLAUDE.md` -- стр. 8 ссылается на `PROJECT.md`; добавить ссылку на `EVENTS.md`
- `tests/test_init_scaffold.py` -- `_make_template`, тесты болванки и повторного init завязаны на `PROJECT.md`
- `tests/test_init_project.py` -- мини-шаблон (стр. 91), список файлов (стр. 250), git-tracked (стр. 295), битый шаблон (стр. 377)
- `docs/init-and-storage.md` -- раздел «Шаблон хранилища» (стр. 78–127): `PROJECT.md`, «четыре файла»

## Tasks & Acceptance

**Execution:**
- [x] `templates/external_storage/PROJECT.md` -- `git mv` в `gdd.md`; убрать буллит «Важные цели и события Метрики…», добавить ссылку «события — в `EVENTS.md`»; сохранить маркеры `заполни`
- [x] `templates/external_storage/EVENTS.md` -- создать болванку: вводный комментарий (заполняет владелец, агент читает) + таблица-плейсхолдер `Событие | Имя в Метрике | Что значит | Когда срабатывает` с примером-комментарием `<!-- заполни: … -->`
- [x] `templates/external_storage/CLAUDE.md` -- обновить ссылку `PROJECT.md` → `gdd.md`; добавить, что каталог событий — в `EVENTS.md`
- [x] `scripts/init/scaffold.py` -- `REQUIRED_TEMPLATE_FILES` += `gdd.md`, `EVENTS.md` (вместо `PROJECT.md`); `PRESERVE_ON_REPEAT = ("gdd.md", "EVENTS.md")`; обновить докстринги/комментарии (стр. 6, 9, 18, 46, 83, 86, 107, 113 «4 файла»→«5 файлов»)
- [x] `scripts/init/symlinks.py` -- докстринг стр. 21: `PROJECT.md` → `gdd.md`
- [x] `tests/test_init_scaffold.py` -- `_make_template`: заменить `PROJECT.md` на `gdd.md` + добавить `EVENTS.md`; переименовать `test_real_project_md_is_nonempty_placeholder` под `gdd.md` (+ проверка `EVENTS.md` непуст/плейсхолдер); переписать `test_repeat_init_preserves_filled_project_md`: оба файла заполнены→оба сохранены, `copied` = `len(REQUIRED)-2`; `test_incomplete_template_fails_before_mutations` unlink `gdd.md`
- [x] `tests/test_init_project.py` -- мини-шаблон пишет `gdd.md`+`EVENTS.md`; список ожидаемых файлов и git-tracked → `gdd.md`; битый шаблон unlink `gdd.md`
- [x] `docs/init-and-storage.md` -- раздел шаблона: `PROJECT.md`→`gdd.md`, «четыре»→«пять файлов», описать `EVENTS.md` и что оба файла бережутся при повторном развороте

**Acceptance Criteria:**
- Given валидный шаблон, when `uv run pytest tests/test_init_scaffold.py tests/test_init_project.py`, then все тесты зелёные и нет упоминаний `PROJECT.md` в живых артефактах.
- Given повторный `gdau-init` с заполненными `gdd.md` и `EVENTS.md`, when копирование, then оба файла сохранены дословно, а `.env.example`/`.gitignore`/`CLAUDE.md` обновлены из шаблона.
- Given шаблон без `gdd.md` или `EVENTS.md`, when `copy_storage_template`, then `StorageTemplateError` ДО создания `storage_root`.

## Verification

**Commands:**
- `uv run pytest tests/test_init_scaffold.py tests/test_init_project.py` -- expected: все зелёные
- `uv run mypy scripts` -- expected: strict без ошибок
- `git grep -in "PROJECT.md" scripts/ templates/ tests/ docs/` -- expected: ноль совпадений в живых артефактах

## Suggested Review Order

**Контракт состава шаблона (ядро изменения)**

- Точка входа: расширенный список обязательных файлов шаблона — +`gdd.md`, +`EVENTS.md`
  [`scaffold.py:44`](../../scripts/init/scaffold.py#L44)

- Оба owner-файла теперь бережём при повторном init (раньше только `PROJECT.md`)
  [`scaffold.py:55`](../../scripts/init/scaffold.py#L55)

- Логика пропуска бережёных файлов — не тронута, лишь подтянут комментарий
  [`scaffold.py:116`](../../scripts/init/scaffold.py#L116)

**Файлы шаблона**

- `PROJECT.md` → `gdd.md` (git mv): убран буллит событий, добавлена ссылка на `EVENTS.md`
  [`gdd.md:1`](../../templates/external_storage/gdd.md#L1)

- Новый каталог событий аналитики — структурированная болванка-таблица
  [`EVENTS.md:1`](../../templates/external_storage/EVENTS.md#L1)

- Инструкция агенту указывает на оба файла контекста
  [`CLAUDE.md:8`](../../templates/external_storage/CLAUDE.md#L8)

**Живая спека**

- Раздел шаблона: 4→5 файлов, описание `EVENTS.md`, оба бережутся
  [`init-and-storage.md:85`](../../docs/init-and-storage.md#L85)

**Тесты (опора)**

- Переписанный кейс повторного init: оба файла сохранены дословно, `copied == REQUIRED-2`
  [`test_init_scaffold.py:168`](../../tests/test_init_scaffold.py#L168)

- Болванки `gdd.md`/`EVENTS.md` непусты и несут плейсхолдер
  [`test_init_scaffold.py:109`](../../tests/test_init_scaffold.py#L109)

- Интеграционный init: 5 файлов на месте, `gdd.md`/`EVENTS.md` в git
  [`test_init_project.py:251`](../../tests/test_init_project.py#L251)
