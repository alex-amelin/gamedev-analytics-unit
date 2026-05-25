"""MCP-сервер ``gdau`` — единый инструмент ``duckdb_query`` поверх рабочего слоя (Epic 3).

Поднимается на официальном ``mcp`` SDK (``mcp.server.fastmcp.FastMCP``, НЕ отдельный
``fastmcp`` 3.x) и регистрируется через ``.mcp.json`` запуском
``uv run python -m scripts.mcp.gdau_mcp_server``. Канал **только чтения**: инструмент —
тонкая обёртка над :func:`scripts.mcp.tools.core.handle_query` (read-only-соединение к
``gdau.duckdb`` 2.1, view'ы ``visits``/``hits`` 2.6); запись невозможна, ``.writer.lock`` 2.5
не берётся.

# vendored from directaiq @ scripts/mcp/directaiq_mcp_server.py, seam: gdau-брендинг + .env-bootstrap;
# trimmed: config_manager (utils/common.get_mcp_output_dir) + audit-лог (_save_audit_log) → 3.2.
Развязка швов под наш репозиторий (AC #5): идентификаторы переименованы под gdau
(``FastMCP("gdau_mcp")``), нет завязки на ``config_manager``/``auth_manager``; audit-лог и
сервисные команды — следующие истории (3.2). ``readOnlyHint=True`` (в directaiq был ``False``
из-за ``--export``; в 3.1 экспорта нет → канал чисто читающий).
"""

from __future__ import annotations

# .env-bootstrap ДО импортов scripts.* (риск №3): Claude Code сам .env не грузит
# ([[mcp-env-delivery]]). Нужен только GDAU_DATA_ROOT (резолюция gdau.duckdb) — креды Метрики
# read-каналу не нужны. find_dotenv(usecwd=True): walk-up от КАТАЛОГА ЗАПУСКА (cwd MCP-процесса =
# каталог хранилища с .env), а НЕ от каталога модуля (в wheel это site-packages, мимо .env
# оператора — [[dotenv-usecwd-gotcha]]). override=False: реальное окружение приоритетнее файла.
from dotenv import find_dotenv, load_dotenv

_dotenv_path = find_dotenv(usecwd=True)
if _dotenv_path:
    load_dotenv(_dotenv_path, override=False)

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from scripts.mcp.tools.core import DEFAULT_LIMIT, MAX_LIMIT, handle_query

mcp = FastMCP("gdau_mcp")


@mcp.tool(
    name="duckdb_query",
    annotations=ToolAnnotations(
        title="DuckDB Query",
        readOnlyHint=True,  # в 3.1 экспорта/записи нет → канал чисто читающий
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def duckdb_query(
    query: Annotated[
        str,
        Field(
            description=(
                "SQL-запрос только для чтения к рабочему слою игры. Доступны view'ы "
                "`visits` и `hits` (колонки в snake_case). Запись (INSERT/UPDATE/DELETE/"
                "CREATE/DROP/COPY TO/PRAGMA/…) отклоняется — канал только читает."
            ),
            min_length=1,
        ),
    ],
    format: Annotated[
        Literal["json", "markdown", "csv"],
        Field(
            default="json",
            description="json — для обработки агентом, markdown — для показа пользователю, csv — выгрузка.",
        ),
    ] = "json",
    limit: Annotated[
        int,
        Field(
            default=DEFAULT_LIMIT,
            description=f"Лимит строк результата (1..{MAX_LIMIT}); ≤0 или пропуск → {DEFAULT_LIMIT}.",
            ge=0,
        ),
    ] = DEFAULT_LIMIT,
) -> str:
    """Выполнить SQL-запрос к данным игры и вернуть результат в выбранном формате.

    Канал **только для чтения**: соединение read-only, ``.writer.lock`` не берётся, запись
    отклоняется. Доступны типизированные view'ы ``visits`` (визиты) и ``hits`` (просмотры/
    события) с колонками в snake_case. До первой выгрузки (``gdau-logs update``) данных нет —
    инструмент вернёт понятную подсказку, а не ошибку.

    Примеры:
        - ``SELECT count(*) FROM visits``
        - ``SELECT date, count(*) FROM visits GROUP BY 1 ORDER BY 1``
        - ``DESCRIBE hits`` — список колонок и типов.
    """
    return handle_query(query, format, limit)


if __name__ == "__main__":
    mcp.run()
