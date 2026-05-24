"""Offline-тесты атомарной записи Parquet-партиции дня (история 2.2).

Покрывают дисциплину сырьевого слоя, а не только happy-path: строки as-is + единственное
преобразование = переименование колонок по каталогу (AC #1), temp→rename в той же ФС
(AC #2, #5), идемпотентный перезалив ровно одной партиции без затрагивания других дней
(AC #3), перезапись существующего файла через ``os.replace`` (а не ``os.rename``, который
упал бы на Windows — AC #4), осиротевший ``.tmp`` от прошлого крэша (AC #6), легитимно
пустой день со схемой (AC #7), fail-loud на неизвестной колонке/источнике/битой ширине/
битом корне без побочных эффектов, и запрет pandas/polars/numpy/pyarrow + directaiq-инфры
(по реальным import-узлам через ``ast``, не по подстроке — docstring модуля упоминает
pandas/polars). Live-набор осознанно отсутствует: модуль в сеть не ходит, DuckDB локален
([[realapi-smoke-tests]] — opt-in live только для внешнего API).

Без сети. Корень хранилища — ``monkeypatch.setenv`` на ``tmp_path``. Каталог — мини-фикстура
``Catalog`` напрямую (инъектируемый шов ``catalog=``). Чтение записанной партиции — через
транзиентный ``duckdb.connect`` (DuckDB уже в зависимостях; pandas не нужен).
"""

from __future__ import annotations

import ast
from pathlib import Path

import duckdb
import pytest

from scripts.utils.catalog import Catalog, CatalogField
from scripts.utils.env_reader import DATA_ROOT_ENV
from scripts.utils.parquet_store import write_partition
from scripts.utils.paths import get_raw_partition_path


def _catalog() -> Catalog:
    """Мини-каталог: visits (visit_id/date_time/watch_ids) + hits (watch_id).

    Конструируем ``Catalog`` напрямую (валидация живёт в ``load_catalog``, здесь не нужна).
    Типы DuckDB указаны для полноты, но ``parquet_store`` их игнорирует — всё пишется VARCHAR.
    """
    return Catalog(
        fields=(
            CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", "Идентификатор визита"),
            CatalogField("visits", "date_time", "ym:s:dateTime", "TIMESTAMP", "Дата/время визита"),
            CatalogField("visits", "watch_ids", "ym:s:watchIDs", "HUGEINT[]", "Просмотры визита"),
            CatalogField("hits", "watch_id", "ym:pv:watchID", "HUGEINT", "Идентификатор события"),
        )
    )


# Родные имена Метрики visits в порядке TSV-заголовка (как в tests/fixtures/logs_visits_sample.tsv).
_VISITS_COLUMNS = ["ym:s:visitID", "ym:s:dateTime", "ym:s:watchIDs"]
_VISITS_ROWS = [
    ["17298374650000000001", "2026-05-20 12:34:56", "[8273645,8273646]"],
    ["17298374650000000002", "2026-05-20 13:01:02", "[8273647]"],
]


def _read(path: Path) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
    """Прочитать партицию: (имена колонок, строковые типы, строки). Через DuckDB, без pandas."""
    con = duckdb.connect()
    try:
        result = con.execute(f"SELECT * FROM read_parquet('{path.as_posix()}')")
        names = [c[0] for c in result.description]
        types = [str(c[1]) for c in result.description]
        data = result.fetchall()
        return names, types, data
    finally:
        con.close()


# --- AC #1: строки as-is + единственное преобразование = переименование по каталогу ----


def test_writes_verbatim_rows_with_renamed_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Данные ложатся строками как пришли; колонки переименованы в snake_case по каталогу (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    written = write_partition(
        "visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=_catalog()
    )
    assert written == 2

    partition_path = get_raw_partition_path("visits", "2026-05-20")
    assert partition_path.is_file()

    names, types, data = _read(partition_path)
    # Единственное преобразование — переименование колонок по каталогу (порядок сохранён).
    assert names == ["visit_id", "date_time", "watch_ids"]
    # Сырьё строками: все колонки VARCHAR, без CAST/типизации (это view 2.6).
    assert types == ["VARCHAR", "VARCHAR", "VARCHAR"]
    # Значения дословно: массив watch_ids лежит строкой, не распарсен в LIST.
    assert data == [
        ("17298374650000000001", "2026-05-20 12:34:56", "[8273645,8273646]"),
        ("17298374650000000002", "2026-05-20 13:01:02", "[8273647]"),
    ]


def test_preserves_none_cells_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустая ячейка (None) сохраняется как NULL, без подмены/усечения (AC #1)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    rows = [["17298374650000000001", "2026-05-20 12:34:56", None]]
    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, rows, catalog=_catalog())

    _, _, data = _read(get_raw_partition_path("visits", "2026-05-20"))
    assert data == [("17298374650000000001", "2026-05-20 12:34:56", None)]


# --- AC #2/#5: temp→rename в той же ФС; tmp после записи не остаётся --------------------


def test_temp_replaced_and_in_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После записи финальная партиция есть, а ``*.parquet.tmp`` подменён/убран (AC #2, #5)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=_catalog())

    partition_path = get_raw_partition_path("visits", "2026-05-20")
    tmp_path_partition = partition_path.with_suffix(".parquet.tmp")
    assert partition_path.is_file()
    assert not tmp_path_partition.exists()
    # Та же ФС: tmp лежит в каталоге финальной партиции (без cross-FS rename).
    assert tmp_path_partition.parent == partition_path.parent


# --- AC #3: идемпотентный перезалив ровно одной партиции; другие дни нетронуты ----------


def test_reload_rewrites_one_partition_others_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Перезалив дня = одна партиция; соседний день не тронут; повтор идемпотентен (AC #3)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    catalog = _catalog()

    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=catalog)
    other_rows = [["17298374650000000003", "2026-05-21 09:00:00", "[8273648]"]]
    write_partition("visits", "2026-05-21", _VISITS_COLUMNS, other_rows, catalog=catalog)

    path_20 = get_raw_partition_path("visits", "2026-05-20")
    path_21 = get_raw_partition_path("visits", "2026-05-21")
    before_20 = _read(path_20)
    before_21 = _read(path_21)

    # Перезалив 2026-05-20 тем же входом.
    written = write_partition(
        "visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=catalog
    )
    assert written == 2

    # Соседний день не изменился (содержимое идентично — НЕ проверяем байты: риск №6).
    assert _read(path_21) == before_21
    # Перезалитый день идемпотентен ПО СОДЕРЖИМОМУ ЦЕЛИКОМ (имена + типы + все строки/ячейки),
    # а не только по первой ячейке; байт-равенство НЕ проверяем (DuckDB вшивает метаданные
    # писателя — риск №6).
    assert _read(path_20) == before_20
    names_20, _, data_20 = _read(path_20)
    assert names_20 == ["visit_id", "date_time", "watch_ids"]
    assert data_20 == [
        ("17298374650000000001", "2026-05-20 12:34:56", "[8273645,8273646]"),
        ("17298374650000000002", "2026-05-20 13:01:02", "[8273647]"),
    ]

    # Ровно две партиции в каталоге источника, без осиротевших .tmp.
    source_dir = path_20.parent
    assert sorted(p.name for p in source_dir.glob("*.parquet")) == [
        "2026-05-20.parquet",
        "2026-05-21.parquet",
    ]
    assert list(source_dir.glob("*.tmp")) == []


# --- AC #4: перезапись существующего файла (os.replace, не os.rename) -------------------


def test_overwrite_existing_partition_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторная запись поверх существующей партиции не падает (доказывает os.replace, AC #4).

    На Windows ``os.rename`` поверх существующего файла бросил бы FileExistsError/
    PermissionError — успешный второй вызов фиксирует, что используется ``os.replace``.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    catalog = _catalog()

    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=catalog)
    # Файл уже существует — второй вызов не должен бросить исключение.
    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=catalog)

    assert get_raw_partition_path("visits", "2026-05-20").is_file()


# --- AC #6: осиротевший .tmp от прошлого крэша не мешает --------------------------------


def test_stale_tmp_is_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Осиротевший ``*.parquet.tmp`` с мусором перезаписывается; после записи его нет (AC #6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    partition_path = get_raw_partition_path("visits", "2026-05-20")
    stale_tmp = partition_path.with_suffix(".parquet.tmp")
    stale_tmp.parent.mkdir(parents=True, exist_ok=True)
    stale_tmp.write_text("частичный мусор от прошлого крэша", encoding="utf-8")

    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=_catalog())

    assert partition_path.is_file()
    assert not stale_tmp.exists()
    _, _, data = _read(partition_path)
    assert len(data) == 2


# --- AC #7: легитимно пустой день → пустая партиция со схемой, день валиден -------------


def test_empty_day_writes_schema_only_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 строк → файл-партиция со схемой (count==0, колонки на месте), возврат 0 (AC #7)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    written = write_partition("visits", "2026-05-20", _VISITS_COLUMNS, [], catalog=_catalog())
    assert written == 0

    partition_path = get_raw_partition_path("visits", "2026-05-20")
    assert partition_path.is_file()

    names, _, data = _read(partition_path)
    assert names == ["visit_id", "date_time", "watch_ids"]  # схема есть
    assert data == []  # данных нет — но это валидный загруженный день, не ошибка


# --- Негативные / гарды (fail-loud) ----------------------------------------------------


def test_unknown_column_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Колонка без записи в каталоге → ValueError (поле без записи = дефект)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="ym:s:unknownField"):
        write_partition(
            "visits",
            "2026-05-20",
            ["ym:s:visitID", "ym:s:unknownField"],
            [["a", "b"]],
            catalog=_catalog(),
        )


def test_invalid_source_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Невалидный источник → ValueError (через get_raw_partition_path)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="source"):
        write_partition("sessions", "2026-05-20", _VISITS_COLUMNS, [], catalog=_catalog())


def test_row_width_mismatch_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Строка неверной ширины → ValueError (без молчаливого паддинга/усечения, FR-6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    bad_rows = [["only-one-cell"]]  # 1 ячейка против 3 колонок
    with pytest.raises(ValueError, match="ширин"):
        write_partition("visits", "2026-05-20", _VISITS_COLUMNS, bad_rows, catalog=_catalog())


def test_non_string_cell_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не-строковая ячейка (list/int) → ValueError (сырьё строками as-is, без тихой коэрции).

    Без гарда DuckDB молча привёл бы list к VARCHAR через repr (``[1, 2, 3]`` с пробелами ≠
    исходный TSV ``[1,2,3]``) — тихое искажение сырья. Контракт ``Sequence[str | None]``
    подкрепляется рантайм-проверкой (raw-integrity, NFR-1).
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    # Третья ячейка — распарсенный список вместо TSV-строки (нарушение контракта вызывающим).
    bad_rows = [["17298374650000000001", "2026-05-20 12:34:56", [8273645, 8273646]]]
    with pytest.raises(ValueError, match="ожидалась строка или None"):
        write_partition("visits", "2026-05-20", _VISITS_COLUMNS, bad_rows, catalog=_catalog())


def test_empty_columns_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой список колонок → ValueError (нет схемы / нечего писать)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError):
        write_partition("visits", "2026-05-20", [], [], catalog=_catalog())


def test_missing_root_fails_loud_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Битый корень (GDAU_DATA_ROOT не задан) → ValueError, и ни одного файла/каталога не создано."""
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)

    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=_catalog())

    # Никаких побочных эффектов — fail до построения пути/mkdir (паттерн test_paths.py).
    assert not (tmp_path / "data").exists()


# --- Сбойные пути записи: duckdb.Error → RuntimeError; зачистка .tmp на фейле -----------


def test_duckdb_write_failure_wrapped_as_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой DuckDB-кодировщика (``duckdb.Error``, НЕ ``OSError``) заворачивается в RuntimeError.

    ``duckdb.IOException`` наследует ``Exception``, а не ``OSError`` — без ловли ``duckdb.Error``
    он улетел бы сырым мимо контракта fail-loud. Регрессия патча ``except (OSError, duckdb.Error)``.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    class _BoomRelation:
        def write_parquet(self, *args: object, **kwargs: object) -> None:
            raise duckdb.IOException("нет места на диске (смоделировано)")

    class _BoomConn:
        def execute(self, *args: object, **kwargs: object) -> "_BoomConn":
            return self

        def executemany(self, *args: object, **kwargs: object) -> "_BoomConn":
            return self

        def table(self, *args: object, **kwargs: object) -> _BoomRelation:
            return _BoomRelation()

        def close(self) -> None:
            pass

    # parquet_store вызывает duckdb.connect() — подменяем на «взрывной» коннект.
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: _BoomConn())

    with pytest.raises(RuntimeError, match="Не удалось записать партицию"):
        write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=_catalog())


def test_failure_after_tmp_created_cleans_up_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой ``os.replace`` ПОСЛЕ создания ``.tmp`` → RuntimeError, а ``.tmp`` убран (AC #6 / риск №4).

    Закрывает вторую половину AC #6: на фейле записи частичный temp не остаётся на диске.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    def _boom_replace(*args: object, **kwargs: object) -> None:
        raise OSError("replace упал (смоделировано)")

    monkeypatch.setattr("scripts.utils.parquet_store.os.replace", _boom_replace)

    partition_path = get_raw_partition_path("visits", "2026-05-20")
    tmp_partition = partition_path.with_suffix(".parquet.tmp")

    with pytest.raises(RuntimeError, match="Не удалось записать партицию"):
        write_partition("visits", "2026-05-20", _VISITS_COLUMNS, _VISITS_ROWS, catalog=_catalog())

    # .tmp реально создан write_parquet, затем replace упал → finally его убрал.
    assert not tmp_partition.exists()
    # Финальной партиции нет (replace не выполнился) — на диске не остаётся «полу-дня».
    assert not partition_path.exists()


# --- Анти-зависимость: Parquet пишет DuckDB, а не запрещённый стек ----------------------


def test_no_heavy_or_directaiq_infra_imported() -> None:
    """Нет import pandas/polars/numpy/pyarrow и directaiq-инфры (ключевой тест риска №1).

    Не по подстроке (docstring модуля упоминает pandas/polars) — парсим AST и смотрим
    реальные import-узлы по корню имени. Именно этот тест фиксирует, что Parquet пишется
    встроенным DuckDB, а не запрещённым аналитическим стеком.
    """
    import scripts.utils.parquet_store as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    forbidden = {"pandas", "polars", "numpy", "pyarrow", "config_manager", "base_script"}
    offenders = {n for n in imported if n.split(".")[0] in forbidden}
    assert not offenders, f"запрещённые импорты в parquet_store: {offenders}"
