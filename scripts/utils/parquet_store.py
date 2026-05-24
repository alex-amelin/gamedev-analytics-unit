"""Единственная точка атомарной записи сырья одного дня в Parquet-партицию.

Берёт разобранные строки одного дня одного источника (``visits``/``hits``) и кладёт их
в один файл-партицию ``data/raw/{source}/{date}.parquet`` под корнем хранилища игры.
Значения пишутся **строками как пришли** (TSV-ячейки дословно, массивы — строкой);
единственное рантайм-преобразование — **переименование колонок** из родных имён Метрики
(``ym:s:*``/``ym:pv:*``) в storage-имена ``snake_case`` по каталогу-SSOT (1.5). Никакого
``CAST``/усечения/дедупа в сырьевом слое — типизация это забота view (2.6).

**Атомарность (FR-14):** запись идёт в ``{date}.parquet.tmp`` в том же каталоге, затем
:func:`os.replace` атомарно подменяет финальную партицию (и на POSIX, и на Windows). Сбой
посреди записи не оставляет «полу-дня»: либо целая партиция, либо прежнее состояние.

**Parquet пишется встроенным DuckDB, БЕЗ pandas/polars/pyarrow** (project-context: тяжёлый
аналитический стек запрещён; ``duckdb`` уже в зависимостях). Поднимается транзиентное
in-memory соединение-«кодировщик» и закрывается в ``finally`` — это НЕ рабочая база
``gdau.duckdb`` и НЕ :class:`DatabaseManager` (сырьевые партиции — самостоятельные файлы,
к рабочей базе отношения не имеют).

**Границы (что делает ДРУГОЙ компонент, не этот):** ``.writer.lock`` вокруг записи берёт
оркестратор p81 (2.5/2.7); жёсткую сверку числа строк делает 2.3; учёт загруженных дней
(``load_state``) — 2.4; типизированные view с ``TRY_CAST`` и парсингом массивов — 2.6;
сборку дня из TSV-частей и весь цикл приёма — p81 (2.7). Модуль в сеть не ходит и
``gdau.duckdb`` не открывает.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable, Sequence

import duckdb

from scripts.utils.catalog import Catalog, load_catalog
from scripts.utils.paths import get_raw_partition_path

logger = logging.getLogger(__name__)

# Имя транзиентной таблицы в in-memory кодировщике. Соединение одноразовое и приватное,
# поэтому коллизий нет; имя с подчёркиванием подчёркивает «внутреннее, не наружу».
_TMP_TABLE = "_raw"

__all__ = ["write_partition"]


def write_partition(
    source: str,
    date: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[str | None]],
    *,
    catalog: Catalog | None = None,
) -> int:
    """Атомарно записать сырьё одного дня одного источника в Parquet-партицию (AC #1–#7).

    :param source: ``visits``/``hits`` (валидируется через :func:`get_raw_partition_path`).
    :param date: уже отформатированная дата ``YYYY-MM-DD`` (форматирование дат — ``dates.py`` 1.4).
    :param columns: родные имена Метрики в порядке TSV-заголовка (``ym:s:visitID``, …) —
        задают и схему, и порядок колонок партиции.
    :param rows: разобранные строки дня; TSV-ячейки дословно (``str``/``None``, массивы —
        строкой). ``[]`` = легитимно пустой день (AC #7), не ошибка.
    :param catalog: инъектируемый шов; ``None`` → :func:`load_catalog` (прод-путь).
    :returns: число записанных строк данных (для p81/сверки 2.3).
    :raises ValueError: пустой список колонок, неизвестная колонка/источник, коллизия
        storage-имени, неверная ширина строки, битый корень хранилища (наследуется из ``paths``).
    :raises RuntimeError: ОС-сбой создания каталога партиции или атомарной замены (не сырой ``OSError``).
    """
    column_list = list(columns)
    if not column_list:
        raise ValueError(
            "Список колонок пуст — нет схемы партиции и нечего писать "
            f"(источник {source!r}, дата {date!r})."
        )

    # Резолюция пути валидирует source и наследует fail-loud корня хранилища (paths 2.1)
    # ДО любого mkdir/записи — битая конфигурация падает без побочных эффектов.
    partition_path = get_raw_partition_path(source, date)

    # Единственное преобразование (AC #1): родное имя Метрики → storage_name по каталогу.
    effective_catalog = catalog if catalog is not None else load_catalog()
    name_map = {f.metrica_field: f.storage_name for f in effective_catalog.fields_for(source)}
    storage_names: list[str] = []
    for metrica_field in column_list:
        try:
            storage_names.append(name_map[metrica_field])
        except KeyError:
            raise ValueError(
                f"Колонка {metrica_field!r} отсутствует в каталоге для источника "
                f"{source!r} (поле без записи в каталоге = дефект)."
            ) from None
    if len(set(storage_names)) != len(storage_names):
        raise ValueError(
            f"Коллизия storage-имён после переименования по каталогу: {storage_names} "
            f"(источник {source!r})."
        )

    # Материализуем строки один раз (rows может быть генератором) и проверяем ширину
    # fail-loud — сырьё пишется без молчаливого паддинга/усечения (FR-6).
    width = len(column_list)
    rows_list: list[tuple[str | None, ...]] = []
    for index, row in enumerate(rows):
        cells = tuple(row)
        if len(cells) != width:
            raise ValueError(
                f"Строка {index}: ширина {len(cells)} не совпадает с числом колонок "
                f"{width} (сырьё пишется без паддинга/усечения)."
            )
        # Сырьё пишется строками дословно. Значение вне str|None DuckDB молча привёл бы к
        # VARCHAR через repr (list → "[1, 2, 3]" с пробелами ≠ исходный TSV) — поэтому
        # fail-loud на контрактном нарушении, чтобы не исказить сырьё тихо (raw-integrity).
        for col_index, cell in enumerate(cells):
            if cell is not None and not isinstance(cell, str):
                raise ValueError(
                    f"Строка {index}, колонка {col_index} ({column_list[col_index]!r}): "
                    f"значение типа {type(cell).__name__!r} — ожидалась строка или None "
                    f"(сырьё пишется строками as-is, без преобразования значений)."
                )
        rows_list.append(cells)

    # Каталог партиции внутри уже провалидированного корня хранилища (не dev-репо). Сырой
    # OSError заворачиваем в RuntimeError с путём (контракт fail-loud, как патч ревью 2.1).
    try:
        partition_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Не удалось создать каталог партиции {partition_path.parent}: {exc}"
        ) from exc

    # .tmp в том же каталоге (та же ФС — иначе rename не атомарен, AC #5).
    tmp_path = partition_path.with_suffix(".parquet.tmp")
    try:
        # Транзиентный in-memory DuckDB-кодировщик: НЕ gdau.duckdb, НЕ DatabaseManager.
        conn = duckdb.connect()
        try:
            cols_ddl = ", ".join(f'"{name}" VARCHAR' for name in storage_names)
            conn.execute(f"CREATE TABLE {_TMP_TABLE} ({cols_ddl})")
            # Пустой день → пропускаем вставку, схема уже задана CREATE TABLE (AC #7).
            # (executemany c пустым списком в DuckDB бросает ошибку — поэтому именно гард.)
            if rows_list:
                placeholders = ", ".join(["?"] * width)
                conn.executemany(
                    f"INSERT INTO {_TMP_TABLE} VALUES ({placeholders})", rows_list
                )
            # write_parquet по python-пути (не COPY '<sql-литерал>') — путь не попадает в
            # SQL: ни инъекции, ни проблем с кавычками в корне хранилища (риск №1).
            # Перезаписывает осиротевший .tmp от прошлого крэша (AC #6).
            conn.table(_TMP_TABLE).write_parquet(str(tmp_path))
        finally:
            with contextlib.suppress(Exception):
                conn.close()
        # os.replace (НЕ os.rename): атомарная перезапись существующей партиции и на
        # Windows тоже (AC #3, #4). Замена одного файла не трогает другие дни (FR-6/#10).
        os.replace(str(tmp_path), str(partition_path))
    except (OSError, duckdb.Error) as exc:
        # DuckDB-сбой кодировщика (CREATE TABLE/executemany/write_parquet) бросает
        # duckdb.Error-подкласс (напр. IOException), который НЕ наследует OSError — ловим
        # его наравне с OSError от os.replace, чтобы наружу шёл обещанный RuntimeError, а не
        # сырой duckdb-трейсбек (паттерн database_manager.py).
        raise RuntimeError(
            f"Не удалось записать партицию {partition_path}: {exc}"
        ) from exc
    finally:
        # Не оставлять частичный temp: при успехе .tmp уже подменён (его нет), при фейле —
        # убираем. Перед записью .tmp путь резолвится без исключений, так что finally безопасен.
        with contextlib.suppress(OSError):
            if tmp_path.exists():
                tmp_path.unlink()

    logger.info(
        "Записана партиция %s (%d строк, источник %s)",
        partition_path,
        len(rows_list),
        source,
    )
    return len(rows_list)
