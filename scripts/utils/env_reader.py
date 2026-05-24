"""Тонкий ридер кредов Метрики из окружения.

Замена ``auth_manager.py`` из directaiq: читает токен и счётчик Метрики из
процесс-окружения или ``.env`` per-game хранилища. Сознательно НЕ тянет тяжёлые
зависимости (``ConfigManager``/``AuthManager``/``tapi_yandex_*``) и НЕ делает
fallback на Direct-токен. Падает понятно (fail-loud) ДО любых сетевых вызовов,
если кредов нет, — чтобы ошибка конфигурации не превращалась в opaque-4xx позже.

Единственная публичная точка — :func:`read_metrica_credentials`; её дёргают
вендоренный ``MetricaClient`` (1.3, инъекция кредов), CLI ``create`` (1.6) и
оркестратор p81 (2.7).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

logger = logging.getLogger(__name__)

# Имена переменных окружения — контракт с Logs API и будущим .env.example (Epic 4).
TOKEN_ENV = "YANDEX_METRICA_TOKEN"
COUNTER_ENV = "YANDEX_METRICA_COUNTER_ID"
DATA_ROOT_ENV = "GDAU_DATA_ROOT"


@dataclass(frozen=True, slots=True)
class MetricaCredentials:
    """Готовые креды Метрики для инъекции в ``MetricaClient`` (1.3) и CLI (1.6).

    ``counter_id`` хранится как ``int`` (валидируется при чтении): f-строка в
    URL-пути клиента сериализует его корректно. Контейнер «глупый» — вся
    валидация в функции-ридере, не в ``__post_init__``.

    ``token`` помечен ``repr=False`` — иначе дефолтный ``repr`` датакласса вывел бы
    секрет в логи/трейсбеки (``logger.debug("%r", creds)``, упавший assert), нарушая
    NFR-5 «креды не логировать, в т.ч. в repr».
    """

    token: str = field(repr=False)
    counter_id: int


def read_metrica_credentials() -> MetricaCredentials:
    """Прочитать и провалидировать креды Метрики из окружения / ``.env``.

    Возвращает :class:`MetricaCredentials` при наличии валидных токена и счётчика.
    Поднимает :class:`ValueError` (до сетевых вызовов) если какой-либо креды нет,
    он пуст/из пробелов, либо счётчик не приводится к положительному целому.
    """
    env_found = _load_env()
    token = _require(TOKEN_ENV, env_found=env_found)
    raw_counter = _require(COUNTER_ENV, env_found=env_found)
    counter_id = _coerce_counter_id(raw_counter)
    return MetricaCredentials(token=token, counter_id=counter_id)


def _load_env() -> bool:
    """Best-effort загрузка ``.env`` в ``os.environ``.

    Сначала пробует ``.env`` per-game хранилища (``GDAU_DATA_ROOT/.env``), затем
    walk-up от каталога ЗАПУСКА (cwd) вверх по дереву (``find_dotenv(usecwd=True)``).
    Возвращает агрегат «нашёлся ли ХОТЬ один файл» (OR
    обоих вызовов) — для диагностики AC #7; решение о fail принимает :func:`_require`
    по факту отсутствия кредов, а не здесь (креды могут прийти прямо в окружение —
    режим ``uv --env-file .env``, см. [[mcp-env-delivery]]).

    ``override=False`` — реальное процесс-окружение (CI) имеет приоритет над файлом.
    ``interpolate=False`` — иначе ``$``/``${...}`` в токене молча исказятся (тихий
    провал класса AC #5/#7; креды — не шаблоны).
    """
    found_storage = False
    data_root = os.environ.get(DATA_ROOT_ENV)
    if data_root:
        storage_env = Path(data_root) / ".env"
        # is_file (не exists): если GDAU_DATA_ROOT указывает на файл/мусор —
        # Path(file)/".env" не пройдёт is_file() и загрузка просто пропустится.
        if storage_env.is_file():
            found_storage = load_dotenv(storage_env, override=False, interpolate=False)
    # usecwd=True: walk-up от КАТАЛОГА ЗАПУСКА оператора, а не от каталога модуля.
    # Дефолт find_dotenv (usecwd=False) ищет от scripts/utils/ — в установленном
    # wheel это site-packages, мимо .env оператора. "" если файл не найден.
    cwd_env = find_dotenv(usecwd=True)
    found_cwd = (
        load_dotenv(cwd_env, override=False, interpolate=False) if cwd_env else False
    )
    return bool(found_storage) or bool(found_cwd)


def _require(env_name: str, *, env_found: bool) -> str:
    """Вернуть непустое значение переменной ``env_name`` или fail-loud.

    Пустое/``None``/только пробелы трактуются как отсутствие (AC #5). Текст ошибки
    зависит от ``env_found`` (AC #7): нет ни одного ``.env`` → подсказка про
    ``GDAU_DATA_ROOT``; файл есть, но переменной нет → ошибка про окружение/файл.
    """
    raw = os.environ.get(env_name)
    value = raw.strip() if raw is not None else ""
    if value:
        return value
    # Логируем факт (без значения!), затем fail-loud. Креды не логируются (NFR-5).
    logger.error("Обязательная переменная окружения %s не задана или пуста", env_name)
    if env_found:
        raise ValueError(
            f"Переменная {env_name} отсутствует или пуста в .env/окружении."
        )
    raise ValueError(
        f"Переменная {env_name} не задана, и .env не найден "
        f"(проверь {DATA_ROOT_ENV} или запусти из каталога хранилища)."
    )


def _coerce_counter_id(raw: str) -> int:
    """Привести значение счётчика к строго положительному ``int`` (AC #6).

    ``repr`` мусорного значения в сообщении допустим (counter_id — не секрет,
    помогает диагностике). Токен в сообщения/логи не попадает никогда.
    """
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(
            f"{COUNTER_ENV} должен быть целым числом, получено: {raw!r}"
        ) from None
    # int("-5")/int("0") валидны для int(), но счётчик Метрики — строго > 0;
    # иначе мусорный counter молча уйдёт в URL клиента (1.3) → opaque-4xx.
    if value <= 0:
        raise ValueError(
            f"{COUNTER_ENV} должен быть положительным целым, получено: {raw!r}"
        )
    return value
