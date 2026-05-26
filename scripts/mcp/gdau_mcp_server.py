"""MCP-сервер ``gdau`` — единый инструмент ``duckdb_query`` поверх рабочего слоя (Epic 3).

Поднимается на официальном ``mcp`` SDK (``mcp.server.fastmcp.FastMCP``, НЕ отдельный
``fastmcp`` 3.x) и регистрируется через ``.mcp.json`` запуском
``uv run python -m scripts.mcp.gdau_mcp_server``. Канал **только чтения**: инструмент —
тонкая обёртка над :func:`scripts.mcp.tools.core.handle_query` (read-only-соединение к
``gdau.duckdb`` 2.1, view'ы ``visits``/``hits`` 2.6); запись невозможна, ``.writer.lock`` 2.5
не берётся.

# vendored from directaiq @ scripts/mcp/directaiq_mcp_server.py, seam: gdau-брендинг + .env-bootstrap
# + audit из нашего paths.py + WARNING вместо except:pass;
# trimmed: config_manager (utils/common.get_config) + goal-плейсхолдеры/Direct-семантика НЕ
# переносятся принципиально (никогда не вендорились — у геймдева Директа нет, история 3.3 риск №1).
Развязка швов под наш репозиторий (AC #5): идентификаторы переименованы под gdau
(``FastMCP("gdau_mcp")``), нет завязки на ``config_manager``/``auth_manager``. 3.2 нарастил
сервисные команды (в :mod:`scripts.mcp.tools.core`) и **audit-лог** каждого вызова инструмента
(:func:`_save_audit_log` → ``data/mcp_output/``; сбой → WARNING, не ``except: pass`` directaiq —
риск №7/AC #9). ``get_mcp_output_dir`` берётся из нашего ``scripts.utils.paths`` (НЕ из
directaiq-``utils/common``, его в репо нет). ``readOnlyHint=False`` (3.2: ``--export``/авто-экспорт
пишут файлы-результаты в ``data/results/`` — как directaiq; в 3.1 экспорта не было → было ``True``).
3.3 (ФИНАЛ Epic 3) добавил команду ``--context`` (авто-обзор рабочего слоя) и колонку
``semantics`` в ``--schema TABLE`` — семантика берётся из нашего каталога схемы (FR-18); ``Field``/
докстринг инструмента это рекламируют. ``readOnlyHint``/``_save_audit_log`` 3.2 не тронуты.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

# .env-bootstrap ДО импортов scripts.* (риск №3): Claude Code сам .env не грузит
# ([[mcp-env-delivery]]). Нужен только GDAU_DATA_ROOT (резолюция gdau.duckdb) — креды Метрики
# read-каналу не нужны. find_dotenv(usecwd=True): walk-up от КАТАЛОГА ЗАПУСКА (cwd MCP-процесса =
# каталог хранилища с .env), а НЕ от каталога модуля (в wheel это site-packages, мимо .env
# оператора — [[dotenv-usecwd-gotcha]]). override=False: реальное окружение приоритетнее файла.
from dotenv import find_dotenv, load_dotenv

_dotenv_path = find_dotenv(usecwd=True)
if _dotenv_path:
    load_dotenv(_dotenv_path, override=False)

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from scripts.mcp.tools.core import DEFAULT_LIMIT, MAX_LIMIT, handle_query
from scripts.utils.paths import get_mcp_output_dir

logger = logging.getLogger(__name__)

mcp = FastMCP("gdau_mcp")


def _save_audit_log(tool_name: str, parameters: dict[str, Any], result: str) -> None:
    """Записать audit-конверт вызова инструмента в ``data/mcp_output/`` (AC #3/#9, риск №7).

    Конверт ``{tool, timestamp, parameters, result}`` в JSON-файл с таймстампом в имени
    (``{tool}_{YYYY-MM-DD_HHMMSS_ffffff}.json`` — микросекунды, чтобы два вызова в одну секунду
    не перезаписали запись друг друга: журналируется КАЖДЫЙ вызов, AC #3). ``result`` пробуем распарсить как JSON (формат
    ``json`` результата) → кладём объектом, иначе строкой как есть (markdown/csv/сообщение).
    Каталог создаётся на месте записи (``mkdir``, AC #7) — резолвер ``get_mcp_output_dir``
    чистый. **Сбой записи** (нет каталога/диск полон/сериализация) → ``logger.warning`` (НЕ
    ``except: pass`` directaiq и НЕ сырой проброс, риск №7): результат уже вычислен ДО аудита
    → его возврат от сбоя логирования не зависит, сам read-запрос не валится (AC #9).
    """
    try:
        mcp_dir = get_mcp_output_dir()
        mcp_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        filename = f"{tool_name}_{now.strftime('%Y-%m-%d_%H%M%S_%f')}.json"
        try:
            result_payload: Any = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            result_payload = result
        envelope = {
            "tool": tool_name,
            "timestamp": now.isoformat(),
            "parameters": parameters,
            "result": result_payload,
        }
        with (mcp_dir / filename).open("w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        # Риск №7/AC #9: аудит — диагностика, а не часть контракта чтения. Любой сбой записи
        # глушим в WARNING (не наружу, не молча) — результат агенту уже посчитан и будет отдан.
        logger.warning("Не удалось записать audit-лог вызова %s: %s", tool_name, exc)


@mcp.tool(
    name="duckdb_query",
    annotations=ToolAnnotations(
        title="DuckDB Query",
        # 3.2: --export/авто-экспорт пишут файлы-результаты в data/results/ (как directaiq) →
        # канал уже не «чисто читающий». БД при этом не мутируется (destructiveHint=False).
        readOnlyHint=False,
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
                "SQL-запрос ИЛИ сервисная команда к рабочему слою игры (только чтение БД). "
                "Доступны view'ы `visits` и `hits` (колонки в snake_case). Сервисные команды: "
                "`--context` — авто-обзор рабочего слоя (объекты, колонки/типы, число строк, "
                "диапазон дат + что означает каждое поле — семантика из каталога схемы); "
                "`--tables` — список таблиц/view; `--schema` — схема всех объектов; "
                "`--schema TABLE` — колонки/типы одной таблицы (+ колонка `semantics` — описание "
                'поля из каталога); `--sample TABLE [N]` — N строк-примеров (по умолчанию 5); '
                '`--export "SELECT …" file.{csv|parquet|json}` — результат SELECT в файл под '
                "data/results/. Большой результат (>500 строк) авто-экспортируется в data/results/ "
                "вместо переполнения ответа. Запись в БД "
                "(INSERT/UPDATE/DELETE/CREATE/DROP/COPY TO/PRAGMA/…) отклоняется."
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
    """Выполнить SQL-запрос или сервисную команду к данным игры и вернуть результат.

    Канал читает БД **только** на чтение (соединение read-only, ``.writer.lock`` не берётся,
    запись в БД отклоняется), но **пишет файлы-результаты** в ``data/results/`` (``--export`` и
    авто-экспорт результатов >500 строк). Доступны типизированные view'ы ``visits`` (визиты) и
    ``hits`` (просмотры/события) с колонками в snake_case. До первой выгрузки (``gdau-logs
    update``) данных нет — инструмент вернёт понятную подсказку, а не ошибку. Каждый вызов
    пишется в audit-лог ``data/mcp_output/``.

    Примеры:
        - ``SELECT count(*) FROM visits``
        - ``--context`` — обзор всего рабочего слоя (объекты, колонки/типы, сколько строк, за
          какие даты + что означает каждое поле).
        - ``--tables`` — какие таблицы/view доступны.
        - ``--schema visits`` — колонки, типы и семантика (описание из каталога) view ``visits``.
        - ``--sample hits 3`` — три строки-примера из ``hits``.
        - ``--export "SELECT * FROM visits" visits.parquet`` — выгрузка в файл.
    """
    result = handle_query(query, format, limit)
    # Audit-лог ПОСЛЕ получения результата (AC #3): обёртка инструмента — единственное место,
    # где видна и сигнатура вызова, и его результат. Сбой аудита не валит чтение (риск №7/AC #9).
    _save_audit_log(
        "duckdb_query",
        {"query": query, "format": format, "limit": limit},
        result,
    )
    return result


if __name__ == "__main__":
    mcp.run()
