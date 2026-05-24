"""Примитивы безопасной границы дат для Logs API.

Единственное место правила «``date2`` не дальше вчера по МСК» (Logs API требует
``date2 < today``) и строгого формата ``YYYY-MM-DD``. Не вендоринг: directaiq-аналог
(``date_utils.py`` + clamp в ``p81_load_logs.py``) тянет тяжёлый ``pytz`` и на
инвертированном диапазоне тихо отдаёт «успех, 0 файлов» — здесь крошечная версия на
чистой stdlib ``datetime`` с fail-loud на пустом/инвертированном диапазоне.

Потребители (реализуются в своих историях, не здесь): CLI ``create`` (1.6) клампит
``date2`` перед запросом; оркестратор p81 (2.7); hot-window (2.8) берёт якорь окна =
:func:`moscow_yesterday`. Сети/argparse/путей хранилища этот модуль не знает.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Фиксированный офсет, а НЕ zoneinfo("Europe/Moscow")/pytz. Москва — постоянный UTC+3
# с 26.10.2014 (отмена «зимнего времени», ФЗ-№193), DST нет; все наши даты — после
# 2014, значит фикс-офсет точен. zoneinfo на Windows требует пакет tzdata (stdlib не
# несёт базу зон под Windows) → новая зависимость либо ZoneInfoNotFoundError в
# рантайме; pytz — тяжёлая зависимость вне стека (NFR-6). Фикс-офсет: ноль
# зависимостей, кросс-платформенно (NFR-2 Win↔Linux).
MSK = timezone(timedelta(hours=3))

__all__ = [
    "MSK",
    "moscow_today",
    "moscow_yesterday",
    "parse_date",
    "format_date",
    "clamp_date_range",
]

# Каноничный YYYY-MM-DD (zero-padded). date.fromisoformat с 3.11 шире контракта:
# принимает basic-формат (20260524) и week-даты (2026-W21-1) — guard их режет.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _now_utc() -> datetime:
    """Единственный шов к стене часов (тесты монкейпатчат фиксированным aware-UTC)."""
    return datetime.now(timezone.utc)


def moscow_today() -> date:
    """Сегодня по МСК.

    Инстант берётся в UTC и переводится в МСК (``.astimezone``), поэтому результат не
    зависит от локальной зоны машины (AC #4).
    """
    return _now_utc().astimezone(MSK).date()


def moscow_yesterday() -> date:
    """Вчера по МСК — потолок clamp и якорь hot-window (потребляется 2.8)."""
    return moscow_today() - timedelta(days=1)


def parse_date(value: str) -> date:
    """Строго распарсить ``YYYY-MM-DD`` в :class:`date` или fail-loud (AC #3, #6).

    Сначала guard на каноничный вид (zero-padded, дефисы), затем
    :func:`date.fromisoformat` добивает невалидные календарно (``2026-13-01``,
    ``2026-05-40``, ``0000-00-00``). Без guard голый ``fromisoformat`` в 3.11+ принял
    бы basic-формат (``20260524``) и week-даты — это не «строго YYYY-MM-DD».
    Сообщение несёт сам ввод (``{value!r}``) — дата не секрет, помогает диагностике.
    """
    stripped = value.strip()
    if not _ISO_DATE_RE.fullmatch(stripped):
        raise ValueError(
            f"Дата должна быть строго в формате YYYY-MM-DD, получено: {value!r}"
        )
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        raise ValueError(
            f"Невалидная календарная дата: {value!r}"
        ) from None


def format_date(value: date) -> str:
    """Отформатировать :class:`date` как ``YYYY-MM-DD`` (всегда zero-padded, AC #3)."""
    return value.isoformat()


def clamp_date_range(
    date1: date, date2: date, *, today_msk: date | None = None
) -> tuple[date, date]:
    """Зажать ``date2`` на «вчера по МСК» и провалидировать диапазон (AC #1, #2, #5).

    ``date2`` сегодня/будущее (> вчера) → клампится к вчера с INFO-логом, без падения
    (AC #1). ``date2`` ≤ вчера (включая ровно границу) → не меняется, лог не пишется
    (AC #2). Будущий ``date1`` или инвертированный диапазон (``date1 > date2`` после
    clamp) → :class:`ValueError` ДО возврата — у потребителя это происходит до любого
    сетевого вызова (AC #5; осознанное отличие от directaiq, где было тихо «0 файлов»).

    ``today_msk`` (keyword-only) — инъекция «сегодня» для детерминированных тестов; в
    проде не передаётся (берётся :func:`moscow_today`).
    """
    ceiling = (today_msk if today_msk is not None else moscow_today()) - timedelta(
        days=1
    )
    clamped2 = date2
    if date2 > ceiling:
        logger.info("Clamp date2 %s → %s (вчера по МСК)", date2, ceiling)
        clamped2 = ceiling
    if date1 > clamped2:
        raise ValueError(
            f"Пустой/инвертированный диапазон: date1={date1} > date2={clamped2} "
            f"(вчера по МСК {ceiling})"
        )
    return date1, clamped2
