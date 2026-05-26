"""Копирование статического шаблона хранилища в новое per-game рабочее пространство.

Роль. Вторая часть Epic 4 (init): dev-репо несёт версионируемый шаблон
``templates/external_storage/`` — образец файла кредов (``.env.example``), правила
игнорирования для git хранилища (``.gitignore``), инструкции агенту рабочего
пространства (``CLAUDE.md``) и файл описания игры (``PROJECT.md``). Этот модуль —
ПРИМИТИВ копирования шаблона в папку игры: валидирует наличие шаблона fail-loud ДО
любых мутаций (битый шаблон не родит полу-пустое хранилище, AC #5), копирует файлы и
НЕ затирает уже заполненный владельцем ``PROJECT.md`` при повторном развороте (AC #6).

Границы (что НЕ здесь). Корни ``storage_root``/``template_root`` ИНЪЕКТИРУЮТСЯ:
резолюцию ``GDAU_DATA_ROOT`` и имя игры делает оркестратор 4.3, не этот модуль (шов
инъекции, как ``conn``/``path`` в 2.x и ``dev_repo_root``/``storage_root`` в
``symlinks.py`` 4.1). Создание симлинков на инфру dev-репо — 4.1 (``symlinks.py``).
Генерация реального ``.env`` из образца, ``uv sync``, создание ``gdau.duckdb`` +
представлений, ``git init`` и ПОЛНЫЙ откат хранилища при сбое посреди init — 4.3. Этот
примитив частичную копию НЕ откатывает (граница D6): он лишь валидирует, копирует и
бережёт ``PROJECT.md``. Модуль в сеть не ходит, ``gdau.duckdb`` не открывает, данные/
партиции не пишет, ``.writer.lock`` не берёт, симлинки не создаёт, TSV/CSV не парсит.
Зависит только от stdlib; НЕ импортирует ``paths``/``database_manager``/``duckdb``/
``metrica_client``/``symlinks``.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Дефолтный путь к шаблону резолвится ОТ МОДУЛЯ, а не от cwd: шаблон — артефакт dev-репо
# (`templates/`), он путешествует с кодом (в per-game хранилище инфра приходит симлинком),
# а НЕ данные оператора. `.resolve()` проходит сквозь симлинк хранилища в dev-репо, где
# `templates/` и `scripts/` — реальные соседи. parents[2]: scaffold.py → init → scripts →
# корень репо. Зеркало catalog.DEFAULT_CATALOG_PATH / symlinks.DEFAULT_CONTRACT_PATH.
DEFAULT_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[2] / "templates" / "external_storage"
)

# Обязательный состав шаблона: контракт AC #1 + страж валидации AC #5. Отсутствие любого
# из этих файлов в template_root → fail-loud ДО мутаций (полу-битое хранилище не родим).
REQUIRED_TEMPLATE_FILES = (".env.example", ".gitignore", "CLAUDE.md", "PROJECT.md")

# Имена, которые при повторном развороте НЕ перезаписываем, если уже есть в хранилище:
# PROJECT.md заполняет владелец — затереть его шаблоном = потерять описание игры (AC #6).
PRESERVE_ON_REPEAT = ("PROJECT.md",)

__all__ = [
    "StorageTemplateError",
    "DEFAULT_TEMPLATE_ROOT",
    "REQUIRED_TEMPLATE_FILES",
    "PRESERVE_ON_REPEAT",
    "copy_storage_template",
]


class StorageTemplateError(RuntimeError):
    """Шаблон отсутствует/битый (AC #5) или сбой копирования (обёртка ``OSError``).

    Наследует :class:`RuntimeError` — инцидент окружения (нет шаблона / диск полон / нет
    прав), не дефект данных. Сырой ``OSError``/``shutil``-исключение наружу не выпускаем
    (паттерн ревью 2.1 / 4.1): всегда оборачиваем сюда с путём для диагностики.
    """


def copy_storage_template(
    *, storage_root: Path, template_root: Path | None = None
) -> list[Path]:
    """Скопировать шаблон хранилища в ``storage_root`` (AC #2, #5, #6).

    ``storage_root`` — корень папки игры (резолвит 4.3; может не существовать — создаётся, либо
    существует при повторном развороте). ``template_root`` — корень шаблона; ``None`` →
    :data:`DEFAULT_TEMPLATE_ROOT` (артефакт dev-репо). Оба — инъектируемые швы (тесты дают
    ``tmp_path``, прод даёт реальные пути).

    Порядок строго: **сначала** полная валидация шаблона fail-loud (AC #5) — ``template_root``
    не каталог ИЛИ не хватает любого из :data:`REQUIRED_TEMPLATE_FILES` →
    :class:`StorageTemplateError` ДО создания/копирования чего-либо в ``storage_root`` (чтобы не
    родить полу-битое хранилище). **Затем** мутации: создать ``storage_root`` (идемпотентно) и
    скопировать записи шаблона в детерминированном порядке (``sorted``). Запись из
    :data:`PRESERVE_ON_REPEAT`, уже существующая в ``storage_root`` (владелец заполнил
    ``PROJECT.md``), **пропускается** — не затирается (AC #6).

    Возвращает список фактически скопированных путей (для лога/диагностики 4.3; пропущенный
    ``PROJECT.md`` в него не входит). Частичную копию НЕ откатывает — полный откат хранилища
    при сбое посреди init делает 4.3 (граница D6). Сырой ``OSError`` от ``shutil``/``mkdir``
    оборачивается в :class:`StorageTemplateError` с путём.
    """
    root = template_root if template_root is not None else DEFAULT_TEMPLATE_ROOT

    # --- Валидация ДО любых мутаций (AC #5): падаем «насухо», ничего не создав в storage_root.
    if not root.is_dir():
        raise StorageTemplateError(f"Шаблон хранилища не найден/не каталог: {root}")
    for name in REQUIRED_TEMPLATE_FILES:
        if not (root / name).is_file():
            raise StorageTemplateError(
                f"В шаблоне отсутствует обязательный файл: {name} (шаблон: {root})"
            )

    # --- Мутации: создать корень и скопировать. Откат частичной копии — у 4.3 (граница D6).
    try:
        storage_root.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for entry in sorted(root.iterdir()):
            dest = storage_root / entry.name
            # Сохранение заполненного владельцем PROJECT.md при повторном init (AC #6).
            if entry.name in PRESERVE_ON_REPEAT and dest.exists():
                logger.info(
                    "Сохраняю существующий %s (повторный init — не затираю)", entry.name
                )
                continue
            # Шаблон сейчас плоский (4 файла), но код терпим к будущим подкаталогам.
            if entry.is_dir():
                shutil.copytree(entry, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, dest)
            copied.append(dest)
    except OSError as exc:  # диск полон / нет прав — оборачиваем; откат хранилища у 4.3 (D6)
        raise StorageTemplateError(
            f"Сбой копирования шаблона в {storage_root}: {exc}"
        ) from exc

    return copied
