"""Ядро инструмента ``duckdb_query`` — исполнение произвольного SQL к рабочему слою.

Тонкий **канал чтения**: агент шлёт SQL → результат в ``json``/``markdown``/``csv`` по
view'ам ``visits``/``hits`` (2.6). Соединение всегда **read-only** (:class:`DatabaseManager`
2.1); ``.writer.lock`` (2.5) не берётся. Запись невозможна **двумя слоями**: (а) соединение
``read_only=True`` и (б) statement-guard, пропускающий только read-операции — потому что
эмпирически (DuckDB 1.5.3) под ``read_only`` ``COPY … TO`` всё равно пишет файл, а ``PRAGMA``
проходит, т.е. одного ``read_only`` недостаточно.

# vendored from directaiq @ scripts/mcp/tools/core.py, seam: read-only + statement-guard +
# guard внутреннего SQL экспорта + path-резолверы из нашего paths.py + семантика колонок
# ИЗ НАШЕГО КАТАЛОГА (не Direct/НДС);
# trimmed: config_manager/goal-плейсхолдеры/Direct-VAT-semantics НЕ переносятся принципиально
# (никогда не вендорились — у геймдева рекламного Директа нет, см. история 3.3 риск №1).
Из directaiq перенесены форматтеры (``format_result_*``) и классификатор ошибок
(``_format_sql_error``); добавлены statement-guard записи (риск №1/AC #7), watchdog-таймаут
через ``conn.interrupt()`` (риск №2/AC #11 — ``statement_timeout``-PRAGMA в DuckDB нет),
однократный retry на транзиентном чтении партиции (риск №4/AC #9) и кламп лимита
``[1, MAX_LIMIT]`` (риск №5/AC #10).

3.2 нарастил **сервисный слой** поверх тонкого read-канала: роутинг спец-команд
``--tables``/``--schema [TABLE]``/``--sample TABLE [N]``/``--export`` в :func:`handle_query`,
авто-экспорт результатов ``> AUTO_EXPORT_THRESHOLD`` в :func:`execute_query`, общий
COPY-хелпер :func:`_run_copy_export`, безопасный :func:`_export_query` (guard внутреннего
SQL/расширение/traversal/клоббер), ``--schema`` plain через :func:`_handle_schema`,
двух-слойная валидация имени таблицы (:func:`_validate_table_name` + проверка существования).

3.3 (ФИНАЛ Epic 3, FR-18) замкнул контекст/семантику под **нашу** схему: добавлен
``--context``/:func:`_handle_context` (объекты/колонки/типы + row counts + диапазоны дат
рабочего слоя одним вызовом, механика information_schema/COUNT/MIN-MAX адаптирована из
directaiq), а ``--schema TABLE`` обогащён колонкой ``semantics``. Источник семантики —
``description`` нашего каталога схемы (FR-16, :meth:`Catalog.descriptions`), это «замена
``_COST_COLUMN_SEMANTICS``». Direct/НДС-аннотаторы (``_COST_COLUMN_SEMANTICS``/
``_annotate_money_column``/``_GENERIC_MONEY_COL_RE``), goal-плейсхолдеры
(``process_sql_placeholders``/``{{PRIMARY_GOAL_ID}}``) и ``config_manager`` здесь
**отсутствуют как класс** — они не вендорились (закреплено guard/ast-тестами); секции
``## Money/Goal/Config`` directaiq-контекста НЕ переносятся (директовая разметка, не наша).
"""

from __future__ import annotations

import json
import logging
import math
import re
import shlex
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from scripts.utils.catalog import VALID_SOURCES, load_catalog
from scripts.utils.database_manager import DatabaseManager
from scripts.utils.paths import get_results_dir

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "execute_query", "handle_query"]

# --- Константы канала ------------------------------------------------------------------

#: Лимит строк по умолчанию (≤0 / пропуск). Отличие от directaiq (там 0 = «без лимита» +
#: авто-экспорт >500): в 3.1 авто-экспорта нет, поэтому результат всегда ограничен (риск №5).
DEFAULT_LIMIT = 100
#: Жёсткий потолок строк в ответе агенту (риск №5/AC #10).
MAX_LIMIT = 10_000
#: Порог авто-экспорта (3.2, AC #2/#8): результат **строго >** порога уходит в файл вместо
#: переполнения ответа; ≤ порога — inline. Считается по ``len(rows)`` (полный fetch), ОРТОГОНАЛЬНО
#: дисплей-клампу ``_clamp_limit`` (риск №5): 500 → inline, 501 → авто-экспорт (граница без off-by-one).
AUTO_EXPORT_THRESHOLD = 500
#: Размер выборки ``--sample TABLE`` по умолчанию (3.2, AC #10): ``N`` отсутствует/нечисловой → столько строк.
DEFAULT_SAMPLE = 5
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

#: Санитизация имени таблицы (3.2, AC #4, слой 1): только буквы/цифры/подчёркивание —
#: отсекает инъекцию/спецсимволы/пробелы через идентификатор ДО обращения к БД (вендоринг verbatim).
_VALID_TABLE_NAME = re.compile(r"^[A-Za-z0-9_]+$")

#: Допустимые расширения файла ``--export`` (3.2, AC #6): иное → отказ (НЕ молчаливое
#: до-приписывание ``.csv`` как в directaiq).
_EXPORT_EXTENSIONS = frozenset({".csv", ".parquet", ".json"})


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

            # Авто-экспорт (3.2, AC #2/#8): результат СТРОГО > порога не возвращаем inline (не
            # переполняем ответ агента) — пишем в файл-результат и отдаём статус-сообщение. Порог по
            # len(rows) (полный fetch), ортогонально display_limit (риск №5). Граница '>' без off-by-one:
            # 500 → форматтер ниже, 501 → файл. conn УЖЕ открыт и запрос УЖЕ прошёл guard записи в начале
            # функции — переиспользуем его для COPY (риск №2: без ВТОРОГО СОЕДИНЕНИЯ). NB: COPY (query)
            # исполняет query ПОВТОРНО (это не повторный fetch тех же rows) — для детерминированной
            # аналитики над статичными партициями ок; недетерминированный query даст в файле иной срез.
            if len(rows) > AUTO_EXPORT_THRESHOLD:
                auto_path = _auto_export_path()
                # mkdir на месте записи (AC #7): резолвер пути чистый, каталог создаёт писатель.
                get_results_dir().mkdir(parents=True, exist_ok=True)
                exported = _run_copy_export(conn, query, auto_path, ".csv")
                return f"Результат велик ({len(rows)} строк). {exported}"

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


# --- Сервисный слой 3.2: валидация имени, COPY-экспорт, схема, выборка -------------------


def _validate_table_name(name: str) -> str | None:
    """Санитизировать имя таблицы regex'ом ``^[A-Za-z0-9_]+$`` → имя или ``None`` (слой 1, AC #4).

    Первый из двух слоёв (второй — :func:`_check_table_exists` против реальных объектов БД).
    Отсекает инъекцию/спецсимволы/пробелы в идентификаторе ДО любого обращения к БД:
    ``"visits; DROP TABLE …"`` / ``"a b"`` / ``"a'"`` не матчатся → ``None``.
    """
    candidate = name.strip()
    if _VALID_TABLE_NAME.match(candidate):
        return candidate
    return None


def _invalid_table_name_msg(raw: str) -> str:
    """Понятный отказ на невалидное имя таблицы (слой 1 не пройден)."""
    return (
        f"Недопустимое имя таблицы {raw!r}: разрешены только латинские буквы, цифры и "
        "подчёркивание (без пробелов и спецсимволов). Список объектов — команда `--tables`."
    )


def _check_table_exists(name: str) -> str | None:
    """Текст ошибки, если таблицы ``name`` нет среди реальных объектов БД, иначе ``None`` (AC #4).

    Слой 2 валидации (слой 1 — :func:`_validate_table_name` regex'ом): сверка с
    ``information_schema.tables`` рабочего слоя. Открывает СВОЁ read-only соединение (двойное
    обращение к БД — existence-check + сам запрос — приемлемо для «одного оператора», не
    усложняем переиспользованием conn). Несуществующее имя → not-found СО СПИСКОМ известных (из
    того же запроса), не сырой DuckDB-error. До первой выгрузки (нет ``gdau.duckdb``) →
    дружелюбная подсказка про ``gdau-logs update`` (AC #8, паритет с :func:`execute_query`).
    """
    try:
        with DatabaseManager.connection(read_only=True) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
    except RuntimeError as exc:
        # AC #8: БД ещё не создана → понятный текст «… gdau-logs update», не сырой IOException.
        return str(exc)
    except duckdb.Error as exc:
        return _format_sql_error(exc, "")

    known = [str(r[0]) for r in rows]
    if name not in known:
        known_list = ", ".join(f"`{t}`" for t in known) if known else "(объектов нет)"
        return (
            f"Таблица `{name}` не найдена. Известные объекты рабочего слоя: {known_list}. "
            "Список — команда `--tables`."
        )
    return None


def _run_copy_export(
    conn: duckdb.DuckDBPyConnection, sql: str, output_path: Path, ext: str
) -> str:
    """Записать результат ``sql`` в ``output_path`` через ``COPY (…) TO`` и вернуть статус-текст.

    Единый путь записи файла-результата для обоих сценариев (риск №2): авто-экспорт зовёт его с
    **уже открытым** read-only ``conn`` (без второго соединения; сам ``query`` при этом исполняется
    повторно внутри ``COPY (query)`` — это не повторный fetch уже полученных строк), ``--export`` —
    со своим. Это **развязка от statement-guard** (риск №1): ``COPY`` строит СЕРВЕР с контролируемым
    путём под ``data/results/`` (валидирован AC #5/#6), а НЕ сырой ввод агента — поэтому он
    легитимно пишет файл-результат (под ``read_only=True`` ``COPY … TO`` пишет файл, эмпирика 3.1),
    при этом ``gdau.duckdb`` не мутируется и ``.writer.lock`` не берётся. Формат COPY — по
    расширению; путь в SQL-литерале экранируется удвоением ``'``.
    """
    safe_path = output_path.as_posix().replace("'", "''")
    if ext == ".parquet":
        copy_options = "(FORMAT PARQUET)"
    elif ext == ".json":
        copy_options = "(FORMAT JSON, ARRAY true)"
    else:  # ".csv"
        copy_options = "(HEADER, DELIMITER ',')"

    conn.execute(f"COPY ({sql}) TO '{safe_path}' {copy_options}")
    # Число записанных строк — пере-чтением файла-результата (форма directaiq); guard None у fetchone().
    count_row = conn.execute(f"SELECT COUNT(*) FROM '{safe_path}'").fetchone()
    row_count = int(count_row[0]) if count_row is not None else 0
    return f"Экспортировано {row_count} строк в файл `{output_path.name}` (data/results/)."


def _auto_export_path() -> Path:
    """Путь авто-экспорта с таймстамп-именем: ``data/results/auto_export_{YYYYMMDD_HHMMSS}.csv``.

    Серверное таймстамп-имя исключает молчаливый клоббер (AC #6) — коллизия возможна лишь при двух
    экспортах в одну секунду; на этот случай добавляем микросекунды. Каталог создаёт вызывающий
    (резолвер :func:`scripts.utils.paths.get_results_dir` чистый, без ``mkdir``).
    """
    results_dir = get_results_dir()
    now = datetime.now()
    candidate = results_dir / f"auto_export_{now.strftime('%Y%m%d_%H%M%S')}.csv"
    if candidate.exists():
        candidate = results_dir / f"auto_export_{now.strftime('%Y%m%d_%H%M%S_%f')}.csv"
    return candidate


def _export_query(sql: str, filename: str) -> str:
    """Экспортировать результат read-SELECT в файл под ``data/results/`` (AC #5/#6/#7, риск №1/№4).

    Дисциплина (строже directaiq): (1) внутренний SQL обязан пройти read-only guard 3.1 —
    ``--export "DROP TABLE visits" x.csv`` отклоняется ДО построения ``COPY`` (риск №1,
    defense-in-depth); (2) расширение ∈ {csv,parquet,json}, иначе отказ (НЕ до-приписывать ``.csv``,
    AC #6); (3) путь принудительно под ``data/results/`` — ``(dir / filename).resolve()`` +
    ``is_relative_to``, выход за пределы (абсолютный/``..``) → отказ (AC #5); (4) существующий файл →
    отказ, без молчаливого клоббера (AC #6); (5) ``mkdir`` каталога на месте записи (AC #7); затем
    свой read-only conn → :func:`_run_copy_export`. Ошибки ловятся ВНУТРИ и возвращаются строкой
    (сервер жив, риск №6).
    """
    # Риск №1: внутренний SELECT экспорта прогоняем через guard записи (COPY-обёртку строит сервер,
    # но вложенный пользовательский SQL обязан быть read-only — иначе экспорт стал бы каналом записи).
    rejection = _reject_if_not_readonly(sql)
    if rejection is not None:
        return rejection

    ext = Path(filename).suffix.lower()
    if ext not in _EXPORT_EXTENSIONS:
        shown = ext if ext else "(без расширения)"
        return (
            f"Недопустимое расширение файла экспорта: {shown}. Разрешены только "
            ".csv / .parquet / .json — укажи имя файла с одним из них."
        )

    try:
        results_dir = get_results_dir()
        results_root = results_dir.resolve()
        output_path = (results_dir / filename).resolve()
        # AC #5: абсолютный путь / '..'-traversal уводят за пределы data/results/ → отказ.
        if not output_path.is_relative_to(results_root):
            return (
                "Экспорт разрешён только внутрь data/results/: путь выходит за пределы "
                "каталога результатов (абсолютный путь или '..' запрещены), файл не записан."
            )
        # Подкаталоги не поддерживаем: файл — строго прямой потомок data/results/. Иначе COPY упал бы
        # сырым IOException (родитель не создан) с утечкой абсолютного пути хранилища → дружелюбный отказ.
        if output_path.parent != results_root:
            return (
                "Имя файла экспорта не должно содержать подкаталогов — укажи простое имя "
                "(результат кладётся прямо в data/results/), файл не записан."
            )
        # AC #6: молчаливый клоббер запрещён (в отличие от directaiq) — отказ с предложением имени.
        if output_path.exists():
            return (
                f"Файл `{output_path.name}` уже существует в data/results/ — выбери другое "
                "имя; существующий файл не перезаписывается."
            )
        results_dir.mkdir(parents=True, exist_ok=True)  # AC #7: каталог создаётся на месте записи
        with DatabaseManager.connection(read_only=True) as conn:
            return _run_copy_export(conn, sql, output_path, ext)
    except RuntimeError as exc:
        # AC #8: до первой выгрузки (нет gdau.duckdb) → дружелюбный текст, не «**Error:** RuntimeError».
        return str(exc)
    except duckdb.Error as exc:
        return _format_sql_error(exc, sql)
    except Exception as exc:
        # Риск №6: ни ValueError битого корня, ни OSError ФС наружу не выпускаем — сервер жив.
        return f"**Error:** {type(exc).__name__}: {exc!s}"


def _handle_schema(table_name: str, output_format: str, limit: int) -> str:
    """Схема одной таблицы — колонки/типы + семантика колонок ИЗ КАТАЛОГА (3.3, AC #2/#7/#8, риск №4).

    Имя ``table_name`` уже прошло :func:`_validate_table_name` (``^[A-Za-z0-9_]+$``) и проверку
    существования. SQL читает только ``column_name``/``data_type`` из ``information_schema`` с
    квотированным строковым ЛИТЕРАЛОМ имени (regex + удвоение ``'`` → инъекция невозможна).

    3.3 обогатил plain-схему (3.2 отдавала ``column_name, data_type``) колонкой ``semantics``:
    для источника (``visits``/``hits`` ∈ :data:`VALID_SOURCES`) семантика — ``description``
    каталога (:meth:`Catalog.descriptions`), для прочих объектов (``load_state``) семантики нет
    (``None``). **Семантика приклеивается в Python, а НЕ встраивается в SQL-литерал** (как в
    :func:`_handle_context`): описания каталога — свободный текст с ``;``
    (``screen_format``/``events_product_type``/``device_category``), а встроенный в SQL ``;`` ловил
    guard мульти-стейтмента (:func:`_reject_if_not_readonly`) и рубил всю команду ложным отказом
    «несколько стейтментов» — на реальном каталоге ``--schema visits``/``--schema hits`` вообще не
    работали (поймано обкаткой 2026-05-26; offline-фикстура ``_catalog()`` была без ``;``).
    Пустое/отсутствующее описание → ``None`` (AC #8, паритет со старым ``ELSE NULL``). Фильтр
    ``table_schema = 'main'`` сохранён из 3.2 (одноимённый объект в ``temp``/``pg_catalog`` задвоил
    бы строки). Битый/недоступный каталог → понятная ошибка строкой (AC #7, риск №6 — сервер жив).
    """
    try:
        catalog = load_catalog()
    except ValueError as exc:
        # AC #7: каталог недоступен/битый (или битый GDAU_DATA_ROOT) → понятная ошибка строкой,
        # сервер жив; НЕ отдаём полу-схему без семантики.
        return f"Каталог схемы недоступен: {exc}"

    # Семантика — только для источников каталога; для прочих объектов (load_state) описаний нет.
    descriptions = catalog.descriptions(table_name) if table_name in VALID_SOURCES else {}

    safe_literal = table_name.replace("'", "''")
    sql = (
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema = 'main' AND table_name = '{safe_literal}' "
        "ORDER BY ordinal_position"
    )

    fmt = output_format if output_format in _SUPPORTED_FORMATS else "json"
    display_limit = _clamp_limit(limit)
    try:
        # Свой read-only conn (как _handle_context/_check_table_exists): SQL фиксированный и
        # server-built (только information_schema), guard записи здесь не нужен.
        with DatabaseManager.connection(read_only=True) as conn:
            rows = _execute_with_timeout(conn, sql, STATEMENT_TIMEOUT_S)
    except RuntimeError as exc:
        # AC #8: БД ещё не создана → понятный текст «… gdau-logs update», не сырой IOException.
        return str(exc)
    except duckdb.Error as exc:
        return _format_sql_error(exc, sql)

    # Семантику приклеиваем в Python, НЕ в SQL: описание — свободный текст с ';' иначе попало бы в
    # SQL-литерал и было бы зарублено guard'ом мульти-стейтмента. Пустое/отсутствующее описание →
    # None (паритет со старым CASE … ELSE NULL: несопоставленная/пустая колонка = unknown, AC #8).
    columns = ["column_name", "data_type", "semantics"]
    enriched: list[tuple[Any, ...]] = [
        (str(col_name), str(data_type), descriptions.get(str(col_name)) or None)
        for col_name, data_type in rows
    ]

    if fmt == "json":
        return format_result_json(columns, enriched, display_limit)
    if fmt == "csv":
        return format_result_csv(columns, enriched, display_limit)
    return format_result_markdown(columns, enriched, display_limit)


def _handle_sample(cleaned: str, output_format: str, limit: int) -> str:
    """Роутинг ``--sample TABLE [N]``: валидация имени + существования, клампинг ``N`` (AC #1/#4/#10).

    ``N`` ≥ 1 всегда: отсутствует/нечисловой → :data:`DEFAULT_SAMPLE`; ``'0'`` → ``max(1, 0) = 1``
    (клампинг; отличие от directaiq, где ``LIMIT 0`` давал пустой результат). Проверка числа —
    ``str.isdecimal()``, а НЕ ``isdigit()``: ``isdigit()`` истинно для юникод-надстрочных/дробных
    (``²``/``⁵``), на которых ``int()`` бросает ``ValueError`` мимо try/except → падение инструмента;
    ``isdecimal()`` ⊆ принимаемого ``int()``. Имя в SQL — квотированный идентификатор (``"name"``).
    """
    parts = cleaned.split()
    # parts[0] == "--sample"; ожидаем имя в parts[1], опциональный N в parts[2].
    if len(parts) < 2:
        return "Использование: --sample TABLE [N] — укажи имя таблицы (N — число строк, ≥1)."

    name = _validate_table_name(parts[1])
    if name is None:
        return _invalid_table_name_msg(parts[1])
    existence_error = _check_table_exists(name)
    if existence_error is not None:
        return existence_error

    sample_n = DEFAULT_SAMPLE
    if len(parts) >= 3 and parts[2].isdecimal():
        # isdecimal() (НЕ isdigit()): isdigit() пропускает юникод-надстрочные/дробные цифры (`²`,`⁵`),
        # на которых int() бросает ValueError мимо try/except. isdecimal() ⊆ принимаемого int().
        # '0' → max(1, 0) = 1 (а не пустой LIMIT 0); отрицательное/нечисловое → дефолт. N всегда ≥1 (AC #10).
        sample_n = max(1, int(parts[2]))

    safe_name = name.replace('"', '""')
    sql = f'SELECT * FROM "{safe_name}" LIMIT {sample_n}'
    return execute_query(sql, output_format, limit)


def _handle_export(cleaned: str) -> str:
    """Роутинг ``--export "SELECT …" file.{csv|parquet|json}``: разбор аргументов + :func:`_export_query`.

    ``shlex.split`` (``posix=True`` по умолчанию) кросс-платформенно снимает кавычки вокруг SQL.
    Меньше двух аргументов → usage-подсказка; битый разбор кавычек (:class:`ValueError`) → понятная
    ошибка. Сами проверки безопасности (guard внутреннего SQL/расширение/traversal/клоббер) — в
    :func:`_export_query`.
    """
    remainder = cleaned[len("--export ") :]
    try:
        args = shlex.split(remainder)
    except ValueError as exc:
        return (
            f"Не удалось разобрать аргументы --export ({exc}). Формат: "
            '--export "SELECT …" file.{csv|parquet|json} (запрос в кавычках).'
        )
    if len(args) < 2:
        return (
            'Использование: --export "SELECT …" file.{csv|parquet|json} — '
            "укажи SQL-запрос в кавычках и имя файла."
        )
    return _export_query(args[0], args[1])


# --- Контекст рабочего слоя 3.3 (--context, AC #1/#2/#7/#8/#9) ---------------------------


def _first_date_like_column(columns: list[tuple[str, str]]) -> str | None:
    """Первая date-подобная колонка по порядку (тип ``DATE``/``TIMESTAMP`` или имя ``date``).

    ``columns`` — ``[(имя, тип), …]`` в порядке ``ordinal_position``. Для ``visits``/``hits``
    это ``date`` (``DATE``); для ``load_state`` — тоже ``date``. По ней считается диапазон дат
    (``MIN``/``MAX``) в :func:`_handle_context`. Нет date-подобной колонки → ``None`` (диапазона нет).
    """
    for name, col_type in columns:
        if col_type.upper().startswith(("DATE", "TIMESTAMP")) or name.lower() == "date":
            return name
    return None


def _handle_context() -> str:
    """Авто-контекст рабочего слоя одним вызовом → markdown-сводка (3.3, AC #1/#2/#7/#8/#9).

    По каждому объекту main-схемы (view'ы ``visits``/``hits`` + мета-таблица ``load_state``):
    колонки с типом, ``row_count`` (``COUNT(*)``) и диапазон дат (``MIN``/``MAX`` первой
    date-подобной колонки), плюс семантика колонок из **каталога** (``description``) для
    источников ``visits``/``hits``. Зовётся НАПРЯМУЮ из :func:`handle_query` (не через
    :func:`execute_query`), поэтому все ошибки ловятся ВНУТРИ и возвращаются строкой (риск №6:
    голое исключение порвало бы MCP-сессию) — паритет с :func:`execute_query`.

    Отличия от directaiq ``_handle_context`` (осознанные):
    - **COUNT считается и для view'ов.** directaiq пропускал ``COUNT(*)`` для VIEW (тяжёлый
      парсящий view → timeout); наши view'ы — тонкий ``TRY_CAST`` над parquet-glob (COUNT дёшев
      по метаданным parquet), а AC #1 ТРЕБУЕТ row counts для ``visits``/``hits``.
    - **Per-object SELECT'ы, не один ``UNION ALL``.** Объектов мало (``visits``/``hits``/
      ``load_state``) → читаемее два служебных SELECT'а на объект, чем сборка ``UNION ALL``.
    - **Семантика — из каталога, не из денег/НДС**, и НЕТ секций ``## Money/Goal/Config``
      (директовая разметка, у геймдева её нет — риск №1).

    Read-only (риск №6): своя ``read_only=True``-conn, ``.writer.lock`` не берётся, БД не
    мутируется, файлов не пишет. ``--context`` собирает текст сам — ``format`` для него не
    осмыслен (курированный markdown, как directaiq).
    """
    try:
        # Каталог СНАЧАЛА (риск №5/AC #7): битый/недоступный → выходим строкой ДО сбора контекста,
        # не отдаём полу-сводку с пустой семантикой.
        catalog = load_catalog()

        with DatabaseManager.connection(read_only=True) as conn:
            # Объекты/колонки/типы main-схемы в порядке ordinal_position (квотирование имён ниже).
            schema_rows = conn.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'main' "
                "ORDER BY table_name, ordinal_position"
            ).fetchall()

            if not schema_rows:
                return (
                    "_Рабочий слой пуст: в базе нет ни одной таблицы/view. "
                    "Запусти `gdau-logs update` (приём данных) или `gdau-init` (разворачивание)._"
                )

            # Группируем колонки по объекту, сохраняя порядок (ordinal_position из ORDER BY).
            objects: dict[str, list[tuple[str, str]]] = {}
            for table_name, col_name, col_type in schema_rows:
                objects.setdefault(str(table_name), []).append(
                    (str(col_name), str(col_type))
                )

            sections: list[str] = ["# Контекст рабочего слоя\n"]
            for obj_name in sorted(objects):
                columns = objects[obj_name]
                esc_name = obj_name.replace('"', '""')

                # row_count: COUNT(*) и для view'ов (тонкий TRY_CAST → дёшев; AC #1). Пустой
                # источник (view WHERE false, 2.6) → 0 без обращения к parquet (AC #9).
                count_row = conn.execute(f'SELECT COUNT(*) FROM "{esc_name}"').fetchone()
                row_count = int(count_row[0]) if count_row is not None else 0

                # Диапазон дат по первой date-подобной колонке. CAST AS VARCHAR — иначе DATE/
                # TIMESTAMP пришли бы Python-объектами date и в markdown попал бы repr, не '2026-05-20'.
                date_info = ""
                date_col = _first_date_like_column(columns)
                if date_col is not None:
                    esc_dcol = date_col.replace('"', '""')
                    range_row = conn.execute(
                        f'SELECT CAST(MIN("{esc_dcol}") AS VARCHAR), '
                        f'CAST(MAX("{esc_dcol}") AS VARCHAR) FROM "{esc_name}"'
                    ).fetchone()
                    # Пустой объект → MIN/MAX = NULL → диапазона нет (AC #9, без None-разыменования).
                    if range_row is not None and range_row[0] is not None:
                        date_info = f", {date_col}: {range_row[0]} … {range_row[1]}"

                # Семантика — из каталога только для источников; для load_state и пр. — нет (AC #8).
                semantics = (
                    catalog.descriptions(obj_name) if obj_name in VALID_SOURCES else {}
                )

                sections.append(f"### {obj_name} ({row_count} строк{date_info})")
                for col_name, col_type in columns:
                    # AC #8: dict.get (НЕ индексация) — несопоставленная колонка → None, без KeyError.
                    sem = semantics.get(col_name)
                    if sem:
                        # Описание каталога — свободный текст: перевод строки разорвал бы пункт
                        # markdown-списка на несколько физических строк (паритет с _md_escape для
                        # ячеек таблицы). '|' в пункте списка безвреден → НЕ экранируем (иначе в
                        # выводе появился бы лишний '\').
                        safe_sem = sem.replace("\r", " ").replace("\n", " ")
                        sections.append(f"- {col_name}: {col_type} — {safe_sem}")
                    else:
                        # Колонка источника без описания в каталоге = рассинхрон view↔каталог
                        # (AC #8) → WARNING. Для НЕ-источников (load_state) семантики нет штатно
                        # — не шумим.
                        if obj_name in VALID_SOURCES:
                            logger.warning(
                                "Колонка %r объекта %r не сопоставлена каталогу "
                                "(пустое/отсутствующее description) — семантика unknown",
                                col_name,
                                obj_name,
                            )
                        sections.append(f"- {col_name}: {col_type} — —")
                sections.append("")

            return "\n".join(sections)

    except RuntimeError as exc:
        # До первой выгрузки DatabaseManager (2.1) бросает RuntimeError «… gdau-logs update»
        # ДО connect → дружелюбный текст строкой (паритет execute_query AC #8, риск №6).
        return str(exc)
    except ValueError as exc:
        # Один класс: битый/недоступный каталог load_catalog (AC #7) И битый/незаданный
        # GDAU_DATA_ROOT (paths.get_storage_root). НЕ делаем except только под каталог.
        return f"Каталог схемы недоступен: {exc}"
    except duckdb.Error as exc:
        return _format_sql_error(exc, "--context")
    except Exception as exc:
        # Риск №6: голых исключений из инструмента наружу не выпускаем — иначе MCP-сессия рвётся.
        return f"**Error:** {type(exc).__name__}: {exc!s}"


def handle_query(
    query: str, output_format: str = "json", limit: int = DEFAULT_LIMIT
) -> str:
    """Входная точка инструмента: пустой запрос → подсказка; спец-команда → роутинг; иначе SQL.

    Роутинг спец-команд (``--context``/``--tables``/``--schema [TABLE]``/``--sample TABLE [N]``/
    ``--export``) идёт ПЕРЕД fall-through на произвольный SQL (:func:`execute_query`). Матчим по
    ``cleaned`` (``strip`` сделан здесь — повторно не делаем). ``--context`` (3.3) собирает
    markdown-сводку рабочего слоя сам, мимо ``execute_query``. Любой иной текст — обычный read-SQL
    к view'ам ``visits``/``hits``.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return (
            "Пустой запрос. Пришли SQL к view'ам `visits`/`hits`, "
            "например: SELECT count(*) FROM visits."
        )

    # --- Роутинг сервисных команд (AC #1) — точные команды раньше префиксных ---
    if cleaned == "--context":
        # 3.3: авто-контекст рабочего слоя (объекты/типы/row counts/диапазоны дат + семантика
        # каталога). Собирает markdown сам (format игнорируется — курированный текст, риск №6).
        return _handle_context()
    if cleaned == "--tables":
        # Список таблиц/view рабочего слоя из information_schema (имена snake_case).
        return execute_query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name",
            output_format,
            limit,
        )
    if cleaned == "--schema":
        # Схема ВСЕХ объектов: таблица/колонка/тип (plain, без семантики — риск №6, 3.3 обогатит).
        return execute_query(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position",
            output_format,
            limit,
        )
    if cleaned.startswith("--schema "):
        raw_name = cleaned[len("--schema ") :].strip()
        name = _validate_table_name(raw_name)
        if name is None:
            return _invalid_table_name_msg(raw_name)
        existence_error = _check_table_exists(name)
        if existence_error is not None:
            return existence_error
        return _handle_schema(name, output_format, limit)
    if cleaned.startswith("--sample "):
        return _handle_sample(cleaned, output_format, limit)
    if cleaned.startswith("--export "):
        return _handle_export(cleaned)

    # Fall-through: обычный произвольный read-SQL (как в 3.1) — guard внутри execute_query.
    return execute_query(cleaned, output_format, limit)
