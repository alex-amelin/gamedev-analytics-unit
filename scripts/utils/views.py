"""Рабочий слой — типизированные view'ы ``visits``/``hits`` поверх Parquet-партиций.

Сырьё хранится **строками as-is** (Parquet, без типов — забота ``parquet_store`` 2.2),
а агент анализирует типы. Этот модуль строит **читаемую половину** пути записи: по
каталогу-SSOT (1.5) генерирует DDL DuckDB-view'ов, где каждое поле типизировано через
``TRY_CAST`` по типу каталога. Битая ячейка → ``NULL`` (view НЕ падает, FR-7); ID-поля
→ ``HUGEINT`` (значение > 2^63 не переполняется, NFR-4); массивы → ``LIST`` (нативный
``TRY_CAST(col AS T[])`` — вариант A, утверждён Шефом 2026-05-24: исход «массив → LIST,
битая → NULL», как у скаляров; пустой ``[]`` → ``[]``). Дрейф схемы между партициями
снимается ``read_parquet(..., union_by_name => true)`` — отсутствующая в части партиций
колонка → ``NULL`` (AC #7). ``CREATE OR REPLACE VIEW`` делает разворачивание идемпотентным
(AC #4). View **ленив** → отражает текущий Parquet БЕЗ материализации: перезалив партиции
(2.2/2.8) виден следующим запросом сразу, пере-создавать view не нужно (OQ#3 — рабочий
слой = view'ы, не материализованные таблицы). **Исключение — переход 0→N партиций:** до
первой партиции источника view создаётся пустышкой (``WHERE false``, ветка
``has_partitions=False`` ниже), поэтому писатель (p81 2.7) ОБЯЗАН пере-вызвать
:func:`create_views` ПОСЛЕ записи первой партиции — иначе свежезагруженный источник
останется пуст (баг порядка create_views ДО load_day).

**Транзиентная граница (НЕ в скоупе AC #7):** если поле добавлено в каталог, но НИ ОДНОЙ
партиции с ним ещё нет, ``TRY_CAST("new_col" …)`` упадёт «column not found» при запросе.
Окно ничтожно: p81 (2.7) выгружает полный список полей каталога на каждый день → каждая
свежая партиция несёт все текущие колонки; окно живёт лишь между расширением каталога и
следующим обновлением и закрывается им. Усложнять ``COALESCE``'ом не нужно ([[simplicity-first]]).

**`TRY_CAST` даёт «битая → NULL», но НЕ per-cell лог:** view — декларативный SELECT, не
построчный обработчик; per-cell логирование в SQL-view невозможно. Контракт «битая → NULL,
день не падает» соблюдён; «+ лог» из FR-7 на уровне view сводится к опциональному
агрегатному диагностическому запросу (не реализуется здесь).

**Границы (что делает ДРУГОЙ компонент, не этот):** типы/имена полей — ``catalog`` (1.5,
SSOT); путь партиций — ``paths.get_raw_source_dir`` (2.1); открытие/закрытие ``gdau.duckdb``
— :class:`DatabaseManager` (2.1): сюда инъектируется готовый ``conn``, модуль БД сам **не**
открывает; захват ``.writer.lock`` вокруг записи DDL — вызывающий (init 4.3 / p81 2.7:
``CREATE OR REPLACE VIEW`` пишет в каталог ``gdau.duckdb`` → write-conn под локом); запись
Parquet — 2.2; учёт дней — 2.4; чтение/анализ и конкуренция читатель↔писатель (Windows
``os.replace`` поверх открытой читателем партиции, defer 2.2) — рантайм-забота MCP-чтения
(3.1, read-only, лок НЕ берёт). Модуль в сеть не ходит, ``gdau.duckdb`` **не** открывает,
лок **не** берёт, Parquet **не** пишет, ``mkdir`` **не** делает и **не** импортирует
``database_manager``/``parquet_store``/``load_state``/``writer_lock`` (риск №6).
"""

from __future__ import annotations

import glob
import logging
from collections.abc import Iterable

import duckdb

from scripts.utils.catalog import VALID_SOURCES, Catalog, load_catalog
from scripts.utils.paths import get_raw_source_dir

logger = logging.getLogger(__name__)

__all__ = ["build_view_ddl", "create_views"]


def build_view_ddl(
    source: str,
    catalog: Catalog,
    *,
    partition_glob: str,
    has_partitions: bool,
) -> str:
    """Сгенерировать ``CREATE OR REPLACE VIEW``-DDL источника из каталога — **чистая** функция.

    Главный тестируемый шов: возвращает текст DDL без живой БД (как чистые функции
    ``paths``/``catalog``). Типизированная проекция — по :meth:`Catalog.duckdb_types`
    (``storage_name → duckdb_type`` в порядке каталога). Идентификаторы квотируются
    двойными кавычками (AC #5; ``"date"`` — зарезервированно-похожее имя).

    :param source: ``visits``/``hits`` (валидируется через :meth:`Catalog.duckdb_types`).
    :param catalog: каталог-SSOT — источник имён/типов колонок.
    :param partition_glob: glob партиций (``…/*.parquet``, ``as_posix``) для непустого
        источника; одинарные кавычки экранируются удвоением (риск №2). При
        ``has_partitions=False`` не используется.
    :param has_partitions: есть ли хотя бы один ``.parquet`` у источника. ``True`` →
        ``FROM read_parquet('{glob}', union_by_name => true)`` (риск №4, AC #7); ``False``
        → проекция ``CAST(NULL AS {type})`` + ``WHERE false`` (0 строк, корректные типы —
        пустой типизированный view, AC #6, риск №3).
    :raises ValueError: невалидный ``source`` или источник без полей в каталоге (fail-loud).
    """
    types = catalog.duckdb_types(source)  # валидирует source (fail-loud); порядок каталога
    if not types:
        raise ValueError(
            f"В каталоге нет полей для источника {source!r} — невозможно построить view "
            f"(вырожденный SSOT = дефект)."
        )

    if has_partitions:
        # Непустой источник: типизация через TRY_CAST (битая ячейка → NULL, FR-7). Массив и
        # скаляр — ОДНО выражение (вариант A): duckdb_type уже несёт `T[]` для массивов.
        projections = ",\n".join(
            f'  TRY_CAST("{name}" AS {duckdb_type}) AS "{name}"'
            for name, duckdb_type in types.items()
        )
        # Путь уходит в DDL строковым ЛИТЕРАЛОМ (биндинга в DDL нет): одинарную кавычку
        # экранируем удвоением — путь не «вырывается» из строки SQL (риск №2).
        escaped_glob = partition_glob.replace("'", "''")
        return (
            f'CREATE OR REPLACE VIEW "{source}" AS\n'
            f"SELECT\n{projections}\n"
            f"FROM read_parquet('{escaped_glob}', union_by_name => true)"
        )

    # Пустой источник (нет партиций): read_parquet по каталогу без файлов бросил бы «No
    # files found» при запросе у потребителя (3.1/3.3). Вместо этого — типизированная
    # проекция NULL'ов без FROM + WHERE false: 0 строк, типы колонок корректны (AC #6).
    null_projections = ",\n".join(
        f'  CAST(NULL AS {duckdb_type}) AS "{name}"'
        for name, duckdb_type in types.items()
    )
    return (
        f'CREATE OR REPLACE VIEW "{source}" AS\n'
        f"SELECT\n{null_projections}\n"
        f"WHERE false"
    )


def create_views(
    conn: duckdb.DuckDBPyConnection,
    *,
    catalog: Catalog | None = None,
    sources: Iterable[str] = VALID_SOURCES,
) -> None:
    """Создать/пере-определить типизированные view'ы источников на переданном ``conn`` (AC #1).

    Тонкий исполнитель над чистым :func:`build_view_ddl`. По каждому источнику: резолвит
    каталог партиций (:func:`scripts.utils.paths.get_raw_source_dir` — валидирует ``source``
    и наследует fail-loud битого корня хранилища ДО построения DDL), определяет наличие
    партиций (``.is_dir()`` + только **файлы** ``glob('*.parquet')`` — приём
    ``load_state._partition_dates`` 2.4; осиротевший ``.parquet.tmp`` по суффиксу и каталог
    с именем ``{date}.parquet`` по ``is_file`` не матчатся, риск №3/№5), собирает DDL и
    исполняет его. **Все DDL собираются ДО первого ``conn.execute``** — набор view'ов
    создаётся атомарно (падение на одном источнике не оставляет частично определённый слой).

    ``conn`` инъектируется (модуль БД сам **не** открывает — как ``load_state`` 2.4).
    ``catalog=None`` → :func:`load_catalog` (прод-путь, шов как ``parquet_store``).
    **НЕ** открывает ``gdau.duckdb``, **НЕ** берёт ``.writer.lock`` (забота вызывающего —
    init 4.3 / p81 2.7), **НЕ** делает ``mkdir``.

    :raises ValueError: невалидный ``source``, источник без полей каталога, битый корень
        хранилища (наследуется из ``paths``).
    """
    effective_catalog = catalog if catalog is not None else load_catalog()

    # Сначала собираем ВСЕ DDL (валидация source/каталога/корня fail-loud ДО любого execute),
    # затем исполняем — набор view'ов создаётся атомарно: падение на одном источнике не
    # оставляет частично определённый слой (CREATE OR REPLACE остаётся идемпотентным).
    planned: list[tuple[str, str, int]] = []
    for source in sources:
        # get_raw_source_dir валидирует source И резолвит корень хранилища fail-loud ДО DDL
        # (битая конфигурация падает без побочных эффектов; mkdir не делаем — резолвер чистый).
        source_dir = get_raw_source_dir(source)
        # Только ФАЙЛЫ '*.parquet' (как load_state._partition_dates): осиротевший
        # '{date}.parquet.tmp' (по суффиксу) и каталог с именем '{date}.parquet' (по is_file)
        # партициями НЕ считаются — иначе read_parquet упал бы при запросе (риск №3/№5).
        parquet_files = (
            sorted(p for p in source_dir.glob("*.parquet") if p.is_file())
            if source_dir.is_dir()
            else []
        )
        has_partitions = bool(parquet_files)

        # Путь в DDL — as_posix() (прямые слеши кросс-платформенно, риск №2) + glob.escape:
        # экранируем каталог-литерал, чтобы glob-метасимволы корня ('…/game[1]/…') НЕ
        # трактовались read_parquet как класс символов; шаблон '*.parquet' добавляем ПОСЛЕ.
        escaped_dir = glob.escape(source_dir.as_posix())
        partition_glob = f"{escaped_dir}/*.parquet"
        ddl = build_view_ddl(
            source,
            effective_catalog,
            partition_glob=partition_glob,
            has_partitions=has_partitions,
        )
        planned.append((source, ddl, len(parquet_files)))

    for source, ddl, partition_count in planned:
        conn.execute(ddl)
        logger.info(
            "Создан view %s (%s, партиций: %d)",
            source,
            "непустой" if partition_count else "пустой типизированный",
            partition_count,
        )
