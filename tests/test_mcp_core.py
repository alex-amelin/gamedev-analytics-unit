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

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest

from scripts.mcp.tools import core
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.env_reader import DATA_ROOT_ENV


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
