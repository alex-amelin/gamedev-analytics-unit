"""Единственная точка открытия встроенного DuckDB ``gdau.duckdb``.

Контекст-менеджер :meth:`DatabaseManager.connection` открывает рабочую базу игры в
режиме записи или только-чтения и **гарантированно закрывает** её при выходе из блока
``with`` (в т.ч. при исключении в теле). База — обычный файл на диске (``data/duckdb/
gdau.duckdb`` под корнем хранилища), встроенный движок: **ноль серверных процессов**
(FR-8), переносится между Windows и Linux копированием файла.

Потребители (реализуются в своих историях): ``load_state`` (2.4), ``views`` (2.6),
оркестратор p81 (2.7, write-conn под ``.writer.lock``), MCP-чтение (3.1, **read-only**),
init (4.3, создаёт БД + view'ы).

**Форма directaiq сохранена для узнаваемости, но это НЕ построчный вендоринг.** Вся
инфра directaiq-``DatabaseManager`` вырезана (NFR-6, [[directaiq-reference]]): система
миграций (``check_schema_version``/``schema_migrations``), UDF-макросы Директа
(``register_udfs`` — Laplace/CPA/goal-семантика, не геймдев), таблицы Директа
(``REQUIRED_TABLES``/``TABLE_METADATA_DDL``/``table_metadata``), legacy ``get_connection``
с отсылками к ``activate.sh`` и ``config_manager``. Остаётся ровно один метод
``connection`` — путь всегда из единого резолвера :func:`scripts.utils.paths.get_db_path`,
объектов БД (таблиц/view'ов) этот модуль не знает: схему заводят 2.4/2.6/init (4.3).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

import duckdb

from scripts.utils.paths import get_db_path

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Открытие/закрытие встроенного DuckDB ``gdau.duckdb`` (один метод — :meth:`connection`)."""

    @staticmethod
    @contextlib.contextmanager
    def connection(read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        """Контекст-менеджер соединения с ``gdau.duckdb`` (AC #2, #3, #4, #6).

        Путь резолвится через :func:`scripts.utils.paths.get_db_path` (внутри —
        ``get_storage_root``, который fail-loud-ит при не заданном/несуществующем корне
        хранилища, AC #5 наследуется ДО открытия соединения).

        ``read_only=False`` (по умолчанию): открыть на запись. Встроенный DuckDB создаёт
        файл БД при отсутствии, поэтому предварительно создаётся родитель ``data/duckdb/``
        (внутри уже провалидированного корня хранилища — не dev-репо, AC #2).

        ``read_only=True``: открыть только на чтение. Если ``gdau.duckdb`` ещё не создан
        (до первой выгрузки / init) → :class:`RuntimeError` с понятным текстом ДО
        ``duckdb.connect`` — иначе движок бросил бы сырой ``duckdb.IOException`` (AC #6,
        риск #4). read-only **никогда** не создаёт файл.

        Соединение закрывается в ``finally`` при любом выходе из блока ``with`` — нет
        утечки/висящего хэндла (AC #4). На ошибку открытия отдаётся :class:`RuntimeError`
        с контекстом, а не сырой трейсбек движка.
        """
        db_path = get_db_path()

        # read-only-гейт (AC #6): понятная остановка вместо сырого IOException движка.
        if read_only and not db_path.exists():
            raise RuntimeError(
                f"БД не инициализирована: {db_path} — запусти gdau-init или gdau-logs update"
            )

        # write-режим создаёт родителя: embedded DuckDB создаёт сам файл, но не каталог.
        # Корень уже провалидирован get_storage_root (это хранилище, не dev-репо), AC #2.
        if not read_only:
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Файл на месте каталога / нет прав записи — понятное сообщение, не сырой
                # OSError (контракт: на ошибку открытия — RuntimeError с контекстом).
                raise RuntimeError(
                    f"Не удалось создать каталог БД {db_path.parent}: {exc}"
                ) from exc

        try:
            conn = duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.Error as exc:
            # Неожиданная ошибка connect (битый файл, занятый лок) → понятное сообщение,
            # не сырой трейсбек движка.
            raise RuntimeError(
                f"Не удалось открыть БД DuckDB {db_path} "
                f"(read_only={read_only}): {exc}"
            ) from exc

        try:
            yield conn
        finally:
            # AC #4: гарантированное закрытие даже при исключении в теле with. suppress на
            # close — закрытие уже-битого хэндла не должно маскировать исходную ошибку.
            with contextlib.suppress(Exception):
                conn.close()
