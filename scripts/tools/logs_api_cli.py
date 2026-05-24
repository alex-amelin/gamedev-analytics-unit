"""CLI-примитивы жизненного цикла Logs API — console-команда ``gdau-logs``.

AI-native канал ДЕЙСТВИЙ юнита: тонкий неинтерактивный argparse-CLI, который
выставляет жизненный цикл Logs API скриптуемыми подкомандами (``create`` /
``evaluate`` / ``status`` / ``download`` / ``clean`` / ``list`` + справочная
``info``). Агент-оператор собирает из этих примитивов ad-hoc обращения к реальному
API; результат печатается человекочитаемым текстом — его одинаково понимают и
LLM-агент, и человек (решение Шефа: без машинных форматов ``--format``).

Это **склейка** уже готовых примитивов историй 1.2–1.5, а НЕ их повтор:

- поля выгрузки берутся из каталога-SSOT (1.5: ``load_catalog().metrica_fields``),
  а не передаются флагом ``--fields`` (применение FR-2);
- ``date2`` клампится на «вчера по МСК» ДО сети (1.4: ``clamp_date_range``);
- креды читает env-ридер и они инъектируются в клиент (1.2 → 1.3), флага
  ``--counter-id`` нет — единственный источник кредов это окружение/``.env``;
- сетевая дисциплина (retry/rate-limit/квота) живёт ТОЛЬКО в ``MetricaClient``
  (1.3) — CLI её не реализует заново, отказ API всплывает как ``RuntimeError``.

Форма (класс + ``_create_parser`` + per-command ``_handle_*``) намеренно повторяет
directaiq-``logs_api_cli.py``, но БЕЗ его инфраструктуры (``BaseScript`` /
``AuthManager`` / ``get_logger`` / ``setup_paths`` / ``config_manager``) — её мы
сознательно не тащим (NFR-6, простота-первой).

**Граница скоупа:** здесь только примитивы поверх ``MetricaClient``. Высокоуровневый
приём за диапазон (``update``/``load``), оркестратор p81, атомарная запись в Parquet
под локом — это Epic 2 (story 2.9/2.7), НЕ здесь. ``download`` — ad-hoc примитив:
пишет сырые ``.tsv`` туда, куда указал оператор (``--output``; по умолчанию — текущий
каталог), без записи в dev-репо.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from scripts.utils.catalog import load_catalog
from scripts.utils.dates import clamp_date_range, format_date, parse_date
from scripts.utils.env_reader import read_metrica_credentials
from scripts.utils.metrica_client import MetricaClient

__all__ = ["LogsApiCLI", "main"]

logger = logging.getLogger(__name__)


class LogsApiCLI:
    """Тонкий CLI жизненного цикла Logs API (форма directaiq, без его инфры).

    Состояния не держит: клиент строится per-command в :meth:`_build_client`
    (после разбора аргументов и валидации дат, не в парсере). Каждый ``_handle_*``
    сам печатает результат человекочитаемым текстом и возвращает структурный
    результат (dict/list) — для тестов и возможного переиспользования; печать в
    :func:`main` НЕ централизована.
    """

    # --- Парсер ---------------------------------------------------------------

    def _create_parser(self) -> argparse.ArgumentParser:
        """Собрать argparse-парсер со всеми подкомандами жизненного цикла (AC #1, #6).

        ``subparsers(required=True)`` закрывает AC #6: голый вызов без подкоманды →
        argparse печатает usage и поднимает ``SystemExit(2)`` (без трейсбека).
        Глобального ``--format`` нет (вывод человекочитаемый — решение Шефа).
        """
        parser = argparse.ArgumentParser(
            prog="gdau-logs",
            description=(
                "CLI-примитивы жизненного цикла Yandex Metrica Logs API.\n"
                "Поля выгрузки берутся из каталога схемы, date2 клампится на "
                "«вчера по МСК», креды — из окружения/.env.\n"
                "Полный приём за диапазон (update) — отдельная команда (Epic 2)."
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        subparsers = parser.add_subparsers(dest="command", required=True)

        # create — заказать выгрузку. Без --fields (поля из каталога, риск #1);
        # --source required choices (строже directaiq: без молчаливого дефолта, AC #6).
        create_parser = subparsers.add_parser(
            "create", help="заказать выгрузку за период (поля — из каталога)"
        )
        create_parser.add_argument("--date1", required=True, help="начальная дата YYYY-MM-DD")
        create_parser.add_argument(
            "--date2", required=True, help="конечная дата YYYY-MM-DD (клампится на «вчера по МСК»)"
        )
        create_parser.add_argument(
            "--source", required=True, choices=["visits", "hits"], help="источник: visits или hits"
        )
        create_parser.add_argument(
            "--attribution",
            default="CROSS_DEVICE_LAST_SIGNIFICANT",
            help="модель атрибуции (по умолчанию CROSS_DEVICE_LAST_SIGNIFICANT)",
        )

        # evaluate — прикинуть, потянет ли API такой заказ (поля те же, что у create).
        evaluate_parser = subparsers.add_parser(
            "evaluate", help="оценить, потянет ли API такой заказ"
        )
        evaluate_parser.add_argument("--date1", required=True, help="начальная дата YYYY-MM-DD")
        evaluate_parser.add_argument("--date2", required=True, help="конечная дата YYYY-MM-DD")
        evaluate_parser.add_argument(
            "--source", required=True, choices=["visits", "hits"], help="источник: visits или hits"
        )

        # status — статус одного запроса.
        status_parser = subparsers.add_parser("status", help="статус одного запроса")
        status_parser.add_argument("--request-id", type=int, required=True, help="ID запроса")

        # download — скачать готовые части (статус-гейт processed, no-clobber).
        download_parser = subparsers.add_parser(
            "download", help="скачать готовые части запроса"
        )
        download_parser.add_argument("--request-id", type=int, required=True, help="ID запроса")
        download_parser.add_argument(
            "--part", type=int, help="номер части (по умолчанию — все части)"
        )
        download_parser.add_argument(
            "--output", help="файл или каталог назначения (по умолчанию — текущий каталог)"
        )
        download_parser.add_argument(
            "--clean", action="store_true", help="очистить запрос после успешного скачивания"
        )

        # clean — очистить (удалить данные) запроса, освободив квоту.
        clean_parser = subparsers.add_parser(
            "clean", help="очистить (удалить) подготовленный запрос"
        )
        clean_parser.add_argument("--request-id", type=int, required=True, help="ID запроса")

        # list — список всех запросов; без аргументов.
        subparsers.add_parser("list", help="список всех запросов на выгрузку")

        # info — информация о счётчике (проверка доступа/счётчика); без аргументов.
        # Только info: counter_id из .env (discovery не нужен), goals — семантика
        # Директа, нерелевантная сессиям/хитам (NFR-6, обоснование в истории).
        subparsers.add_parser("info", help="информация о счётчике (проверка доступа)")

        return parser

    # --- Общие швы ------------------------------------------------------------

    def _build_client(self) -> MetricaClient:
        """Построить клиент на кредах env-ридера (риск #3, единственная точка).

        ``read_metrica_credentials`` падает ``ValueError`` ДО сети, если кредов нет;
        мы её здесь НЕ ловим — она всплывает в :func:`main` (AC #4, non-zero). Токен
        уходит инъекцией в клиент и живёт только в заголовке сессии (NFR-5).
        """
        creds = read_metrica_credentials()
        return MetricaClient(token=creds.token, counter_id=creds.counter_id)

    @staticmethod
    def _resolve_output(output: str | None, request_id: int) -> tuple[Path, str]:
        """Резолюция каталога и префикса имени для ``download`` (без paths.py/storage).

        ``--output`` с суффиксом → каталог = parent, префикс имени = stem (итоговые
        файлы — ``{stem}_part{n}.tsv``, НЕ буквально переданное имя: частей может быть
        несколько); без суффикса → каталог (префикс ``logs_{request_id}``); не задан →
        текущий каталог запуска (``cwd``). Реальная атомарная запись в хранилище под
        локом — это 2.7, не здесь; в dev-репо данные не пишем (пишем туда, куда указал
        оператор).
        """
        prefix = f"logs_{request_id}"
        if output:
            path = Path(output)
            if path.suffix:
                return path.parent, path.stem
            return path, prefix
        return Path.cwd(), prefix

    # --- Handlers жизненного цикла -------------------------------------------

    def _handle_create(self, args: argparse.Namespace) -> dict[str, Any]:
        """Заказать выгрузку: даты→clamp, поля из каталога, креды→клиент (AC #2, #8).

        Невалидная/инвертированная дата → ``ValueError`` из ``dates.py`` ДО построения
        клиента; отказ API (квота/невозможно) → ``RuntimeError`` из клиента (1.3) —
        обе всплывают в :func:`main` (non-zero). Retry/квоту CLI не реализует (NFR-3).
        """
        date1 = parse_date(args.date1)
        date2 = parse_date(args.date2)
        date1, date2 = clamp_date_range(date1, date2)
        fields = load_catalog().metrica_fields(args.source)

        client = self._build_client()
        response = client.create_log_request(
            date1=format_date(date1),
            date2=format_date(date2),
            fields=fields,
            source=args.source,
            attribution=args.attribution,
        )
        # create_log_request отдаёт ПОЛНЫЙ ответ; нужное — под log_request (риск #5).
        # `or response` (а не default=response): защищает и от null-значения ключа,
        # а не только от его отсутствия — иначе None.get(...) дал бы AttributeError.
        log_request: dict[str, Any] = response.get("log_request") or response

        print("Запрос на выгрузку создан.")
        print(f"Request ID: {log_request.get('request_id')}")
        print(f"Status: {log_request.get('status')}")
        print("Проверьте готовность командой: gdau-logs status --request-id <id>")
        return log_request

    def _handle_evaluate(self, args: argparse.Namespace) -> dict[str, Any]:
        """Оценить выполнимость заказа (поля те же, что отправит create) (AC #3, #8).

        Несёт смысл как пред-проверка перед ``create`` (``possible=false`` бережёт
        квоту). Авто-вызова из ``create`` не делаем (простота).
        """
        date1 = parse_date(args.date1)
        date2 = parse_date(args.date2)
        date1, date2 = clamp_date_range(date1, date2)
        fields = load_catalog().metrica_fields(args.source)

        client = self._build_client()
        response = client.evaluate_log_request(
            date1=format_date(date1),
            date2=format_date(date2),
            fields=fields,
            source=args.source,
        )
        # evaluate_log_request отдаёт ПОЛНЫЙ ответ; оценка — под log_request_evaluation.
        # `or response` защищает и от null-значения ключа (см. _handle_create).
        evaluation: dict[str, Any] = response.get("log_request_evaluation") or response

        print(f"Можно создать запрос: {evaluation.get('possible')}")
        print(f"Максимум дней за один запрос: {evaluation.get('max_possible_day_quantity')}")
        return evaluation

    def _handle_status(self, args: argparse.Namespace) -> dict[str, Any]:
        """Статус одного запроса; пустой ответ = не найден, не «успех» (AC #3, #5, #7).

        ``get_log_request`` уже извлекает внутренний dict (риск #5). Несуществующий id:
        клиент на 404 бросит ``RuntimeError``; но если API вернёт 200 с пустым
        ``log_request`` (``{}``) — fail-loud ``ValueError`` (пустой ответ НЕ выдаём за
        успех; осознанное отличие от directaiq).
        """
        client = self._build_client()
        data: dict[str, Any] = client.get_log_request(args.request_id)
        if not data:
            raise ValueError(f"Запрос {args.request_id} не найден.")

        parts = data.get("parts", [])
        print(f"Request ID: {data.get('request_id', args.request_id)}")
        print(f"Status: {data.get('status')}")
        print(f"Период: {data.get('date1')} — {data.get('date2')}")
        print(f"Частей: {len(parts)}")
        for part in parts:
            # float(... or 0): API может отдать size строкой/null — без коэрции
            # арифметика дала бы TypeError мимо except в main (трейсбек, против AC #4).
            size_mb = float(part.get("size") or 0) / (1024 * 1024)
            print(f"  часть {part.get('part_number')}: {size_mb:.2f} МБ")
        return data

    def _handle_list(self, args: argparse.Namespace) -> list[dict[str, Any]]:
        """Список всех запросов выровненной таблицей; пусто → понятная строка (AC #3, #5).

        ``get_log_requests`` уже отдаёт ``list`` (риск #5). Пустой список — валидный
        результат (нет активных запросов), а не ошибка.
        """
        client = self._build_client()
        requests_list: list[dict[str, Any]] = client.get_log_requests()
        if not requests_list:
            print("Нет активных запросов на выгрузку.")
            return requests_list

        header = f"{'ID':<10} {'Статус':<16} {'Источник':<10} {'Период':<25} Размер"
        print(header)
        print("-" * len(header))
        for req in requests_list:
            parts = req.get("parts", [])
            # float(... or 0): size может прийти строкой/null (см. _handle_status).
            size_mb = sum(float(p.get("size") or 0) for p in parts) / (1024 * 1024)
            date_range = f"{req.get('date1')} — {req.get('date2')}"
            print(
                f"{req.get('request_id')!s:<10} {req.get('status')!s:<16} "
                f"{req.get('source')!s:<10} {date_range:<25} {size_mb:.1f} МБ"
            )
        return requests_list

    def _handle_download(self, args: argparse.Namespace) -> dict[str, Any]:
        """Скачать части: статус-гейт processed, выбор части, no-clobber (AC #3, #7, #8).

        Сначала статус-гейт (не найден/не ``processed`` → fail, НИЧЕГО не пишем);
        затем сверка существования ВСЕХ целевых файлов ДО любой записи (no-clobber,
        AC #8: существующий файл не перезаписываем молча); только потом скачиваем.
        Любой сбой части всплывает (не «собрали что есть», AC #7).
        """
        client = self._build_client()
        info: dict[str, Any] = client.get_log_request(args.request_id)
        if not info:
            raise ValueError(f"Запрос {args.request_id} не найден.")
        status = info.get("status")
        if status != "processed":
            raise ValueError(
                f"Запрос {args.request_id} в статусе '{status}', скачивание невозможно "
                f"(нужен статус 'processed')."
            )

        parts = info.get("parts", [])
        if not parts:
            raise ValueError(f"У запроса {args.request_id} нет частей для скачивания.")

        if args.part is not None:
            selected = [p for p in parts if p.get("part_number") == args.part]
            if not selected:
                available = [p.get("part_number") for p in parts]
                raise ValueError(
                    f"Часть {args.part} не найдена у запроса {args.request_id} "
                    f"(доступные: {available})."
                )
        else:
            selected = list(parts)

        output_dir, prefix = self._resolve_output(args.output, args.request_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        # No-clobber: проверяем существование ВСЕХ целевых файлов до записи/сети —
        # иначе часть файлов записалась бы перед падением на коллизии (AC #8).
        targets: list[tuple[int, Path]] = []
        for part in selected:
            part_number = part.get("part_number")
            filepath = output_dir / f"{prefix}_part{part_number}.tsv"
            if filepath.exists():
                raise FileExistsError(
                    f"{filepath} уже существует — укажите другой --output "
                    f"(без молчаливой перезаписи)."
                )
            targets.append((part_number, filepath))

        saved: list[str] = []
        for part_number, filepath in targets:
            content = client.download_log_request_part(args.request_id, part_number)
            filepath.write_bytes(content)
            saved.append(str(filepath))

        # Сводку сохранённых файлов печатаем ДО очистки: если clean упадёт
        # (RuntimeError), оператор всё равно узнает, куда легли уже скачанные части.
        print(f"Скачано частей: {len(saved)}")
        for path in saved:
            print(f"  сохранено: {path}")

        cleaned = bool(args.clean)
        if cleaned:
            client.clean_log_request(args.request_id)
        print(f"Запрос очищен: {'да' if cleaned else 'нет'}")
        return {"downloaded": saved, "parts": len(saved), "cleaned": cleaned}

    def _handle_clean(self, args: argparse.Namespace) -> dict[str, Any]:
        """Очистить запрос (освободить квоту); печатает новый статус (AC #3, #5)."""
        client = self._build_client()
        response = client.clean_log_request(args.request_id)
        # clean_log_request отдаёт ПОЛНЫЙ ответ; новый статус — под log_request (риск #5).
        # `or response` защищает и от null-значения ключа (см. _handle_create).
        log_request: dict[str, Any] = response.get("log_request") or response

        print(f"Request ID: {args.request_id}")
        print(f"Новый статус: {log_request.get('status')}")
        return log_request

    def _handle_info(self, args: argparse.Namespace) -> dict[str, Any]:
        """Информация о счётчике — проверка доступа/счётчика (AC #1, #3, #5)."""
        client = self._build_client()
        data: dict[str, Any] = client.get_counter_info()
        # `or data` защищает и от null-значения ключа (см. _handle_create).
        counter = data.get("counter") or data

        print(f"Counter ID: {counter.get('id')}")
        print(f"Название: {counter.get('name')}")
        print(f"Сайт: {counter.get('site')}")
        print(f"Статус: {counter.get('status')}")
        return data

    # --- Диспетчер ------------------------------------------------------------

    def _dispatch(self, args: argparse.Namespace) -> object:
        """Вызвать handler по ``args.command`` (handler сам печатает результат, Task 2).

        Неизвестная команда невозможна (``required=True`` + ``choices``), но defensive
        ветка остаётся — fail-loud вместо тихого no-op.
        """
        command = args.command
        if command == "create":
            return self._handle_create(args)
        if command == "evaluate":
            return self._handle_evaluate(args)
        if command == "status":
            return self._handle_status(args)
        if command == "download":
            return self._handle_download(args)
        if command == "clean":
            return self._handle_clean(args)
        if command == "list":
            return self._handle_list(args)
        if command == "info":
            return self._handle_info(args)
        raise ValueError(f"Неизвестная команда: {command!r}")


def main() -> None:
    """Точка входа console-команды ``gdau-logs`` (AC #4, #6).

    Плохие аргументы/голый вызов → argparse сам ``SystemExit(2)`` (AC #6). Контрактные
    ошибки примитивов 1.2–1.5 и клиента (``ValueError``/``RuntimeError``/
    ``FileExistsError``/``OSError``) → понятное сообщение + ``SystemExit(1)`` без
    трейсбека (AC #4). Успех → результат уже напечатан handler'ом, неявный код 0.
    Креды НЕ логируем (NFR-5); диагностика (clamp/ретраи) идёт в stderr через logging.
    """
    logging.basicConfig(level=logging.INFO)
    cli = LogsApiCLI()
    parser = cli._create_parser()
    args = parser.parse_args()
    try:
        cli._dispatch(args)  # handler печатает свой результат сам
    except (ValueError, RuntimeError, FileExistsError, OSError) as exc:
        logger.error("%s", exc)  # понятное сообщение, без трейсбека
        raise SystemExit(1) from None
    # успех → неявный exit 0 (вывод уже напечатан handler'ом)


if __name__ == "__main__":
    main()
