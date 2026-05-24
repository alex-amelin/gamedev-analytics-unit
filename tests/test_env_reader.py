"""Offline-тесты env-ридера кредов Метрики (история 1.2).

Покрывают дисциплину, а не только happy-path: отсутствие/пусто/пробелы (AC #2,#5),
мусорный counter_id (AC #6), отсутствие ``.env`` и битый ``GDAU_DATA_ROOT`` (AC #7),
запрет Direct-fallback (AC #3) и запрет тяжёлых импортов (AC #4 — проверяется по
реальным import-узлам через ``ast``, не по подстроке).

Без сети и без реального ``.env``: окружение мокается через ``monkeypatch``,
файлы пишутся в ``tmp_path``. Изоляция окружения вынесена в autouse-fixture —
иначе результат теста зависел бы от машины (см. Dev Notes истории).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from scripts.utils.env_reader import MetricaCredentials, read_metrica_credentials

TOKEN_ENV = "YANDEX_METRICA_TOKEN"
COUNTER_ENV = "YANDEX_METRICA_COUNTER_ID"
DIRECT_ENV = "YANDEX_DIRECT_TOKEN"
DATA_ROOT_ENV = "GDAU_DATA_ROOT"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Чистое детерминированное окружение для каждого теста.

    Изоляцию обеспечивают ТРИ механизма (важны все три):

    1. ``delenv`` на setup КАЖДОГО теста — стирает relevant-переменные, в т.ч. те,
       что ``load_dotenv`` мог дописать в ``os.environ`` в предыдущем тесте. Именно
       это (а не teardown-откат monkeypatch) держит чистоту между тестами.
    2. ``chdir(tmp_path)`` — уводит cwd в пустой каталог: cwd-relative поиск
       (``find_dotenv(usecwd=True)`` в ридере) стартует отсюда.
    3. Стаб ``find_dotenv`` — ищет ``.env`` ТОЛЬКО в cwd, без walk-up к родителям.
       Без него ``find_dotenv(usecwd=True)`` ушёл бы вверх от ``tmp_path`` (в системный
       temp) и мог наткнуться на посторонний ``.env`` → тесты «нет .env» стали бы
       машинозависимыми (ловушка project-context «зелёный/красный зависит от машины»).
       Один ``chdir`` это НЕ глушит — walk-up идёт от cwd вверх. Walk-up к родителям
       недетерминирован, его намеренно не тестируем; контракт «.env относительно
       каталога запуска» сохранён.
    """
    for name in (TOKEN_ENV, COUNTER_ENV, DIRECT_ENV, DATA_ROOT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    def _find_dotenv_cwd_only(*_args: object, **_kwargs: object) -> str:
        candidate = Path.cwd() / ".env"
        return str(candidate) if candidate.is_file() else ""

    monkeypatch.setattr("scripts.utils.env_reader.find_dotenv", _find_dotenv_cwd_only)


def _write_env(path: Path, lines: list[str]) -> None:
    """Записать ``.env`` в каталог ``path`` с заданными строками."""
    (path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- AC #1: оба значения присутствуют ---------------------------------------


def test_returns_both_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Оба значения в окружении → MetricaCredentials с int-счётчиком (AC #1)."""
    monkeypatch.setenv(TOKEN_ENV, "tok-abc")
    monkeypatch.setenv(COUNTER_ENV, "12345")

    creds = read_metrica_credentials()

    assert isinstance(creds, MetricaCredentials)
    assert creds.token == "tok-abc"
    assert creds.counter_id == 12345
    assert isinstance(creds.counter_id, int)


# --- AC #2: отсутствие переменной → ValueError с её именем -------------------


def test_missing_token_raises_with_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет токена → ValueError, в тексте имя переменной токена (AC #2)."""
    monkeypatch.setenv(COUNTER_ENV, "12345")

    with pytest.raises(ValueError, match=TOKEN_ENV):
        read_metrica_credentials()


def test_missing_counter_raises_with_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет счётчика → ValueError, в тексте имя переменной счётчика (AC #2)."""
    monkeypatch.setenv(TOKEN_ENV, "tok-abc")

    with pytest.raises(ValueError, match=COUNTER_ENV):
        read_metrica_credentials()


# --- AC #3: НЕТ fallback на Direct-токен ------------------------------------


def test_no_direct_token_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Есть только YANDEX_DIRECT_TOKEN → fallback не происходит (AC #3)."""
    monkeypatch.setenv(DIRECT_ENV, "direct-secret")
    monkeypatch.setenv(COUNTER_ENV, "12345")

    with pytest.raises(ValueError, match=TOKEN_ENV) as exc:
        read_metrica_credentials()

    # Direct-токен не должен ни использоваться, ни утекать в сообщение об ошибке.
    assert "direct-secret" not in str(exc.value)


# --- AC #5: пустая строка / одни пробелы трактуются как отсутствие -----------


@pytest.mark.parametrize("blank", ["", "   ", "\t", " \n "])
def test_blank_token_is_absence(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    """Пустой/пробельный токен → fail-loud как отсутствие (AC #5)."""
    monkeypatch.setenv(TOKEN_ENV, blank)
    monkeypatch.setenv(COUNTER_ENV, "12345")

    with pytest.raises(ValueError, match=TOKEN_ENV):
        read_metrica_credentials()


# --- AC #6: counter_id обязан приводиться к положительному int ---------------


def test_non_numeric_counter_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нечисловой counter_id → ValueError про целочисленность (AC #6)."""
    monkeypatch.setenv(TOKEN_ENV, "tok-abc")
    monkeypatch.setenv(COUNTER_ENV, "abc")

    with pytest.raises(ValueError, match="целым"):
        read_metrica_credentials()


@pytest.mark.parametrize("bad", ["-5", "0"])
def test_non_positive_counter_raises(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """Ноль/отрицательный counter_id → ValueError про положительность (AC #6)."""
    monkeypatch.setenv(TOKEN_ENV, "tok-abc")
    monkeypatch.setenv(COUNTER_ENV, bad)

    with pytest.raises(ValueError, match="положительн"):
        read_metrica_credentials()


# --- AC #7 + AC #1: загрузка из .env хранилища ------------------------------


def test_reads_from_storage_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Кредов нет в окружении, но есть .env в GDAU_DATA_ROOT → читаются (AC #1,#7)."""
    _write_env(tmp_path, [f"{TOKEN_ENV}=tok-from-file", f"{COUNTER_ENV}=777"])
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    creds = read_metrica_credentials()

    assert creds.token == "tok-from-file"
    assert creds.counter_id == 777


def test_no_env_no_vars_fails_mentioning_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет .env, нет GDAU_DATA_ROOT, нет переменных → fail-loud про .env (AC #7)."""
    # fixture уже сделал chdir в пустой tmp_path и убрал переменные.
    with pytest.raises(ValueError, match=r"\.env"):
        read_metrica_credentials()


def test_broken_data_root_fails_mentioning_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GDAU_DATA_ROOT на несуществующий путь, кредов нет → fail-loud про .env (AC #7)."""
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "нет-такого-каталога"))

    with pytest.raises(ValueError, match=r"\.env"):
        read_metrica_credentials()


def test_data_root_pointing_at_file_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GDAU_DATA_ROOT указывает на файл (мусор) → загрузка пропускается, fail-loud (AC #7)."""
    junk = tmp_path / "junk.txt"
    junk.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv(DATA_ROOT_ENV, str(junk))

    with pytest.raises(ValueError, match=r"\.env"):
        read_metrica_credentials()


def test_strip_only_trims_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Обрамляющие пробелы в .env обрезаются, значащие символы не трогаются."""
    _write_env(
        tmp_path,
        [f'{TOKEN_ENV}="  tok123  "', f"{COUNTER_ENV}=42"],
    )
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path))

    creds = read_metrica_credentials()

    assert creds.token == "tok123"
    assert creds.counter_id == 42


def test_reads_from_cwd_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Нет переменных и нет GDAU_DATA_ROOT, но .env лежит в cwd → читается (cwd-relative)."""
    _write_env(tmp_path, [f"{TOKEN_ENV}=tok-cwd", f"{COUNTER_ENV}=99"])
    # fixture уже сделал chdir(tmp_path); стаб find_dotenv найдёт ./.env в cwd.
    creds = read_metrica_credentials()

    assert creds.token == "tok-cwd"
    assert creds.counter_id == 99


# --- AC #4: модуль не тянет тяжёлые зависимости (проверка по import-узлам) ---


def test_no_heavy_dependencies_imported() -> None:
    """Среди реальных import-узлов нет ConfigManager/AuthManager/tapi_yandex_* и пр. (AC #4).

    Намеренно НЕ по подстроке: модульный docstring сам упоминает ``auth_manager`` —
    наивный ``"auth_manager" not in source`` дал бы ложный красный. Парсим AST и
    смотрим именно ``Import``/``ImportFrom``-узлы.
    """
    import scripts.utils.env_reader as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("config_manager", "auth_manager", "tapi_yandex", "requests", "duckdb")
    offenders = {
        name for name in imported if any(bad in name for bad in forbidden)
    }
    assert not offenders, f"запрещённые импорты в env_reader: {offenders}"


# --- Дисциплина Task 2: импорт без side-effects -----------------------------


def test_dotenv_loaded_only_inside_functions() -> None:
    """``load_dotenv``/``find_dotenv`` вызываются ТОЛЬКО внутри функций, не на уровне модуля.

    Прямая статическая проверка по AST — детерминированная и без reload. Прежний
    reload+``os.environ``-тест давал ложную уверенность: в очищенном окружении
    (фикстура: пустой cwd, нет переменных) гипотетический module-level вызов мог бы
    не изменить ``os.environ`` и пройти незамеченным. Здесь же мы ловим сам факт
    вызова на уровне модуля статически.
    """
    import scripts.utils.env_reader as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    def _call_name(call: ast.Call) -> str:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    module_level_calls: list[str] = []

    def _scan(node: ast.AST, *, inside_func: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.Call)
                and not inside_func
                and _call_name(child) in {"load_dotenv", "find_dotenv"}
            ):
                module_level_calls.append(_call_name(child))
            _scan(
                child,
                inside_func=inside_func
                or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)),
            )

    _scan(tree, inside_func=False)
    assert not module_level_calls, (
        f"dotenv вызывается на уровне модуля: {module_level_calls} — "
        f"загрузка .env должна быть только внутри функций (Task 2)"
    )
