"""Offline-тесты жёсткой сверки числа строк (история 2.3).

Покрывают дисциплину целостности приёма, не только happy-path: счёт строк-данных
источника по сырому TSV с вычитанием заголовка НА КАЖДУЮ часть (off-by-P-гард, AC #3),
суммирование по частям без дедупа (AC #4), жёсткий fail при расхождении ``expected``↔
``actual`` (исключение, НЕ ``warning`` — AC #1), успех ``0 == 0`` для легитимно пустого дня
(AC #4), эквивалентность ``bytes``/``str``-входа, устойчивость к ``\\r\\n`` и хвостовому
``\\n``, и запрет ``pandas``/``polars``/``numpy``/``pyarrow`` + directaiq-инфры + ``duckdb``/
``parquet_store`` (по реальным import-узлам через ``ast``, не по подстроке — риск №1: модуль
чистый, без БД и без сцепки с записью 2.2).

Без сети, без файлов, без DuckDB — ``row_check`` работает на готовых байтах/числах (никаких
``tmp_path``/``monkeypatch``). Live-набор осознанно отсутствует: ``row_check`` в сеть не ходит
([[realapi-smoke-tests]] — opt-in live только для внешнего API; формат/заголовок частей
подтвердит live оркестратора 2.7). AC #2 (день не помечается загруженным) на уровне 2.3 покрыт
негативной гарантией «сверка бросает ДО шага ``load_state``»; интеграционная проверка «день
остался незагруженным» принадлежит 2.4/2.7 (там есть ``load_state``) — это граница истории,
не дыра покрытия.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from scripts.utils.row_check import (
    RowCountMismatchError,
    count_part_rows,
    count_source_rows,
    split_tsv_rows,
    verify_row_count,
)

# Заголовок по образцу tests/fixtures/logs_visits_sample.tsv — повторяется в КАЖДОЙ части.
_HEADER = "ym:s:visitID\tym:s:dateTime\tym:s:watchIDs"
_ROW1 = "17298374650000000001\t2026-05-20 12:34:56\t[8273645,8273646]"
_ROW2 = "17298374650000000002\t2026-05-20 13:01:02\t[8273647]"


def _part(*data_rows: str, trailing_newline: bool = True, sep: str = "\n") -> str:
    """Собрать одну TSV-часть: заголовок + строки-данные (как отдаёт ``download_log_request_part``)."""
    text = sep.join([_HEADER, *data_rows])
    if trailing_newline:
        text += sep
    return text


# --- Единый сплиттер split_tsv_rows: общий шов границ строк с p81 (defer 2.3) -----------


def test_split_tsv_rows_keeps_header_and_data() -> None:
    """Сплиттер отдаёт ВСЕ непустые строки части, включая заголовок (его вычитает счёт, не он)."""
    assert split_tsv_rows(_part(_ROW1, _ROW2)) == [_HEADER, _ROW1, _ROW2]


def test_split_tsv_rows_crlf_and_trailing_newline() -> None:
    r"""``\r\n`` и хвостовой перевод строки не добавляют пустых записей (срез ``\r`` + фильтр пустых)."""
    assert split_tsv_rows(_part(_ROW1, _ROW2, sep="\r\n")) == [_HEADER, _ROW1, _ROW2]
    assert split_tsv_rows(_part(_ROW1, trailing_newline=False)) == [_HEADER, _ROW1]


def test_split_tsv_rows_bytes_equals_str() -> None:
    """``bytes``-вход (utf-8) даёт тот же список строк, что ``str``."""
    text = _part(_ROW1, _ROW2)
    assert split_tsv_rows(text.encode("utf-8")) == split_tsv_rows(text)


def test_count_part_rows_is_split_minus_one_header() -> None:
    """Контракт единого шва: ``count_part_rows`` == ``len(split_tsv_rows) − 1`` (defer 2.3).

    Именно эта связь гарантирует, что счёт сверки и парсинг дня в p81 (2.7) согласованы по
    границам строк — misfire off-by-N исключён по построению.
    """
    for part in (_part(_ROW1, _ROW2), _part(_ROW1, _ROW2, sep="\r\n"), _HEADER, ""):
        assert count_part_rows(part) == max(0, len(split_tsv_rows(part)) - 1)


# --- AC #3: заголовок исключён из счёта (нет off-by-one), счёт на уровне части ----------


def test_count_part_rows_excludes_header() -> None:
    """Часть = заголовок + 2 строки-данные → 2 (заголовок не считается; AC #3)."""
    assert count_part_rows(_part(_ROW1, _ROW2)) == 2


def test_count_part_rows_trailing_newline_does_not_inflate() -> None:
    """Хвостовой перевод строки не инфлейтит счёт (splitlines + фильтр пустых; AC #3)."""
    assert count_part_rows(_part(_ROW1, _ROW2, trailing_newline=True)) == 2
    assert count_part_rows(_part(_ROW1, _ROW2, trailing_newline=False)) == 2


def test_count_part_rows_header_only_is_zero() -> None:
    """Часть только с заголовком (0 строк-данных) → 0, не -1 (гард ``max(0, …)``; AC #3/#4)."""
    assert count_part_rows(_HEADER + "\n") == 0
    assert count_part_rows(_HEADER) == 0


def test_count_part_rows_empty_part_is_zero() -> None:
    """Пустая часть (пустая строка / пустые байты) → 0 (гард ``max(0, …)``)."""
    assert count_part_rows("") == 0
    assert count_part_rows(b"") == 0


def test_count_part_rows_crlf_line_endings() -> None:
    r"""Переводы строк ``\r\n`` считаются корректно (срез хвостового ``\r``; гард краёв)."""
    assert count_part_rows(_part(_ROW1, _ROW2, sep="\r\n")) == 2


def test_count_part_rows_does_not_split_on_exotic_unicode_separators() -> None:
    r"""Встроенный ``\v``/LS/``\x85`` в значении поля НЕ создаёт лишнюю строку (ревью-патч 2026-05-24).

    Записи TSV разделены РОВНО ``\n``; ``str.splitlines`` дополнительно резал бы эти символы и
    инфлейтил бы ``expected`` на валидном дне → ложный жёсткий fail. Поэтому счёт идёт по
    ``text.split("\n")``: поле со встроенным ``\v``/LS остаётся ОДНОЙ строкой-данными.
    """
    row_with_exotic = "17298374650000000003\t2026-05-20 14:00:00\t[1\x0b2 \x853]"
    assert count_part_rows(_part(row_with_exotic)) == 1
    assert count_part_rows(_part(_ROW1, row_with_exotic)) == 2


def test_count_part_rows_blank_crlf_line_not_counted() -> None:
    r"""Пустая ``\r\n``-строка между записями не считается за данные (срез ``\r`` ДО фильтра пустых)."""
    assert count_part_rows(_HEADER + "\r\n" + _ROW1 + "\r\n" + "\r\n" + _ROW2 + "\r\n") == 2


def test_count_part_rows_bytes_equals_str() -> None:
    """``bytes``-вход (utf-8) даёт тот же счёт, что ``str`` (AC #3)."""
    text = _part(_ROW1, _ROW2)
    assert count_part_rows(text.encode("utf-8")) == count_part_rows(text) == 2


# --- AC #3: счёт источника по частям — сумма без P заголовков (off-by-P-гард) -----------


def test_count_source_rows_sums_parts_minus_per_part_header() -> None:
    """Многочастный источник: part1=header+2, part2=header+3 → 5 (не 6 без вычитания, не 4).

    Заголовок есть в КАЖДОЙ части → наивное «всего строк − 1» дало бы off-by-(P−1). Считаем
    на уровне части и суммируем (риск №3).
    """
    part1 = _part(_ROW1, _ROW2)  # header + 2 строки-данные
    part2 = _part(_ROW1, _ROW2, _ROW1)  # header + 3 строки-данные
    assert count_source_rows([part1, part2]) == 5


def test_count_source_rows_single_part() -> None:
    """Одна часть оборачивается в список ``[payload]`` (не передаём голый ``bytes``)."""
    assert count_source_rows([_part(_ROW1, _ROW2)]) == 2


def test_count_source_rows_bytes_parts() -> None:
    """Части-``bytes`` суммируются так же, как ``str`` (3 = 2 + 1)."""
    parts = [_part(_ROW1, _ROW2).encode("utf-8"), _part(_ROW1).encode("utf-8")]
    assert count_source_rows(parts) == 3


def test_count_source_rows_rejects_bare_part_not_iterable() -> None:
    """Одна часть ``bytes``/``str`` вместо списка частей → ``TypeError`` (ревью-патч 2026-05-24).

    ``mypy`` НЕ ловит голую ``str`` (``str`` ⊂ ``Iterable[bytes | str]``); без гарда перебор по
    символам дал бы суммарно ``0`` (тихая потеря данных). Гард делает footgun громким.
    """
    with pytest.raises(TypeError):
        count_source_rows(_part(_ROW1, _ROW2))  # str — забыли обернуть в список
    with pytest.raises(TypeError):
        count_source_rows(_part(_ROW1, _ROW2).encode("utf-8"))  # bytes — забыли обернуть


# --- AC #4: дубли НЕ дедупятся; пустой день 0==0 — успех --------------------------------


def test_duplicate_data_rows_are_counted_not_deduped() -> None:
    """Две ОДИНАКОВЫЕ строки-данные → 2, не 1 (сырьё verbatim, дедупа нет; AC #4)."""
    assert count_part_rows(_part(_ROW1, _ROW1)) == 2


def test_empty_day_no_parts_is_zero() -> None:
    """Ноль частей → 0 (легитимно пустой день; AC #4)."""
    assert count_source_rows([]) == 0


def test_empty_day_header_only_part_is_zero() -> None:
    """Часть только с заголовком → 0 строк источника (пустой день; AC #4)."""
    assert count_source_rows([_HEADER + "\n"]) == 0


# --- AC #1: расхождение → жёсткий fail (исключение, НЕ warning) -------------------------


def test_verify_mismatch_raises_with_context() -> None:
    """``expected != actual`` → RowCountMismatchError; в сообщении источник, дата, оба числа (AC #1)."""
    with pytest.raises(RowCountMismatchError) as exc_info:
        verify_row_count(5, 4, source="visits", date="2026-05-20")
    message = str(exc_info.value)
    assert "5" in message
    assert "4" in message
    assert "visits" in message
    assert "2026-05-20" in message


def test_mismatch_error_is_runtimeerror_subclass() -> None:
    """RowCountMismatchError — подкласс RuntimeError (точечный except для p81 2.7; AC #1/#2)."""
    assert issubclass(RowCountMismatchError, RuntimeError)


def test_verify_match_returns_none_no_raise() -> None:
    """Совпадение → возврат ``None``, без исключения (AC #1)."""
    assert verify_row_count(5, 5, source="visits", date="2026-05-20") is None


def test_verify_does_not_warn_on_mismatch(caplog: pytest.LogCaptureFixture) -> None:
    """Расхождение НЕ логируется как ``warning`` — это исключение, а не мягкий warning (AC #1, риск №4)."""
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RowCountMismatchError):
            verify_row_count(5, 4, source="visits", date="2026-05-20")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_verify_zero_equals_zero_is_success() -> None:
    """``0 == 0`` (легитимно пустой день) → успех, не ошибка/«нет данных» (AC #4)."""
    assert verify_row_count(0, 0, source="visits", date="2026-05-20") is None


# --- AC #2: гейт load_state — негативная гарантия на уровне 2.3 -------------------------


def test_mismatch_blocks_load_state_step_by_raising() -> None:
    """Сверка БРОСАЕТ на mismatch → шаг ``load_state`` (2.4) недостижим (негативная гарантия AC #2).

    На уровне 2.3 гарантия «день не помечается загруженным» обеспечивается тем, что
    ``verify_row_count`` бросает ДО шага ``load_state`` в протоколе идемпотентного дня
    (…→ write_partition → [2.3] сверка → load_state). Интеграционная проверка «день остался
    незагруженным» принадлежит 2.4/2.7 (там есть ``load_state``) — граница истории, не дыра.
    """
    with pytest.raises(RowCountMismatchError):
        verify_row_count(expected=10, actual=9, source="hits", date="2026-05-21")


# --- Гарды: защита от мусора вызывающего ------------------------------------------------


def test_verify_negative_counts_raise_value_error() -> None:
    """Отрицательное число строк → ValueError (баг аргумента, не инцидент целостности)."""
    with pytest.raises(ValueError):
        verify_row_count(-1, 0, source="visits", date="2026-05-20")
    with pytest.raises(ValueError):
        verify_row_count(0, -1, source="visits", date="2026-05-20")


# --- Анти-зависимость: чистый stdlib-примитив, без БД и без сцепки с записью ------------


def test_no_heavy_db_or_write_coupling_imported() -> None:
    """Нет import pandas/polars/numpy/pyarrow, directaiq-инфры, И duckdb/parquet_store (риск №1).

    Не по подстроке (docstring модуля упоминает parquet_store/duckdb) — парсим AST и смотрим
    реальные import-узлы по корню имени. Этот тест фиксирует, что row_check — чистый
    stdlib-примитив без БД и без сцепки с записью (2.2).
    """
    import scripts.utils.row_check as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    forbidden = {
        "pandas",
        "polars",
        "numpy",
        "pyarrow",
        "config_manager",
        "base_script",
        "duckdb",
    }
    offenders = {n for n in imported if n.split(".")[0] in forbidden}
    assert not offenders, f"запрещённые импорты в row_check: {offenders}"
    # Сцепка с записью (риск №1): row_check НЕ импортирует parquet_store (нулевая по коду).
    assert "scripts.utils.parquet_store" not in imported
