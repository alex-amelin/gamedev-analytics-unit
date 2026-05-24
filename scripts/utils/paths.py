"""Единственная точка резолюции путей per-game хранилища.

По корню рабочего пространства игры (переменная окружения ``GDAU_DATA_ROOT``)
этот модуль знает, где в хранилище лежат сырьё (``data/raw/{source}/{date}.parquet``),
сама база (``data/duckdb/gdau.duckdb``) и файл-замок записи (``.writer.lock``).
Фундамент пути записи Epic 2 и чтения Epic 3/4: на эти резолверы встанут
``parquet_store`` (2.2), ``load_state`` (2.4), ``writer_lock`` (2.5), ``views`` (2.6),
оркестратор p81 (2.7), MCP-чтение (3.1) и init (4.3).

Все ``get_*`` — **чистые функции**: они только строят/валидируют пути и НИКОГДА не
создают каталогов (инвариант project-context «в dev-репо данные не пишутся»). Нет
корня / корень не существует на диске → fail-loud (:class:`ValueError`) ДО построения
любого пути, чтобы битая конфигурация не оседала мусорными каталогами.

Это **не построчный вендоринг** directaiq-``paths.py``: узнаваемая форма ``get_*``-
резолверов сохранена, но вся обвязка directaiq вырезана — ``_ensure_external_storage_
initialized`` с ``mkdir`` на корне, ``_load_env_with_fallback`` (``load_dotenv``),
``setup_paths`` (``sys.path``-хаки) и fallback old/new-структур (NFR-6, [[directaiq-reference]]).

**Шов с env_reader (1.2) — общая переменная, разные политики.** Имя переменной берётся
из единой константы :data:`scripts.utils.env_reader.DATA_ROOT_ENV` (не второй литерал —
рассинхрон имени = тихий баг). Загрузку ``.env`` делает только ``env_reader``; этот
модуль ничего не грузит, лишь читает уже разрешённое окружение. Осознанная асимметрия:
для ``env_reader`` отсутствие ``GDAU_DATA_ROOT`` — **не-фатал** (креды могут прийти
прямо в процесс-окружение), а здесь — **жёсткий fail** (без корня хранилища данные
негде брать/писать). Это не противоречие — разные зоны ответственности (креды vs данные).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from scripts.utils.catalog import VALID_SOURCES
from scripts.utils.env_reader import DATA_ROOT_ENV

logger = logging.getLogger(__name__)

__all__ = [
    "get_storage_root",
    "get_db_path",
    "get_raw_partition_path",
    "get_raw_source_dir",
    "get_writer_lock_path",
]


def get_storage_root() -> Path:
    """Вернуть корень per-game хранилища из ``GDAU_DATA_ROOT`` или fail-loud (AC #1, #5).

    Переменная не задана / пуста / из пробелов → :class:`ValueError` (нет корня —
    некуда писать/читать данные). **Относительный путь** → :class:`ValueError`: он
    резолвился бы против текущего рабочего каталога и при запуске из dev-репо мог бы
    увести запись ВНУТРЬ dev-репо (инвариант «в dev-репо данные не пишутся»). Абсолютное
    значение приводится к каноничному виду через ``.resolve()`` (проходит сквозь симлинки
    хранилища); ошибка разрешения (отказ прав / симлинк-петля / слишком длинный путь)
    тоже заворачивается в :class:`ValueError`, чтобы не выпускать сырой ``OSError`` мимо
    контракта fail-loud. Если разрешённый путь не существует или это не каталог →
    :class:`ValueError`. **Никакого ``mkdir``** — резолвер чистый: корень создаёт init
    (4.3), здесь его только валидируем. Так «мусорная резолюция» при битом корне падает
    ДО построения путей, не оставляя каталогов (особенно в dev-репо).
    """
    raw = os.environ.get(DATA_ROOT_ENV)
    value = raw.strip() if raw is not None else ""
    if not value:
        raise ValueError(
            f"Переменная {DATA_ROOT_ENV} не задана или пуста — неизвестен корень "
            f"хранилища игры. Запусти gdau-init или задай {DATA_ROOT_ENV} на каталог хранилища."
        )
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError(
            f"{DATA_ROOT_ENV} должен быть абсолютным путём, получено относительное: "
            f"{value!r}. Запусти gdau-init или задай {DATA_ROOT_ENV} на абсолютный каталог хранилища."
        )
    try:
        root = candidate.resolve()
        is_directory = root.is_dir()
    except OSError as exc:
        raise ValueError(
            f"Не удалось разрешить корень хранилища из {DATA_ROOT_ENV} ({value!r}): {exc}"
        ) from exc
    if not is_directory:
        raise ValueError(
            f"Корень хранилища из {DATA_ROOT_ENV} не существует или не является "
            f"каталогом: {root}. Запусти gdau-init для разворачивания хранилища."
        )
    return root


def get_db_path() -> Path:
    """Путь к встроенной БД DuckDB: ``{root}/data/duckdb/gdau.duckdb`` (AC #1).

    Чистая резолюция (без ``mkdir``): родителя при записи создаёт ``database_manager``
    в write-режиме, не этот резолвер.
    """
    return get_storage_root() / "data" / "duckdb" / "gdau.duckdb"


def get_raw_partition_path(source: str, date: str) -> Path:
    """Путь партиции дня: ``{root}/data/raw/{source}/{date}.parquet`` (AC #1).

    ``source`` ∈ {visits, hits} (валидируется fail-loud — мусорный источник не должен
    молча резолвиться в путь). ``date`` — уже отформатированная строка ``YYYY-MM-DD``;
    форматирование/валидация дат — зона ``dates.py`` (1.4), здесь не дублируется.
    """
    _require_valid_source(source)
    return get_raw_source_dir(source) / f"{date}.parquet"


def get_raw_source_dir(source: str) -> Path:
    """Каталог источника сырья: ``{root}/data/raw/{source}`` (AC #1; для views.py 2.6).

    ``source`` ∈ {visits, hits} (fail-loud). Чистая резолюция, без ``mkdir``.
    """
    _require_valid_source(source)
    return get_storage_root() / "data" / "raw" / source


def get_writer_lock_path() -> Path:
    """Путь файла-замка писателя: ``{root}/.writer.lock`` (AC #1).

    Только путь — захват/освобождение лока это story 2.5, не здесь.
    """
    return get_storage_root() / ".writer.lock"


def _require_valid_source(source: str) -> None:
    """Провалидировать имя источника или fail-loud (переиспользует VALID_SOURCES каталога)."""
    if source not in VALID_SOURCES:
        raise ValueError(
            f"Неизвестный source: {source!r} (ожидается один из {VALID_SOURCES})"
        )
