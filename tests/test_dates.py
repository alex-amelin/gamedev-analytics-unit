"""Offline-тесты примитива дат (история 1.4).

Покрывают дисциплину границы, а не только happy-path: clamp ``date2`` на «вчера по
МСК» с логом (AC #1), стабильность значения под границей (AC #2), строгий формат
``YYYY-MM-DD`` и round-trip (AC #3), расчёт «сегодня» от МСК независимо от локальной
зоны (AC #4), fail-loud на будущем ``date1``/инвертированном диапазоне (AC #5),
строгий парсинг с понятной ошибкой на мусоре (AC #6) и запрет тяжёлых таймзонных
зависимостей (проверяется по реальным import-узлам через ``ast``, не по подстроке).

Без сети и без обращения к стене часов в clamp-тестах: «сегодня» инъектируется
параметром ``today_msk``; единственный шов к часам ``_now_utc`` монкейпатчится
фиксированным aware-UTC инстантом. Так набор детерминирован в любой день и в любой
локальной зоне CI (ubuntu + windows).
"""

from __future__ import annotations

import ast
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts.utils.dates import (
    MSK,
    clamp_date_range,
    format_date,
    moscow_today,
    moscow_yesterday,
    parse_date,
)


# --- AC #4: «сегодня/вчера по МСК» считается от МСК, не от UTC/локальной зоны --


def test_moscow_today_uses_msk_not_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Инстант 24-го 22:30 UTC = 25-го 01:30 МСК → сегодня по МСК = 25-е (AC #4).

    Доказывает, что расчёт опирается на МСК (UTC+3), а не на UTC: в UTC дата ещё
    24-я, но через ``.astimezone(MSK)`` сутки уже перевалили на 25-е.
    """
    monkeypatch.setattr(
        "scripts.utils.dates._now_utc",
        lambda: datetime(2026, 5, 24, 22, 30, tzinfo=timezone.utc),
    )

    assert moscow_today() == date(2026, 5, 25)
    assert moscow_yesterday() == date(2026, 5, 24)


def test_msk_is_fixed_utc_plus_three() -> None:
    """Константа MSK — фиксированный офсет UTC+3 (без DST/библиотеки зон)."""
    assert MSK.utcoffset(None).total_seconds() == 3 * 3600


# --- AC #1: date2 сегодня/будущее → clamp на «вчера по МСК» + INFO-лог --------


def test_clamp_today_to_yesterday_logs_info(caplog: pytest.LogCaptureFixture) -> None:
    """date2 == сегодня (> вчера) → зажимается на вчера, пишется INFO (AC #1)."""
    with caplog.at_level(logging.INFO, logger="scripts.utils.dates"):
        d1, d2 = clamp_date_range(
            date(2026, 5, 1), date(2026, 5, 25), today_msk=date(2026, 5, 25)
        )

    assert d1 == date(2026, 5, 1)
    assert d2 == date(2026, 5, 24)
    assert any("Clamp" in rec.message for rec in caplog.records)


def test_clamp_far_future_date2(caplog: pytest.LogCaptureFixture) -> None:
    """date2 далеко в будущем → клампится к вчера, без исключения (AC #1)."""
    with caplog.at_level(logging.INFO, logger="scripts.utils.dates"):
        d1, d2 = clamp_date_range(
            date(2026, 5, 1), date(2030, 1, 1), today_msk=date(2026, 5, 25)
        )

    assert d2 == date(2026, 5, 24)
    assert any("Clamp" in rec.message for rec in caplog.records)


def test_clamp_default_ceiling_uses_moscow_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Прод-путь без today_msk: ceiling берётся от moscow_today() (AC #1, #4).

    Потребители (CLI 1.6, p81 2.7) НЕ передают today_msk — ceiling считается через
    moscow_today(). Шов _now_utc монкейпатчится 25-м 12:00 UTC (= 25-е по МСК) →
    вчера=24-е; date2=25-е (сегодня) клампится к 24-му. Покрывает default-ветку,
    которую остальные clamp-тесты обходят явным today_msk.
    """
    monkeypatch.setattr(
        "scripts.utils.dates._now_utc",
        lambda: datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
    )

    d1, d2 = clamp_date_range(date(2026, 5, 1), date(2026, 5, 25))

    assert d2 == date(2026, 5, 24)


# --- AC #2: date2 ≤ вчера → значение не меняется, лог не пишется ---------------


def test_clamp_on_boundary_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    """date2 ровно == вчера (граница) → не меняется и лог пуст (нет off-by-one, AC #2)."""
    with caplog.at_level(logging.INFO, logger="scripts.utils.dates"):
        d1, d2 = clamp_date_range(
            date(2026, 5, 1), date(2026, 5, 24), today_msk=date(2026, 5, 25)
        )

    assert d2 == date(2026, 5, 24)
    assert not any("Clamp" in rec.message for rec in caplog.records)


def test_clamp_below_boundary_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    """date2 < вчера → не меняется и лог пуст (AC #2)."""
    with caplog.at_level(logging.INFO, logger="scripts.utils.dates"):
        d1, d2 = clamp_date_range(
            date(2026, 5, 1), date(2026, 5, 20), today_msk=date(2026, 5, 25)
        )

    assert d2 == date(2026, 5, 20)
    assert not any("Clamp" in rec.message for rec in caplog.records)


def test_single_valid_day_equals_yesterday() -> None:
    """Одинокий валидный день == вчера → (день, день) без ошибки и без clamp."""
    d1, d2 = clamp_date_range(
        date(2026, 5, 24), date(2026, 5, 24), today_msk=date(2026, 5, 25)
    )

    assert (d1, d2) == (date(2026, 5, 24), date(2026, 5, 24))


# --- AC #3: строгий формат YYYY-MM-DD + round-trip ----------------------------


def test_format_date_zero_padded() -> None:
    """format_date даёт zero-padded YYYY-MM-DD (AC #3)."""
    assert format_date(date(2026, 5, 1)) == "2026-05-01"


def test_parse_date_canonical() -> None:
    """parse_date принимает каноничный YYYY-MM-DD (AC #3)."""
    assert parse_date("2026-05-01") == date(2026, 5, 1)


def test_parse_format_round_trip() -> None:
    """round-trip parse→format и обратно сохраняет значение (AC #3)."""
    assert format_date(parse_date("2026-05-01")) == "2026-05-01"
    assert parse_date(format_date(date(2026, 5, 1))) == date(2026, 5, 1)


# --- AC #5: будущий date1 / инвертированный диапазон → fail-loud ---------------


def test_future_date1_raises(caplog: pytest.LogCaptureFixture) -> None:
    """date1 в будущем (> вчера ≥ clamped2) → ValueError, обе даты в тексте (AC #5)."""
    with pytest.raises(ValueError, match="инвертирован") as exc:
        clamp_date_range(
            date(2030, 1, 1), date(2026, 5, 20), today_msk=date(2026, 5, 25)
        )

    msg = str(exc.value)
    assert "2030-01-01" in msg
    assert "2026-05-20" in msg


def test_inverted_range_raises() -> None:
    """date1 > date2 (обе в прошлом, clamp не сработал) → ValueError (AC #5)."""
    with pytest.raises(ValueError, match="инвертирован") as exc:
        clamp_date_range(
            date(2026, 5, 20), date(2026, 5, 10), today_msk=date(2026, 6, 1)
        )

    msg = str(exc.value)
    assert "2026-05-20" in msg
    assert "2026-05-10" in msg


def test_clamp_induced_inversion_raises(caplog: pytest.LogCaptureFixture) -> None:
    """date2 в будущем клампится НИЖЕ валидного date1 → ValueError (AC #5).

    Ключевое отличие от directaiq: clamp может сам создать инвертированный диапазон.
    date1=25-е валиден сейчас, но date2=30-е (будущее) зажимается на вчера (24-е),
    после чего date1(25) > clamped2(24) → fail-loud. directaiq тут отдавал «0 файлов».
    """
    with caplog.at_level(logging.INFO, logger="scripts.utils.dates"):
        with pytest.raises(ValueError, match="инвертирован"):
            clamp_date_range(
                date(2026, 5, 25), date(2026, 5, 30), today_msk=date(2026, 5, 25)
            )

    # Clamp всё же отработал (date2 был зажат), и только потом — fail на инверсии.
    assert any("Clamp" in rec.message for rec in caplog.records)


def test_single_today_day_raises() -> None:
    """Одинокий «сегодня» (date1==date2==today) → ValueError: граница ceiling+1 (AC #5).

    Оператор просит «данные за сегодня»: date2=25-е клампится к вчера (24-е), затем
    date1(25) > clamped2(24) → ошибка. Верхняя кромка off-by-one (== ceiling+1).
    """
    with pytest.raises(ValueError, match="инвертирован"):
        clamp_date_range(
            date(2026, 5, 25), date(2026, 5, 25), today_msk=date(2026, 5, 25)
        )


def test_clamp_lands_exactly_on_date1_is_ok() -> None:
    """date2 в будущем клампится РОВНО к date1 → (date1, date1) без ошибки (AC #5).

    Нижняя кромка inversion-guard (``>``): clamped2 == date1 проходит. Дополняет
    test_single_valid_day_equals_yesterday, где clamp не срабатывал вовсе.
    """
    d1, d2 = clamp_date_range(
        date(2026, 5, 24), date(2026, 5, 30), today_msk=date(2026, 5, 25)
    )

    assert (d1, d2) == (date(2026, 5, 24), date(2026, 5, 24))


# --- AC #6: строгий парсинг — мусор поднимает ValueError (не падает в clamp) ---


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        " ",
        "garbage",
        "2026/05/24",
        "24-05-2026",
        "2026-13-01",
        "2026-05-40",
        "20260524",
        "2026-W21-1",
        "2026-5-1",
        "0000-00-00",
    ],
)
def test_parse_date_rejects_garbage(garbage: str) -> None:
    """Мусор → ValueError с самим вводом в тексте; не падение в clamp-логике (AC #6).

    Критичны ``20260524`` (basic-формат) и ``2026-W21-1`` (week-дата): голый
    ``date.fromisoformat`` в 3.13 их ПРИНЯЛ БЫ — guard ``\\d{4}-\\d{2}-\\d{2}`` их
    отсекает. ``2026-13-01``/``2026-05-40``/``0000-00-00`` проходят guard, но
    ``fromisoformat`` добивает их как невалидные календарно.
    """
    with pytest.raises(ValueError):
        parse_date(garbage)


def test_parse_error_mentions_value() -> None:
    """Текст ошибки парсинга содержит сам некорректный ввод (дата не секрет, AC #6)."""
    with pytest.raises(ValueError, match="20260524"):
        parse_date("20260524")


def test_calendar_invalid_distinct_from_format_error() -> None:
    """Каноничный по форме, но невалидный календарно → ветка «Невалидная календарная».

    ``2026-13-01`` проходит guard ``\\d{4}-\\d{2}-\\d{2}``, но ``date.fromisoformat``
    его отвергает — поднимается сообщение ВТОРОЙ ветки, не format-guard (AC #6).
    Отделяет два пути ошибки, чтобы их перестановка не прошла незамеченной.
    """
    with pytest.raises(ValueError, match="Невалидная календарная"):
        parse_date("2026-13-01")


# --- Анти-зависимость: модуль не тянет zoneinfo/pytz/tzdata (AC #4) -----------


def test_no_timezone_dependencies_imported() -> None:
    """Среди реальных import-узлов нет zoneinfo/pytz/tzdata (AC #4).

    Намеренно НЕ по подстроке: docstring/комментарии модуля сами упоминают
    ``zoneinfo``/``pytz`` (объясняя, почему их НЕ берём) — наивный
    ``"zoneinfo" not in source`` дал бы ложный красный. Парсим AST и смотрим именно
    ``Import``/``ImportFrom``-узлы. Гарантирует фикс-офсет и кросс-платформенность.
    """
    import scripts.utils.dates as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("zoneinfo", "pytz", "tzdata")
    offenders = {name for name in imported if any(bad in name for bad in forbidden)}
    assert not offenders, f"запрещённые таймзонные импорты в dates: {offenders}"
