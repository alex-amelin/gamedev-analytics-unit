"""Ядро инструмента ``duckdb_query`` — исполнение произвольного SQL к рабочему слою.

Тонкий **канал чтения**: агент шлёт SQL → результат в ``json``/``markdown``/``csv`` по
view'ам ``visits``/``hits`` (2.6). Соединение всегда **read-only** (:class:`DatabaseManager`
2.1); ``.writer.lock`` (2.5) не берётся. Запись невозможна **двумя слоями**: (а) соединение
``read_only=True`` и (б) statement-guard, пропускающий только read-операции — потому что
эмпирически (DuckDB 1.5.3) под ``read_only`` ``COPY … TO`` всё равно пишет файл, а ``PRAGMA``
проходит, т.е. одного ``read_only`` недостаточно.

# vendored from directaiq @ scripts/mcp/tools/core.py, seam: read-only + statement-guard;
# trimmed: config_manager/placeholders/context/schema/export/audit/Direct-VAT-semantics → 3.2/3.3.
Из directaiq перенесены форматтеры (``format_result_*``) и классификатор ошибок
(``_format_sql_error``); добавлены statement-guard записи (риск №1/AC #7), watchdog-таймаут
через ``conn.interrupt()`` (риск №2/AC #11 — ``statement_timeout``-PRAGMA в DuckDB нет),
однократный retry на транзиентном чтении партиции (риск №4/AC #9) и кламп лимита
``[1, MAX_LIMIT]`` (риск №5/AC #10 — авто-экспорта больших результатов в 3.1 ещё нет).
**Не** перенесены спец-команды (``--context``/``--tables``/``--schema``/``--sample``/
``--export``), goal-плейсхолдеры, audit и Direct/НДС-семантика — это 3.2/3.3.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Sequence
from typing import Any

import duckdb

from scripts.utils.database_manager import DatabaseManager

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "execute_query", "handle_query"]

# --- Константы канала ------------------------------------------------------------------

#: Лимит строк по умолчанию (≤0 / пропуск). Отличие от directaiq (там 0 = «без лимита» +
#: авто-экспорт >500): в 3.1 авто-экспорта нет, поэтому результат всегда ограничен (риск №5).
DEFAULT_LIMIT = 100
#: Жёсткий потолок строк в ответе агенту (риск №5/AC #10).
MAX_LIMIT = 10_000
#: Верхняя граница времени исполнения запроса (риск №2/AC #11). Прерывание — watchdog-таймером
#: + ``conn.interrupt()``: PRAGMA/SET ``statement_timeout`` в DuckDB 1.5.3 не существует.
STATEMENT_TIMEOUT_S = 30.0
#: Пауза перед однократным повтором чтения при транзиентной IOException (риск №4/AC #9).
_RETRY_SLEEP_S = 0.1

_SUPPORTED_FORMATS = ("json", "markdown", "csv")

#: Ведущие ключевые слова read-операций (allowlist). Всё прочее (COPY/PRAGMA/SET/ATTACH/
#: INSTALL/LOAD/CREATE/INSERT/UPDATE/DELETE/DROP/ALTER/CALL/CHECKPOINT/EXPORT/IMPORT) — отказ.
_READ_ONLY_LEADING_KEYWORDS = frozenset(
    {
        "SELECT",
        "WITH",
        "FROM",
        "DESCRIBE",
        "EXPLAIN",
        "SHOW",
        "VALUES",
        "SUMMARIZE",
        "TABLE",
        "PIVOT",
        "UNPIVOT",
    }
)


# --- Statement-guard записи (риск №1/AC #7) --------------------------------------------


def _strip_leading_comments(sql: str) -> str:
    """Срезать ведущие комментарии (``-- …`` и ``/* … */``) и пробелы.

    Без этого ``'/* x */ COPY (…) TO …'`` / ``'-- c\\nCOPY …'`` обошли бы allowlist ведущего
    слова (проверено вживую: под ``read_only`` такой запрос ПИШЕТ файл). Цикл — комментариев
    может быть несколько подряд.
    """
    s = sql.lstrip()
    while True:
        if s.startswith("--"):
            newline = s.find("\n")
            s = "" if newline == -1 else s[newline + 1 :]
            s = s.lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2 :]
            s = s.lstrip()
        else:
            return s


def _reject_if_not_readonly(sql: str) -> str | None:
    """Вернуть текст отказа, если ``sql`` не является одиночной read-операцией, иначе ``None``.

    Второй слой read-only-дисциплины (первый — ``read_only=True`` соединения). Срезает ведущие
    комментарии и пробелы, отбрасывает хвостовой ``;``; если ``;`` остался внутри → мульти-стейтмент
    (``SELECT 1; COPY (…) TO 'x'`` обошёл бы проверку ведущего слова) → отказ. Затем смотрит
    ведущее ключевое слово (с учётом ведущих ``(`` для ``(SELECT …)``); не в allowlist → отказ.
    Регистронезависимо.

    Консервативно: ``;`` внутри строкового литерала (``SELECT ';'``) тоже даст отказ — для канала
    только-чтения это безопасный fail (отклоняем валидное), не дыра.
    """
    body = _strip_leading_comments(sql).rstrip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if not body:
        return "Канал только для чтения: пустой запрос после удаления комментариев."

    if ";" in body:
        return (
            "Канал только для чтения: разрешён ровно один запрос — несколько стейтментов "
            "(разделённых ';') отклонены."
        )

    head = body.lstrip("( \t\r\n")
    # EXPLAIN ANALYZE ИСПОЛНЯЕТ вложенный запрос (проверено на DuckDB 1.5.3:
    # `EXPLAIN ANALYZE COPY (…) TO 'file'` ПИШЕТ файл даже под read_only, тогда как `EXPLAIN COPY`
    # без ANALYZE — нет). Поэтому срезаем ведущий префикс `EXPLAIN [ANALYZE]` и валидируем ведущее
    # слово ВЛОЖЕННОГО стейтмента — иначе `EXPLAIN` в allowlist пропускал бы запись через ANALYZE.
    head = re.sub(
        r"^(?:EXPLAIN\s+(?:ANALYZE\s+)?)+", "", head, flags=re.IGNORECASE
    ).lstrip("( \t\r\n")
    match = re.match(r"[A-Za-z_]+", head)
    keyword = match.group(0).upper() if match else ""
    if keyword not in _READ_ONLY_LEADING_KEYWORDS:
        shown = keyword or head[:16]
        return (
            f"Канал только для чтения: операция '{shown}' запрещена. Разрешены только запросы "
            "чтения (SELECT/WITH/FROM/DESCRIBE/EXPLAIN/SHOW/VALUES/SUMMARIZE/TABLE/PIVOT/UNPIVOT)."
        )
    return None


def _clamp_limit(limit: int) -> int:
    """Зажать лимит строк в ``[1, MAX_LIMIT]`` (риск №5/AC #10).

    ``≤0`` → :data:`DEFAULT_LIMIT`; ``> MAX_LIMIT`` → :data:`MAX_LIMIT`; иначе как есть.
    """
    if limit <= 0:
        return DEFAULT_LIMIT
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


# --- Форматтеры результата (вендоринг directaiq, переименован брендинг) ------------------


def _md_escape(text: str) -> str:
    """Экранировать ячейку markdown-таблицы: ``|`` → ``\\|``; ``\\r``/``\\n`` → пробел.

    Перевод строки в значении (свободный текст/referer) иначе разрывает один логический ряд на
    несколько физических строк → таблица разъезжается. Применяется и к заголовку, и к ячейкам.
    """
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _csv_quote(text: str) -> str:
    """RFC4180-квотирование ячейки CSV: запятая/кавычка/перевод строки → значение в кавычках.

    Применяется к заголовку И к значениям — имя колонки тоже может нести ``,``/``"`` (алиас
    ``SELECT 1 AS "a,b"``), иначе шапка разъедется относительно строк данных.
    """
    if "," in text or '"' in text or "\n" in text or "\r" in text:
        return '"' + text.replace('"', '""') + '"'
    return text


def format_result_markdown(
    columns: list[str], rows: Sequence[tuple[Any, ...]], limit: int
) -> str:
    """Отформатировать результат как markdown-таблицу (усечение по ``limit`` с подсказкой)."""
    if not rows:
        return "_Строк не возвращено_"

    truncated = len(rows) > limit
    display_rows = rows[:limit]

    lines: list[str] = []
    header = " | ".join(_md_escape(c) for c in columns)
    lines.append(f"| {header} |")
    lines.append(f"| {' | '.join(['---'] * len(columns))} |")

    for row in display_rows:
        values: list[str] = []
        for val in row:
            if val is None:
                values.append("NULL")
            else:
                text = str(val)
                if len(text) > 50:
                    text = text[:47] + "..."
                text = _md_escape(text)
                values.append(text)
        lines.append(f"| {' | '.join(values)} |")

    result = "\n".join(lines)
    if truncated:
        result += f"\n\n_… ещё {len(rows) - limit} строк (лимит: {limit})_"
    return result


def format_result_json(
    columns: list[str], rows: Sequence[tuple[Any, ...]], limit: int
) -> str:
    """Отформатировать результат как JSON; несёт ``total_rows``/``has_more``/``next_offset``."""
    display_rows = rows[:limit]
    data: list[dict[str, Any]] = []
    for row in display_rows:
        record: dict[str, Any] = {}
        for i, col in enumerate(columns):
            val = row[i]
            # Привести к JSON-сериализуемому: NaN/Inf → null, дата/время → isoformat.
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                val = None
            elif hasattr(val, "isoformat"):
                val = val.isoformat()
            record[col] = val
        data.append(record)

    total = len(rows)
    has_more = total > limit
    result: dict[str, Any] = {
        "columns": columns,
        "rows": data,
        "total_rows": total,
        "limit": limit,
        "has_more": has_more,
        "next_offset": limit if has_more else None,
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def format_result_csv(
    columns: list[str], rows: Sequence[tuple[Any, ...]], limit: int
) -> str:
    """Отформатировать результат как CSV (RFC4180-квотирование значений с запятой/кавычкой)."""
    display_rows = rows[:limit]
    lines: list[str] = [",".join(_csv_quote(c) for c in columns)]
    for row in display_rows:
        values: list[str] = []
        for val in row:
            values.append("" if val is None else _csv_quote(str(val)))
        lines.append(",".join(values))
    return "\n".join(lines)


# --- Классификатор ошибок SQL (вендоринг; подсказки адаптированы под 3.1) ----------------


def _format_sql_error(error: duckdb.Error, query: str) -> str:
    """Понятный текст ошибки SQL с подсказкой (классификация по тексту движка).

    Подсказки адаптированы под 3.1: спец-команд ``--tables``/``--schema`` ещё нет (они в 3.2),
    поэтому при «does not exist» подсказываем доступные view'ы, а не несуществующую команду.
    ``query`` сохранён в сигнатуре для паритета с источником (в тексте не используется).
    """
    msg = str(error)
    low = msg.lower()
    suggestions: list[str] = []

    if "does not exist" in low or "not found" in low:
        suggestions.append(
            "Проверь имя таблицы/колонки — доступны view'ы `visits` и `hits` (snake_case-колонки)."
        )
    elif "syntax error" in low or "parser error" in low:
        suggestions.append("Проверь синтаксис SQL рядом с указанной позицией.")
    elif "could not convert" in low or "type mismatch" in low:
        suggestions.append("Используй CAST(колонка AS тип) для явного приведения типов.")
    elif "division by zero" in low:
        suggestions.append("Используй NULLIF(знаменатель, 0) для защиты от деления на ноль.")

    result = f"**SQL Error:** {msg}"
    if suggestions:
        result += "\n\n**Подсказки:**\n" + "\n".join(f"- {s}" for s in suggestions)
    return result


# --- Исполнение запроса ------------------------------------------------------------------


def _execute_with_timeout(
    conn: duckdb.DuckDBPyConnection, query: str, timeout_s: float
) -> list[Any]:
    """Исполнить ``query`` с верхней границей времени (риск №2/AC #11).

    PRAGMA/SET ``statement_timeout`` в DuckDB 1.5.3 НЕ существует (``CatalogException``) →
    watchdog: :class:`threading.Timer` через ``timeout_s`` бьёт ``conn.interrupt()`` →
    ``conn.execute`` поднимает :class:`duckdb.InterruptException`.

    ``timer.cancel()`` НЕ отменяет уже-запущенный колбэк, но это безопасно: ``conn`` —
    per-call и закрывается в том же ``with``-блоке сразу за исполнением (``interrupt`` на
    закрывающемся соединении = no-op, в следующий запрос не «протекает»).
    """
    timer = threading.Timer(timeout_s, conn.interrupt)
    timer.start()
    try:
        return conn.execute(query).fetchall()
    finally:
        timer.cancel()


def execute_query(
    query: str, output_format: str = "json", limit: int = DEFAULT_LIMIT
) -> str:
    """Исполнить read-SQL и вернуть результат строкой в ``output_format`` (ловит все ошибки).

    Поток: guard записи (до открытия соединения) → read-only-соединение (2.1) → таймаут+retry →
    fetch → формат. Любая ошибка возвращается строкой (сервер не падает, риск №6/AC #6).
    """
    # Слой guard'а ДО открытия соединения (риск №1/AC #7): read_only сам пропускает COPY TO/
    # PRAGMA (проверено на DuckDB 1.5.3), поэтому запись режем allowlist'ом ведущего слова.
    rejection = _reject_if_not_readonly(query)
    if rejection is not None:
        return rejection

    display_limit = _clamp_limit(limit)
    # Неизвестный формат → дефолт json (AC #4/AC #10); Literal-тип инструмента отсекает его раньше.
    fmt = output_format if output_format in _SUPPORTED_FORMATS else "json"

    try:
        # read-only: до создания gdau.duckdb → RuntimeError «… gdau-logs update» ДО connect
        # (AC #8 наследуется из 2.1); лок не берётся (FR-15/2.5).
        with DatabaseManager.connection(read_only=True) as conn:
            try:
                rows = _execute_with_timeout(conn, query, STATEMENT_TIMEOUT_S)
            except duckdb.IOException:
                # риск №4/AC #9: оркестратор (2.7) подменяет партицию os.replace во время чтения
                # parquet-glob view'а → транзиентная IOException. ОДНОКРАТНЫЙ повтор; синтаксис/
                # каталог сюда не попадают (они не IOException) — их не ретраим (это AC #6).
                logger.warning(
                    "Транзиентная IOException чтения партиции — однократный повтор через %.2f c",
                    _RETRY_SLEEP_S,
                )
                time.sleep(_RETRY_SLEEP_S)
                rows = _execute_with_timeout(conn, query, STATEMENT_TIMEOUT_S)

            if conn.description is None:
                return "_Запрос выполнен (без результата)_"
            columns = [str(desc[0]) for desc in conn.description]

            if fmt == "json":
                return format_result_json(columns, rows, display_limit)
            if fmt == "csv":
                return format_result_csv(columns, rows, display_limit)
            return format_result_markdown(columns, rows, display_limit)

    except duckdb.InterruptException:
        # риск №2/AC #11: watchdog прервал «убегающий» запрос (ловим ДО duckdb.Error — подкласс).
        return (
            f"Запрос превысил лимит времени (~{STATEMENT_TIMEOUT_S:.0f} c) — "
            "упростите запрос или добавьте фильтры/LIMIT."
        )
    except RuntimeError as exc:
        # AC #8: DatabaseManager (2.1) бросает RuntimeError «БД не инициализирована … gdau-logs
        # update» ДО connect (read-only без файла БД). Наружу — понятный текст строкой (риск №6:
        # сервер жив), не сырой IOException движка.
        return str(exc)
    except duckdb.Error as exc:
        return _format_sql_error(exc, query)
    except Exception as exc:
        # Риск №6: голых исключений из инструмента наружу не выпускаем — иначе MCP-сессия рвётся.
        return f"**Error:** {type(exc).__name__}: {exc!s}"


def handle_query(
    query: str, output_format: str = "json", limit: int = DEFAULT_LIMIT
) -> str:
    """Входная точка инструмента: пустой запрос → подсказка; иначе исполнить SQL.

    В 3.1 — **только** произвольный SQL (роутинг спец-команд ``--context``/``--tables``/… добавит
    3.2 поверх этой же функции). Если агент пришлёт ``--tables`` сейчас — уйдёт в DuckDB как SQL →
    понятная синтаксическая ошибка (AC #6), не падение.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return (
            "Пустой запрос. Пришли SQL к view'ам `visits`/`hits`, "
            "например: SELECT count(*) FROM visits."
        )
    return execute_query(cleaned, output_format, limit)
