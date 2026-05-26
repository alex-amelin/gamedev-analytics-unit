"""Offline-тесты ядра MCP-инструмента ``duckdb_query`` (история 3.1).

Покрывают весь контракт тонкого канала чтения, не только happy-path: исполнение SQL и
форматы json/markdown/csv (AC #2/#4), битый SQL → строка-ошибка без падения (AC #6),
**двух-слойная** read-only-дисциплина — guard режет COPY TO/PRAGMA/ATTACH/SET/INSTALL/
мульти-стейтмент/comment-bypass, которые сам ``read_only`` пропускает (AC #7), запрос до
первой выгрузки → понятная подсказка про ``gdau-logs update``, не сырой IOException (AC #8),
однократный retry на транзиентной IOException чтения партиции (AC #9), валидация аргументов
и кламп лимита ``[1, MAX_LIMIT]`` (AC #10), watchdog-таймаут «убегающего» запроса (AC #11).

Без сети, без внешнего API. DuckDB локален: фикстура пишет ``gdau.duckdb`` write-соединением
(2.1) под временным ``GDAU_DATA_ROOT``, MCP-ядро читает его read-only. Кросс-платформенно
(``tmp_path``/``pathlib``). Live-набор осознанно отсутствует: MCP-чтение в Logs API не ходит.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest

from scripts.mcp.tools import core
from scripts.utils.catalog import Catalog, CatalogField
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.env_reader import DATA_ROOT_ENV
from scripts.utils.load_state import ensure_load_state_table, mark_loaded
from scripts.utils.parquet_store import write_partition
from scripts.utils.paths import get_results_dir
from scripts.utils.views import create_views


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Временное хранилище с ``gdau.duckdb`` и таблицей ``visits`` (3 строки), MCP читает read-only.

    Достаточно простой таблицы (контракт 3.1 — исполнение произвольного SQL; имя ``visits``
    совпадает с прод-view'ом, чтобы запросы тестов были реалистичны).
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    with DatabaseManager.connection() as conn:
        conn.execute("CREATE TABLE visits(visit_id BIGINT, client_id BIGINT, page_views INTEGER)")
        conn.execute("INSERT INTO visits VALUES (1, 100, 5), (2, 200, 3), (3, 300, 8)")
    return tmp_path


# --- AC #2/#4: SELECT отдаёт строки; формат уважается -----------------------------------


def test_select_json_returns_rows(db: Path) -> None:
    """SELECT → JSON с колонками/строками/метаданными усечения (AC #2/#4)."""
    out = core.handle_query("SELECT visit_id, page_views FROM visits ORDER BY visit_id", "json", 10)
    parsed = json.loads(out)
    assert parsed["columns"] == ["visit_id", "page_views"]
    assert parsed["total_rows"] == 3
    assert parsed["has_more"] is False
    assert parsed["rows"][0] == {"visit_id": 1, "page_views": 5}


def test_select_markdown_format(db: Path) -> None:
    """format=markdown → markdown-таблица с разделителем (AC #4)."""
    out = core.handle_query("SELECT visit_id FROM visits ORDER BY visit_id", "markdown", 10)
    assert "| visit_id |" in out
    assert "| --- |" in out
    assert "| 1 |" in out


def test_select_csv_format(db: Path) -> None:
    """format=csv → строки CSV с заголовком (AC #4)."""
    out = core.handle_query("SELECT visit_id FROM visits ORDER BY visit_id", "csv", 10)
    lines = out.splitlines()
    assert lines[0] == "visit_id"
    assert lines[1] == "1"


def test_no_result_statement(db: Path) -> None:
    """Запрос без результирующего набора (DESCRIBE отдаёт набор; берём EXPLAIN-форму через SELECT)."""
    # SELECT, не возвращающий строк, всё равно даёт описание колонок → форматтер, не «без результата».
    out = core.handle_query("SELECT visit_id FROM visits WHERE false", "json", 10)
    parsed = json.loads(out)
    assert parsed["total_rows"] == 0
    assert parsed["rows"] == []


# --- AC #6: битый SQL → строка-ошибка, исключение не вылетает ----------------------------


def test_broken_sql_returns_error_string(db: Path) -> None:
    """Синтаксически битый SQL → строка с `**SQL Error` (AC #6), исключение не выходит наружу.

    Запрос начинается с валидного read-слова SELECT (проходит guard) и падает уже в парсере
    движка — иначе опечатка в ведущем слове отсеклась бы guard'ом раньше БД (см. AC #7).
    """
    out = core.handle_query("SELECT * FORM visits", "json", 10)
    assert "**SQL Error" in out
    assert isinstance(out, str)


def test_unknown_column_error_suggests_views(db: Path) -> None:
    """Несуществующая колонка → классифицированная ошибка с подсказкой про view'ы (AC #6)."""
    out = core.handle_query("SELECT no_such_col FROM visits", "json", 10)
    assert "**SQL Error" in out
    assert "visits" in out  # подсказка указывает на доступные view'ы, не на --tables/--schema (3.2)


# --- AC #7: двух-слойный read-only — guard режет запись, которую read_only пропускает -----


def test_guard_rejects_write_keywords(db: Path) -> None:
    """INSERT/UPDATE/DELETE/CREATE/DROP/ALTER → отказ guard'ом (AC #7)."""
    for sql in (
        "INSERT INTO visits VALUES (9, 9, 9)",
        "UPDATE visits SET page_views = 0",
        "DELETE FROM visits",
        "CREATE TABLE z(x INT)",
        "DROP TABLE visits",
        "ALTER TABLE visits ADD COLUMN y INT",
    ):
        out = core.handle_query(sql)
        assert "только для чтения" in out, sql


def test_guard_rejects_copy_to_and_no_file_written(db: Path, tmp_path: Path) -> None:
    """COPY … TO режется guard'ом (read_only сам его ПРОПУСКАЕТ) и файл НЕ создаётся (риск №1/AC #7)."""
    target = (tmp_path / "leak.csv").as_posix()
    out = core.handle_query(f"COPY (SELECT * FROM visits) TO '{target}' (HEADER)")
    assert "только для чтения" in out
    assert not os.path.exists(target)  # запись не дошла до движка — файла нет


def test_guard_rejects_pragma_set_attach_install_load(db: Path) -> None:
    """PRAGMA/SET/ATTACH/INSTALL/LOAD → отказ (read_only пропускает PRAGMA — нужен guard, AC #7)."""
    for sql in (
        "PRAGMA disable_optimizer",
        "SET threads = 1",
        "ATTACH 'other.duckdb' AS other",
        "INSTALL httpfs",
        "LOAD httpfs",
    ):
        out = core.handle_query(sql)
        assert "только для чтения" in out, sql


def test_guard_rejects_multi_statement(db: Path, tmp_path: Path) -> None:
    """Мульти-стейтмент `SELECT 1; COPY … TO …` → отказ (иначе второй стейтмент пишет файл, AC #7)."""
    target = (tmp_path / "multi.csv").as_posix()
    out = core.handle_query(f"SELECT 1; COPY (SELECT * FROM visits) TO '{target}' (HEADER)")
    assert "только для чтения" in out
    assert not os.path.exists(target)


def test_guard_rejects_comment_bypass(db: Path, tmp_path: Path) -> None:
    """Comment-bypass `/* x */ COPY …` и `-- c\\nCOPY …` → отказ (единственный путь обхода, AC #7)."""
    block = (tmp_path / "block.csv").as_posix()
    line = (tmp_path / "line.csv").as_posix()
    out_block = core.handle_query(f"/* safe */ COPY (SELECT * FROM visits) TO '{block}' (HEADER)")
    out_line = core.handle_query(f"-- safe\nCOPY (SELECT * FROM visits) TO '{line}' (HEADER)")
    assert "только для чтения" in out_block
    assert "только для чтения" in out_line
    assert not os.path.exists(block)
    assert not os.path.exists(line)


def test_guard_rejects_explain_analyze_copy_and_no_file_written(
    db: Path, tmp_path: Path
) -> None:
    """`EXPLAIN ANALYZE COPY … TO` → отказ guard'ом, файл НЕ создаётся (обход read-only, AC #7).

    `EXPLAIN ANALYZE` ИСПОЛНЯЕТ вложенный запрос (проверено на DuckDB 1.5.3: пишет файл даже под
    read_only). Ведущее слово EXPLAIN в allowlist пропускало бы его → guard валидирует вложенный
    стейтмент после среза `EXPLAIN [ANALYZE]`.
    """
    target = (tmp_path / "analyze_leak.csv").as_posix()
    out = core.handle_query(f"EXPLAIN ANALYZE COPY (SELECT * FROM visits) TO '{target}' (HEADER)")
    assert "только для чтения" in out
    assert "COPY" in out  # вложенная операция названа в отказе
    assert not os.path.exists(target)  # запись не дошла до движка — файла нет


def test_guard_allows_read_operations() -> None:
    """Read-операции (SELECT/WITH/FROM/DESCRIBE/EXPLAIN [ANALYZE] read/(SELECT)/`;`/коммент) → None."""
    for sql in (
        "SELECT 1",
        "   select 1",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "FROM visits SELECT *",
        "DESCRIBE visits",
        "(SELECT 1)",
        "SELECT 1;",  # хвостовой ';' срезается → один стейтмент
        "-- комментарий\nSELECT 1",
        "/* комментарий */ SELECT 1",
        "EXPLAIN SELECT 1",  # EXPLAIN над read-запросом → срезается, вложенный SELECT проходит
        "EXPLAIN ANALYZE SELECT 1",  # ANALYZE над read-запросом допустим (вложенный SELECT)
    ):
        assert core._reject_if_not_readonly(sql) is None, sql


# --- AC #8: запрос до первой выгрузки → понятная подсказка, не сырой IOException ----------


def test_query_before_data_gives_friendly_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Хранилище без gdau.duckdb → сообщение про `gdau-logs update`, не сырой IOException (AC #8)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))  # каталог есть, БД ещё нет
    out = core.handle_query("SELECT 1")
    assert "gdau-logs update" in out
    assert "IOException" not in out


# --- AC #9: однократный retry на транзиентной IOException чтения партиции -----------------


def test_retry_once_on_transient_io_then_success(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Первая попытка → IOException, вторая → успех: ровно один повтор, результат отдан (AC #9)."""
    calls = {"n": 0}
    real = core._execute_with_timeout

    def flaky(conn: duckdb.DuckDBPyConnection, query: str, timeout_s: float) -> list[object]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise duckdb.IOException("transient os.replace race")
        return real(conn, query, timeout_s)

    monkeypatch.setattr(core, "_execute_with_timeout", flaky)
    out = core.handle_query("SELECT visit_id FROM visits ORDER BY visit_id", "json", 10)
    assert calls["n"] == 2  # initial + ровно один повтор
    assert json.loads(out)["total_rows"] == 3


def test_retry_gives_up_after_second_io_failure(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IOException дважды → классифицированная ошибка, повтор НЕ бесконечный (AC #9)."""
    calls = {"n": 0}

    def always_io(conn: duckdb.DuckDBPyConnection, query: str, timeout_s: float) -> list[object]:
        calls["n"] += 1
        raise duckdb.IOException("persistent read failure")

    monkeypatch.setattr(core, "_execute_with_timeout", always_io)
    out = core.handle_query("SELECT visit_id FROM visits", "json", 10)
    assert calls["n"] == 2  # один повтор, не больше
    assert "**SQL Error" in out  # IOException — подкласс duckdb.Error → классификатор


def test_syntax_error_not_retried(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Синтаксическая ошибка (не IOException) НЕ ретраится — это AC #6, не AC #9."""
    calls = {"n": 0}
    real = core._execute_with_timeout

    def counting(conn: duckdb.DuckDBPyConnection, query: str, timeout_s: float) -> list[object]:
        calls["n"] += 1
        return real(conn, query, timeout_s)

    monkeypatch.setattr(core, "_execute_with_timeout", counting)
    out = core.handle_query("SELECT * FORM visits", "json", 10)
    assert calls["n"] == 1  # ни одного повтора на синтаксис
    assert "**SQL Error" in out


# --- AC #10: валидация аргументов и кламп лимита -----------------------------------------


def test_empty_query_gives_hint() -> None:
    """Пустой/из пробелов/None query → подсказка, а не сырой запрос в БД (AC #10)."""
    for q in ("", "   ", "\n\t", None):
        out = core.handle_query(q)  # type: ignore[arg-type]  # None — рантайм-кейс невалидного ввода
        assert "Пустой запрос" in out


def test_clamp_limit_bounds() -> None:
    """limit ≤0 → DEFAULT_LIMIT; > MAX_LIMIT → MAX_LIMIT; внутри — как есть (AC #10)."""
    assert core._clamp_limit(0) == core.DEFAULT_LIMIT
    assert core._clamp_limit(-5) == core.DEFAULT_LIMIT
    assert core._clamp_limit(10**9) == core.MAX_LIMIT
    assert core._clamp_limit(50) == 50
    assert core._clamp_limit(core.MAX_LIMIT) == core.MAX_LIMIT


def test_unknown_format_defaults_to_json(db: Path) -> None:
    """Неизвестный format в ядре → дефолт json (AC #4/AC #10; Literal-тип инструмента отсекает раньше)."""
    out = core.execute_query("SELECT visit_id FROM visits ORDER BY visit_id", "xml", 10)
    parsed = json.loads(out)  # вернулся валидный JSON, а не markdown/ошибка
    assert parsed["total_rows"] == 3


def test_limit_truncates_and_flags_has_more(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Результат больше лимита → усечён до DEFAULT_LIMIT, has_more=True, next_offset выставлен (AC #10)."""
    # Понизим DEFAULT_LIMIT, чтобы 3 строки превысили лимит без вставки сотен строк.
    monkeypatch.setattr(core, "DEFAULT_LIMIT", 2)
    out = core.execute_query("SELECT visit_id FROM visits ORDER BY visit_id", "json", 0)  # 0 → DEFAULT
    parsed = json.loads(out)
    assert parsed["total_rows"] == 3
    assert len(parsed["rows"]) == 2  # усечено лимитом
    assert parsed["has_more"] is True
    assert parsed["next_offset"] == 2


# --- AC #11: watchdog-таймаут «убегающего» запроса ---------------------------------------


def test_runaway_query_interrupted_by_timeout(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Малый STATEMENT_TIMEOUT_S → cross join прерывается → сообщение про лимит времени (AC #11)."""
    monkeypatch.setattr(core, "STATEMENT_TIMEOUT_S", 0.5)
    out = core.execute_query(
        "SELECT count(*) FROM range(1000000000) a, range(1000000000) b", "json", 10
    )
    assert "превысил лимит времени" in out


def test_fast_query_not_interrupted(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Быстрый запрос при малом таймауте НЕ прерывается (таймер отменяется, нет ложного срабатывания)."""
    monkeypatch.setattr(core, "STATEMENT_TIMEOUT_S", 0.5)
    out = core.execute_query("SELECT visit_id FROM visits ORDER BY visit_id", "json", 10)
    assert json.loads(out)["total_rows"] == 3


# --- Read-only-соединение: ядро зовёт connection(read_only=True) -------------------------


def test_execute_opens_read_only_connection(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_query открывает соединение именно read-only (инвариант канала, AC #3)."""
    captured: dict[str, bool] = {}
    real = DatabaseManager.connection

    @contextmanager
    def spy(read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        captured["read_only"] = read_only
        with real(read_only=read_only) as conn:
            yield conn

    monkeypatch.setattr(core.DatabaseManager, "connection", staticmethod(spy))
    core.execute_query("SELECT 1", "json", 10)
    assert captured["read_only"] is True


# --- Экранирование в форматтерах (заголовки и спецсимволы) -------------------------------


def test_csv_header_is_rfc4180_quoted() -> None:
    """Имя колонки с запятой/кавычкой квотируется в заголовке так же, как значения."""
    out = core.format_result_csv(['a,b', 'c"d'], [("1", "2")], 10)
    header = out.splitlines()[0]
    # запятая в имени → всё имя в кавычках (иначе шапка разъедется относительно строк данных);
    # кавычка → удвоение по RFC4180.
    assert header == '"a,b","c""d"'


def test_markdown_header_escapes_pipe() -> None:
    """Имя колонки с `|` экранируется в заголовке (иначе лишний разделитель ломает таблицу)."""
    out = core.format_result_markdown(["a|b"], [("1",)], 10)
    assert "| a\\|b |" in out


def test_markdown_cell_newline_does_not_break_row() -> None:
    """Значение со встроенным `\\n` не разрывает однострочный ряд markdown-таблицы."""
    out = core.format_result_markdown(["v"], [("line1\nline2",)], 10)
    data_rows = [ln for ln in out.splitlines() if ln.startswith("| line")]
    assert data_rows == ["| line1 line2 |"]  # перевод строки → пробел, ряд остался одной строкой


# ========================================================================================
# История 3.2: сервисные команды + авто-экспорт. Реалистичная фикстура — реальные view'ы
# visits/hits через views.create_views (2.6) поверх tmp-партиций (2.2), как в test_views.py.
# Существующая фикстура `db` (простая таблица) НЕ меняется — регресс 3.1 остаётся зелёным.
# ========================================================================================


def _catalog() -> Catalog:
    """Мини-каталог visits/hits (типы реалистичны, как в test_views.py 2.6)."""
    return Catalog(
        fields=(
            CatalogField("visits", "visit_id", "ym:s:visitID", "HUGEINT", "Идентификатор визита"),
            CatalogField("visits", "client_id", "ym:s:clientID", "HUGEINT", "Аноним. идентификатор"),
            CatalogField("visits", "watch_ids", "ym:s:watchIDs", "HUGEINT[]", "Просмотры визита"),
            CatalogField("visits", "date", "ym:s:date", "DATE", "Дата визита"),
            CatalogField("visits", "page_views", "ym:s:pageViews", "INTEGER", "Глубина просмотра"),
            CatalogField("hits", "watch_id", "ym:pv:watchID", "HUGEINT", "Идентификатор события"),
            CatalogField("hits", "goals_id", "ym:pv:goalsID", "BIGINT[]", "Номера целей"),
            CatalogField("hits", "date", "ym:pv:date", "DATE", "Дата события"),
        )
    )


_VISITS_COLUMNS = ["ym:s:visitID", "ym:s:clientID", "ym:s:watchIDs", "ym:s:date", "ym:s:pageViews"]
_HITS_COLUMNS = ["ym:pv:watchID", "ym:pv:goalsID", "ym:pv:date"]


@pytest.fixture
def views_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Хранилище с реальными view'ами visits/hits + таблица со смешанным регистром (3.2).

    visits — 6 строк (чтобы ``--sample`` default=5 проверялся на усечении), hits — 2 строки.
    Дополнительно таблица ``Mixed_Case`` (имя с ``_`` и заглавными) — грунт под квотирование
    идентификатора (AC #4). View'ы персистятся в gdau.duckdb write-conn'ом; MCP читает read-only.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    visits_rows = [[str(i), str(100 + i), "[1,2]", "2026-05-20", str(i)] for i in range(1, 7)]
    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, visits_rows, catalog=_catalog())
    hits_rows = [["10", "[1]", "2026-05-20"], ["20", "[2]", "2026-05-20"]]
    write_partition("hits", "2026-05-20", _HITS_COLUMNS, hits_rows, catalog=_catalog())
    with DatabaseManager.connection() as conn:
        create_views(conn, catalog=_catalog())
        conn.execute('CREATE TABLE "Mixed_Case"(x INTEGER)')
        conn.execute('INSERT INTO "Mixed_Case" VALUES (1), (2)')
    return tmp_path


# --- AC #1: сервис-команды --tables / --schema / --schema TABLE / --sample ---------------


def test_tables_lists_visits_and_hits(views_db: Path) -> None:
    """`--tables` → список объектов рабочего слоя, среди них visits и hits (AC #1)."""
    parsed = json.loads(core.handle_query("--tables", "json", 100))
    names = {row["table_name"] for row in parsed["rows"]}
    assert {"visits", "hits"} <= names


def test_schema_all_objects(views_db: Path) -> None:
    """`--schema` (без таблицы) → схема всех объектов (table_name/column_name/data_type) (AC #1)."""
    parsed = json.loads(core.handle_query("--schema", "json", 1000))
    assert parsed["columns"] == ["table_name", "column_name", "data_type"]
    assert {row["table_name"] for row in parsed["rows"]} >= {"visits", "hits"}


def test_schema_single_table_columns_with_semantics(
    views_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--schema visits` → колонки/типы + колонка `semantics` из каталога (3.3, AC #2).

    Смена контракта 3.2→3.3 (НЕ регресс): 3.2 отдавала ровно `column_name`/`data_type`, 3.3
    обогащает третьей колонкой `semantics` (описание поля из каталога). Каталог детерминирован
    через подмену `core.load_catalog` мини-каталогом — тот же, из которого собраны view'ы фикстуры.
    """
    monkeypatch.setattr(core, "load_catalog", lambda *a, **k: _catalog())
    parsed = json.loads(core.handle_query("--schema visits", "json", 100))
    # Три колонки: имя, тип и семантика из каталога.
    assert parsed["columns"] == ["column_name", "data_type", "semantics"]
    by_col = {row["column_name"]: row["semantics"] for row in parsed["rows"]}
    assert {"visit_id", "watch_ids", "page_views"} <= set(by_col)
    # Семантика visit_id — ровно описание из каталога (не Direct/НДС).
    assert by_col["visit_id"] == "Идентификатор визита"
    # Каждая строка несёт строго три поля.
    assert all(
        set(row.keys()) == {"column_name", "data_type", "semantics"} for row in parsed["rows"]
    )


def test_sample_returns_n_rows(views_db: Path) -> None:
    """`--sample visits 3` → не более 3 строк-примеров (AC #1)."""
    parsed = json.loads(core.handle_query("--sample visits 3", "json", 100))
    assert parsed["total_rows"] == 3


# --- AC #4: not-found + инъекция через имя (двух-слойная валидация + квотирование) -------


def test_schema_nonexistent_table_not_found(views_db: Path) -> None:
    """`--schema nonexist` → понятный not-found со списком известных, не сырой DuckDB-error (AC #4)."""
    out = core.handle_query("--schema nonexist", "json", 100)
    assert "не найдена" in out
    assert "**SQL Error" not in out  # не сырая ошибка движка
    assert "visits" in out  # список известных объектов подсказан


def test_sample_nonexistent_table_not_found(views_db: Path) -> None:
    """`--sample nonexist` → not-found (слой 2: проверка существования, AC #4)."""
    out = core.handle_query("--sample nonexist", "json", 100)
    assert "не найдена" in out


def test_schema_injection_via_name_rejected(views_db: Path) -> None:
    """`--schema visits; DROP TABLE visits` → отклонено слоем 1 (regex), visits цела (AC #4)."""
    out = core.handle_query("--schema visits; DROP TABLE visits", "json", 100)
    assert "Недопустимое имя" in out
    # visits не пострадала (инъекция не дошла до БД).
    assert "visits" in core.handle_query("--tables", "json", 100)


def test_sample_injection_via_name_rejected(views_db: Path) -> None:
    """`--sample visits"; DROP` → спецсимволы в имени отклонены слоем 1 (AC #4)."""
    out = core.handle_query('--sample visits"; DROP', "json", 100)
    assert "Недопустимое имя" in out


def test_sample_quoted_identifier_mixed_case_reads(views_db: Path) -> None:
    """Имя с `_`/смешанным регистром (`Mixed_Case`) читается через квотирование `"name"` (AC #4)."""
    parsed = json.loads(core.handle_query("--sample Mixed_Case", "json", 100))
    assert parsed["total_rows"] == 2  # таблица найдена и прочитана, идентификатор квотирован


# --- AC #2/#8: авто-экспорт >500, граница строго '>' (без off-by-one) --------------------


def test_auto_export_above_threshold(views_db: Path) -> None:
    """501 строка → авто-экспорт в data/results/ + статус-сообщение, не inline (AC #2/#8)."""
    out = core.execute_query("SELECT * FROM range(501)", "json", 10)
    assert "Результат велик (501 строк)" in out
    assert "Экспортировано 501 строк" in out

    files = list(get_results_dir().glob("auto_export_*.csv"))
    assert len(files) == 1
    # Файл содержит 501 строку данных + строка заголовка (CSV с HEADER).
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 502


def test_exactly_threshold_stays_inline(views_db: Path) -> None:
    """Ровно 500 строк → inline (граница строго '>', 500 НЕ экспортируется, AC #8)."""
    parsed = json.loads(core.execute_query("SELECT * FROM range(500)", "json", 1000))
    assert parsed["total_rows"] == 500
    # Файл авто-экспорта НЕ создан (off-by-one закреплён: 500 не > 500).
    assert not list(get_results_dir().glob("auto_export_*.csv"))


def test_mid_range_truncated_inline_not_exported(views_db: Path) -> None:
    """300 строк при дефолтном limit=100 → усечено до 100 inline (has_more), файл НЕ создан (риск №5)."""
    parsed = json.loads(core.execute_query("SELECT * FROM range(300)", "json", 100))
    assert parsed["total_rows"] == 300
    assert len(parsed["rows"]) == 100  # усечено дисплей-лимитом, не авто-экспортом
    assert parsed["has_more"] is True
    assert not list(get_results_dir().glob("auto_export_*.csv"))  # 300 ≤ 500 → не файл


# --- AC #5: путь экспорта принудительно под data/results/ (traversal/abs → отказ) --------


def test_export_parent_traversal_rejected(views_db: Path) -> None:
    """`--export "SELECT 1" ../evil.csv` → отказ, файл вне data/results/ НЕ создан (AC #5)."""
    out = core.handle_query('--export "SELECT 1" ../evil.csv')
    assert "data/results/" in out
    assert not (get_results_dir().parent / "evil.csv").exists()  # {root}/data/evil.csv не создан


def test_export_absolute_path_rejected(views_db: Path, tmp_path: Path) -> None:
    """`--export` с абсолютным путём → отказ, файл по абсолютному пути НЕ создан (AC #5)."""
    outside = (tmp_path / "outside_evil.csv").as_posix()  # as_posix: shlex не съест разделители
    out = core.handle_query(f'--export "SELECT 1" {outside}')
    assert "data/results/" in out
    assert not (tmp_path / "outside_evil.csv").exists()


# --- AC #6: расширение валидируется; существующий файл не перезаписывается молча ----------


def test_export_unknown_extension_rejected(views_db: Path) -> None:
    """`--export … report.txt` → отказ; НЕ создаётся ни report.txt, ни до-приписанный report.txt.csv (AC #6)."""
    out = core.handle_query('--export "SELECT 1" report.txt')
    assert "расширение" in out.lower()
    results = get_results_dir()
    assert not (results / "report.txt").exists()
    assert not (results / "report.txt.csv").exists()  # НЕ до-приписали .csv как directaiq


def test_export_existing_file_not_clobbered(views_db: Path) -> None:
    """`--export` в существующий файл → отказ, исходное содержимое цело (AC #6)."""
    results = get_results_dir()
    results.mkdir(parents=True, exist_ok=True)
    (results / "existing.csv").write_text("original-content", encoding="utf-8")

    out = core.handle_query('--export "SELECT 1" existing.csv')
    assert "уже существует" in out
    assert (results / "existing.csv").read_text(encoding="utf-8") == "original-content"


def test_export_each_format_writes_file(views_db: Path) -> None:
    """`.csv`/`.parquet`/`.json` → файл создан в data/results/ (AC #6 — валидные расширения)."""
    for filename in ("out.csv", "out.parquet", "out.json"):
        out = core.handle_query(f'--export "SELECT 1 AS x" {filename}')
        assert "Экспортировано" in out, filename
        assert (get_results_dir() / filename).exists(), filename


# --- Риск №1: внутренний SQL экспорта проходит read-only guard (--export "DROP…" → отказ) -


def test_export_internal_sql_must_be_readonly(views_db: Path) -> None:
    """`--export "DROP TABLE visits" x.csv` → отказ guard'ом, visits цела, файл НЕ создан (риск №1)."""
    out = core.handle_query('--export "DROP TABLE visits" leak.csv')
    assert "только для чтения" in out
    assert not (get_results_dir() / "leak.csv").exists()
    # visits не удалена.
    assert "visits" in core.handle_query("--tables", "json", 100)


# --- AC #7: отсутствующий каталог data/results/ создаётся на месте записи -----------------


def test_export_creates_missing_results_dir(views_db: Path) -> None:
    """Нет data/results/ → `--export` создаёт каталог на месте записи (AC #7, риск №3)."""
    results = get_results_dir()
    assert not results.exists()  # фикстура каталог результатов не создаёт

    out = core.handle_query('--export "SELECT 1 AS x" made.csv')
    assert "Экспортировано" in out
    assert results.exists()
    assert (results / "made.csv").exists()


# --- AC #8: команды до первой выгрузки → дружелюбная подсказка, не сырой RuntimeError -----


def test_service_commands_before_data_friendly_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Хранилище без gdau.duckdb → все команды дают подсказку про `gdau-logs update` (AC #8)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))  # каталог есть, БД ещё нет
    for command in (
        "--tables",
        "--schema visits",
        "--sample visits",
        '--export "SELECT 1" x.csv',
    ):
        out = core.handle_query(command)
        assert "gdau-logs update" in out, command
        assert "**Error:** RuntimeError" not in out, command


# --- AC #10: --sample N — дефолт и клампинг ----------------------------------------------


def test_sample_default_n_is_five(views_db: Path) -> None:
    """`--sample visits` без N → DEFAULT_SAMPLE=5 строк (visits содержит 6) (AC #10)."""
    parsed = json.loads(core.handle_query("--sample visits", "json", 100))
    assert parsed["total_rows"] == 5


def test_sample_zero_clamped_to_one(views_db: Path) -> None:
    """`--sample visits 0` → 1 строка (max(1,0)=1, НЕ пустой LIMIT 0 как directaiq) (AC #10)."""
    parsed = json.loads(core.handle_query("--sample visits 0", "json", 100))
    assert parsed["total_rows"] == 1


def test_sample_negative_n_falls_back_to_default(views_db: Path) -> None:
    """`--sample visits -3` → ≥1 (isdecimal()=False → дефолт 5), не пустой/не ошибка (AC #10)."""
    parsed = json.loads(core.handle_query("--sample visits -3", "json", 100))
    assert parsed["total_rows"] == 5


# ========================================================================================
# Code-review 2026-05-26: регресс-патчи P1 (юникод-цифра в --sample) и P3 (подкаталог --export)
# ========================================================================================


def test_sample_unicode_digit_does_not_crash(views_db: Path) -> None:
    """`--sample visits ²` (юникод-надстрочная цифра U+00B2) НЕ роняет инструмент (P1, AC #10).

    `'²'.isdigit()` == True, но `int('²')` бросает ValueError мимо try/except (он в execute_query,
    а int() срабатывает ДО её вызова) → раньше исключение летело наружу из инструмента. `isdecimal()`
    её отсеивает → падаем на DEFAULT_SAMPLE=5, результат строкой, сервер жив.
    """
    parsed = json.loads(core.handle_query("--sample visits ²", "json", 100))
    assert parsed["total_rows"] == 5  # дефолт, а не исключение наружу


def test_export_subdirectory_rejected(views_db: Path) -> None:
    """`--export "…" sub/out.csv` (подкаталог) → дружелюбный отказ, не сырой IO Error (P3, AC #5).

    Путь остаётся внутри data/results/ (is_relative_to=True), но mkdir не создаёт подкаталог →
    раньше COPY падал сырым `**SQL Error:** IO Error … "<абс.путь>"` с утечкой пути хранилища.
    Теперь — понятный отказ, файл и подкаталог не создаются.
    """
    out = core.handle_query('--export "SELECT 1 AS x" sub/out.csv')
    assert "подкаталог" in out.lower()
    assert "**SQL Error" not in out  # не сырая ошибка движка с утечкой абсолютного пути
    assert not (get_results_dir() / "sub").exists()


# ========================================================================================
# История 3.3: --context (авто-контекст рабочего слоя) + семантика колонок из каталога.
# Фикстура context_db = реальные view'ы visits/hits (как views_db) + мета-таблица load_state
# (её в views_db НЕТ — нужна для AC #1/#8) + подмена core.load_catalog мини-каталогом _catalog()
# (детерминизм семантики, риск №7: тот же каталог, из которого собраны view'ы).
# ========================================================================================


@pytest.fixture
def context_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Хранилище для `--context`: view'ы visits(6)/hits(2) + load_state(2), каталог подменён.

    `load_state` добавлена через `ensure_load_state_table`/`mark_loaded` — иначе AC #1 (она в
    выводе) и AC #8 (её колонки → unknown семантика) непроверяемы (в `views_db` её нет).
    `core.load_catalog` подменён на `_catalog()` — тот же мини-каталог, из которого собраны
    view'ы → семантика детерминирована и согласована. Тесты AC #7/#8 переопределяют подмену.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    visits_rows = [[str(i), str(100 + i), "[1,2]", "2026-05-20", str(i)] for i in range(1, 7)]
    write_partition("visits", "2026-05-20", _VISITS_COLUMNS, visits_rows, catalog=_catalog())
    hits_rows = [["10", "[1]", "2026-05-20"], ["20", "[2]", "2026-05-20"]]
    write_partition("hits", "2026-05-20", _HITS_COLUMNS, hits_rows, catalog=_catalog())
    with DatabaseManager.connection() as conn:
        create_views(conn, catalog=_catalog())
        ensure_load_state_table(conn)
        mark_loaded(conn, "visits", "2026-05-20", 6)
        mark_loaded(conn, "hits", "2026-05-20", 2)
    monkeypatch.setattr(core, "load_catalog", lambda *a, **k: _catalog())
    return tmp_path


@pytest.fixture
def empty_context_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Хранилище с ПУСТЫМИ view'ами visits/hits (нет партиций) — для AC #9.

    `create_views` без партиций строит view `… WHERE false` (2.6): COUNT(*)=0, MIN/MAX=NULL.
    """
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))
    with DatabaseManager.connection() as conn:
        create_views(conn, catalog=_catalog())  # нет партиций → пустые типизированные view'ы
    monkeypatch.setattr(core, "load_catalog", lambda *a, **k: _catalog())
    return tmp_path


# --- AC #1: --context перечисляет объекты с колонками/типами, row counts, диапазонами дат --


def test_context_lists_objects_with_counts_and_dates(context_db: Path) -> None:
    """`--context` → секции visits/hits/load_state с числом строк, колонками/типами, датами (AC #1)."""
    out = core.handle_query("--context")
    # Объекты присутствуют (по вхождению, НЕ точным набором — в фикстуре могут быть и др. объекты).
    assert "### visits (6 строк" in out
    assert "### hits (2 строк" in out
    assert "### load_state" in out
    # Колонки с типами.
    assert "- visit_id: HUGEINT" in out
    assert "- watch_ids:" in out  # колонка-массив присутствует (тип-строку движка не фиксируем)
    # Диапазон дат — СТРОКОЙ (CAST AS VARCHAR), не repr Python-объекта date.
    assert "2026-05-20" in out
    assert "datetime.date" not in out


def test_context_returns_markdown_ignoring_format(context_db: Path) -> None:
    """`--context` отдаёт markdown-сводку независимо от format (риск №6 — курированный текст)."""
    out_json = core.handle_query("--context", "json", 100)
    out_csv = core.handle_query("--context", "csv", 100)
    # format не меняет вывод: всегда markdown-обзор (заголовок секции на месте, не JSON/CSV).
    assert out_json == out_csv
    assert "# Контекст рабочего слоя" in out_json


# --- AC #2: семантика колонок из каталога в --context и --schema TABLE --------------------


def test_context_includes_catalog_semantics(context_db: Path) -> None:
    """Семантика колонок в `--context` = описания каталога (AC #2)."""
    out = core.handle_query("--context")
    assert "Идентификатор визита" in out  # описание visit_id (visits) из _catalog()
    assert "Идентификатор события" in out  # описание watch_id (hits) из _catalog()


def test_context_multiline_semantics_stays_on_one_line(
    context_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Описание с переводом строки не разрывает пункт markdown-списка `--context` (review-патч).

    `--context` — курированный markdown: внутренний `\\n` в описании каталога иначе расщепил бы
    один пункт `- col: type — …` на несколько физических строк. Семантика нормализуется
    (CR/LF → пробел) — паритет с `_md_escape` для ячеек таблицы на пути `--schema`.
    """
    multiline = Catalog(
        fields=tuple(
            CatalogField(
                f.source,
                f.storage_name,
                f.metrica_field,
                f.duckdb_type,
                "Строка1\nСтрока2" if f.storage_name == "visit_id" else f.description,
            )
            for f in _catalog().fields
        )
    )
    monkeypatch.setattr(core, "load_catalog", lambda *a, **k: multiline)
    out = core.handle_query("--context")
    # Пункт visit_id целиком на одной строке: описание склеено через пробел, без разрыва.
    assert "- visit_id: HUGEINT — Строка1 Строка2" in out
    # Сырого перевода строки внутри описания в выводе нет (список не расщепился).
    assert "Строка1\nСтрока2" not in out


# --- AC #7: каталог недоступен/битый → понятная ошибка строкой, сервер жив ----------------


def test_context_broken_catalog_friendly_error(
    context_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Битый каталог → `--context` отдаёт понятную ошибку строкой, не полу-контекст (AC #7)."""
    def boom(*a: object, **k: object) -> Catalog:
        raise ValueError("каталог схемы не найден (битый симлинк)")

    monkeypatch.setattr(core, "load_catalog", boom)
    out = core.handle_query("--context")
    assert "Каталог схемы недоступен" in out
    assert "### visits" not in out  # полу-собранного контекста нет


def test_schema_broken_catalog_friendly_error(
    context_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Битый каталог → `--schema visits` отдаёт понятную ошибку строкой, сервер жив (AC #7)."""
    def boom(*a: object, **k: object) -> Catalog:
        raise ValueError("каталог схемы не найден (битый симлинк)")

    monkeypatch.setattr(core, "load_catalog", boom)
    out = core.handle_query("--schema visits", "json", 100)
    assert "Каталог схемы недоступен" in out
    assert "**SQL Error" not in out


# --- AC #8: рассинхрон view↔каталог → пустая/«unknown» семантика + WARNING, без KeyError ---


def test_context_load_state_columns_unknown_semantics(context_db: Path) -> None:
    """Объект без записей в каталоге (`load_state`) → колонки с пустой семантикой, без KeyError (AC #8)."""
    out = core.handle_query("--context")
    # load_state не в каталоге → его колонки идут с «—» вместо семантики (через dict.get, не индексацию).
    assert "- source: VARCHAR — —" in out
    assert "- status: VARCHAR — —" in out


def test_schema_load_state_semantics_all_null(context_db: Path) -> None:
    """`--schema load_state` → колонка semantics есть, значения NULL (нет в каталоге, AC #8)."""
    parsed = json.loads(core.handle_query("--schema load_state", "json", 100))
    assert parsed["columns"] == ["column_name", "data_type", "semantics"]
    assert all(row["semantics"] is None for row in parsed["rows"])  # без KeyError, всё NULL


def test_context_drift_column_warns(
    context_db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Колонка view-источника, которой нет в каталоге → «—» семантика + WARNING (AC #8).

    Каталог БЕЗ `page_views`, а view `visits` его несёт → рассинхрон: семантика unknown + WARNING
    (через `dict.get` → None, не `KeyError`).
    """
    drift_catalog = Catalog(
        fields=tuple(f for f in _catalog().fields if f.storage_name != "page_views")
    )
    monkeypatch.setattr(core, "load_catalog", lambda *a, **k: drift_catalog)

    with caplog.at_level(logging.WARNING):
        out = core.handle_query("--context")

    assert "- page_views: INTEGER — —" in out  # unknown семантика, без падения
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("page_views" in r.getMessage() for r in warnings)


# --- AC #9: пустые view'ы → row_count=0 и диапазон дат null, без падения ------------------


def test_context_empty_views_zero_rows_null_range(empty_context_db: Path) -> None:
    """Пустые view'ы (нет партиций) → `--context` даёт 0 строк и пустой диапазон дат (AC #9)."""
    out = core.handle_query("--context")
    assert "### visits (0 строк)" in out  # 0 строк, без «, date: …» (MIN/MAX по пустому → NULL)
    assert "### hits (0 строк)" in out
    assert "**Error" not in out  # ни деления, ни None-разыменования


# --- Риск №6: --context до первой выгрузки → дружелюбная подсказка, не сырой RuntimeError --


def test_context_before_data_friendly_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Хранилище без gdau.duckdb → `--context` подсказывает `gdau-logs update`, не трейсбек (риск №6)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))  # каталог есть, БД ещё нет
    out = core.handle_query("--context")
    assert "gdau-logs update" in out
    assert "**Error:** RuntimeError" not in out


# --- AC #5: интерфейс цел — duckdb_query(query, format, limit) + сервис-команды 3.1/3.2 ----


def test_interface_intact_with_context(context_db: Path) -> None:
    """`--context` соседствует с произвольным SQL (3.1) и сервис-командами 3.2 — интерфейс цел (AC #5)."""
    # SQL (3.1)
    assert json.loads(core.handle_query("SELECT count(*) AS n FROM visits"))["rows"][0]["n"] == 6
    # --tables / --sample (3.2)
    assert "visits" in core.handle_query("--tables", "json", 100)
    assert json.loads(core.handle_query("--sample hits 1"))["total_rows"] == 1
    # --context (3.3) рядом
    assert "Контекст рабочего слоя" in core.handle_query("--context")


# --- AC #3/#4: directaiq-специфика (Direct/НДС/goal/config) отсутствует в коде core.py -----


def _code_identifiers(py_file: Path) -> set[str]:
    """Идентификаторы, ИСПОЛЬЗУЕМЫЕ в коде (имена/определения/атрибуты) — НЕ из строк/комментариев.

    AST-обход: docstring'и шапки намеренно упоминают эти символы словами («НЕ переносятся») —
    проверка по подстроке дала бы ложный красный. Берём только реальные code-узлы.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_no_directaiq_money_goal_code_symbols_in_core() -> None:
    """В коде core.py НЕТ Direct/НДС/goal/config-символов (AC #3/#4 — никогда не вендорились).

    Проверка по реальным code-идентификаторам (ast), НЕ по подстроке: шапка модуля упоминает эти
    имена словами, фиксируя их принципиальное отсутствие. Замена `_COST_COLUMN_SEMANTICS` —
    семантика из каталога (`load_catalog`/`Catalog.descriptions`), а не денег/НДС.
    """
    used = _code_identifiers(Path(core.__file__))
    forbidden = {
        "_COST_COLUMN_SEMANTICS",
        "_annotate_money_column",
        "_GENERIC_MONEY_COL_RE",
        "_MONEY_COL_TYPES",
        "process_sql_placeholders",
        "get_config",
    }
    offenders = used & forbidden
    assert not offenders, f"directaiq-символы просочились в код core.py: {offenders}"


def test_core_uses_catalog_for_semantics() -> None:
    """Семантика в core.py берётся из каталога: используются load_catalog/VALID_SOURCES (AC #2)."""
    used = _code_identifiers(Path(core.__file__))
    assert "load_catalog" in used
    assert "VALID_SOURCES" in used
