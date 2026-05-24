"""Offline-тесты CLI-примитивов жизненного цикла Logs API (история 1.6).

Покрывают дисциплину, а не только happy-path:
- AC #1: ``--help`` перечисляет все подкоманды (включая info);
- AC #2: ``create`` берёт поля из каталога (FR-2, не из CLI), клампит ``date2`` (1.4),
  креды от env-ридера (1.2) → клиент (1.3); невалидная/инвертированная дата → fail ДО
  построения клиента;
- AC #3: ``status``/``list``/``clean``/``evaluate``/``info`` корректно проксируют методы
  клиента (с учётом асимметрии форм ответов, риск #5) и печатают результат;
- AC #4: контрактные ошибки → exit 1 + понятное сообщение, успех → exit 0, токен не течёт;
- AC #5: вывод человекочитаемым текстом, параметра ``--format`` нет;
- AC #6: голый вызов / невалидный source → argparse exit 2 без трейсбека;
- AC #7: ранний/несуществующий download/status → fail-loud, ни один файл не записан;
- AC #8: отказ create (квота) → exit 1; ``download`` не перезаписывает существующий файл;
- анти-зависимость: модуль не тянет pandas/polars/numpy и инфру directaiq (по ``ast``).

Без сети и без ``.env``: три шва (``MetricaClient``/``read_metrica_credentials``/
``load_catalog``) монкейпатчатся в неймспейсе CLI-модуля на фейки; никакого
``requests-mock`` (нет такой dev-зависимости). Для детерминизма clamp ``moscow_today``
зафиксирован autouse-фикстурой (иначе результат зависел бы от реального «сегодня»).
"""

from __future__ import annotations

import ast
import logging
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.tools.logs_api_cli as cli_mod
from scripts.tools.logs_api_cli import LogsApiCLI, main

# Фиксированное «сегодня» по МСК для детерминированного clamp: ceiling = вчера = 2026-05-23.
FIXED_TODAY = date(2026, 5, 24)

# Поля фейк-каталога: важно, что они НЕ совпадают с тем, что мог бы передать оператор —
# create обязан взять именно их (FR-2, не из CLI).
DEFAULT_FIELDS: dict[str, list[str]] = {
    "visits": ["ym:s:date", "ym:s:visitID"],
    "hits": ["ym:pv:watchID", "ym:pv:URL"],
}


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Зафиксировать «сегодня по МСК» — clamp детерминирован, не зависит от машины.

    Патчим имя в ``scripts.utils.dates``: ``clamp_date_range`` (импортированный в CLI)
    зовёт ``moscow_today`` из своего модуля по глобальному имени.
    """
    monkeypatch.setattr("scripts.utils.dates.moscow_today", lambda: FIXED_TODAY)


# --- Фейки швов --------------------------------------------------------------


class FakeCatalog:
    """Фейк каталога: отдаёт заранее заданный список полей по источнику (FR-2)."""

    def __init__(self, fields: dict[str, list[str]]) -> None:
        self._fields = fields

    def metrica_fields(self, source: str) -> list[str]:
        return self._fields[source]


class FakeClient:
    """Фейк ``MetricaClient``: фиксирует вызовы, отдаёт заданные ответы/бросает ошибки.

    Ответы задаются kwargs по ключам create/evaluate/get_log_request/get_log_requests/
    download/clean/counter_info; ошибка метода — ключом ``<key>_error`` (Exception).
    """

    def __init__(self, **responses: Any) -> None:
        self._responses = responses
        self.calls: list[tuple[str, Any]] = []
        self.token: str | None = None
        self.counter_id: int | None = None

    def _maybe_raise(self, key: str) -> None:
        err = self._responses.get(f"{key}_error")
        if err is not None:
            raise err

    def create_log_request(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_log_request", kwargs))
        self._maybe_raise("create")
        return self._responses.get("create", {})

    def evaluate_log_request(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("evaluate_log_request", kwargs))
        self._maybe_raise("evaluate")
        return self._responses.get("evaluate", {})

    def get_log_request(self, request_id: int) -> dict[str, Any]:
        self.calls.append(("get_log_request", request_id))
        self._maybe_raise("get_log_request")
        return self._responses.get("get_log_request", {})

    def get_log_requests(self) -> list[dict[str, Any]]:
        self.calls.append(("get_log_requests", None))
        self._maybe_raise("get_log_requests")
        return self._responses.get("get_log_requests", [])

    def download_log_request_part(self, request_id: int, part_number: int) -> bytes:
        self.calls.append(("download_log_request_part", (request_id, part_number)))
        self._maybe_raise("download")
        return self._responses.get("download", b"")

    def clean_log_request(self, request_id: int) -> dict[str, Any]:
        self.calls.append(("clean_log_request", request_id))
        self._maybe_raise("clean")
        return self._responses.get("clean", {})

    def get_counter_info(self) -> dict[str, Any]:
        self.calls.append(("get_counter_info", None))
        self._maybe_raise("counter_info")
        return self._responses.get("counter_info", {})

    def calls_of(self, name: str) -> list[Any]:
        return [payload for method, payload in self.calls if method == name]


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: FakeClient,
    fields: dict[str, list[str]] | None = None,
    creds_error: Exception | None = None,
    token: str = "tok-secret-xyz",
    counter_id: int = 42,
) -> None:
    """Подменить три шва CLI на фейки (load_catalog/read_metrica_credentials/MetricaClient).

    Фабрика ``MetricaClient`` фиксирует на ``client`` token/counter_id, которые передал
    CLI — это проверка шва кредов (AC #2). Если ``creds_error`` задан — ридер бросает её
    (AC #4, нет кредов).
    """
    monkeypatch.setattr(cli_mod, "load_catalog", lambda: FakeCatalog(fields or DEFAULT_FIELDS))

    def _read_creds() -> SimpleNamespace:
        if creds_error is not None:
            raise creds_error
        return SimpleNamespace(token=token, counter_id=counter_id)

    monkeypatch.setattr(cli_mod, "read_metrica_credentials", _read_creds)

    def _factory(*, token: str, counter_id: int) -> FakeClient:
        client.token = token
        client.counter_id = counter_id
        return client

    monkeypatch.setattr(cli_mod, "MetricaClient", _factory)


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """Прогнать ``main()`` с заданным argv (через подмену ``sys.argv``)."""
    monkeypatch.setattr(sys, "argv", ["gdau-logs", *argv])
    main()


# --- AC #1: --help перечисляет все подкоманды --------------------------------


def test_help_lists_all_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` → exit 0, перечислены все lifecycle-подкоманды + info (AC #1)."""
    parser = LogsApiCLI()._create_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0

    out = capsys.readouterr().out
    for name in ("create", "status", "download", "clean", "evaluate", "list", "info"):
        assert name in out, f"подкоманда {name!r} не перечислена в --help"


# --- AC #2: create — поля из каталога, clamp date2, креды от ридера -----------


def test_create_uses_catalog_fields_clamp_and_creds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """create: поля из каталога (не из CLI), date2 клампнут, креды от ридера, exit 0 (AC #2)."""
    fake = FakeClient(create={"log_request": {"request_id": 555, "status": "created"}})
    _wire(monkeypatch, client=fake, token="tok-from-reader", counter_id=4242)

    # date2 в будущем (> вчера 2026-05-23) → должен зажаться на 2026-05-23.
    _run(monkeypatch, ["create", "--date1", "2026-05-20", "--date2", "2026-05-30", "--source", "visits"])

    # (a) поля — ровно из каталога (FR-2), а не из CLI (флага --fields нет).
    create_kwargs = fake.calls_of("create_log_request")[0]
    assert create_kwargs["fields"] == DEFAULT_FIELDS["visits"]
    # (b) date2 зажат на «вчера по МСК».
    assert create_kwargs["date2"] == "2026-05-23"
    assert create_kwargs["date1"] == "2026-05-20"
    assert create_kwargs["source"] == "visits"
    # (c) токен/счётчик пришли от read_metrica_credentials через шов клиента.
    assert fake.token == "tok-from-reader"
    assert fake.counter_id == 4242
    # (d) результат напечатан человекочитаемо: id/статус.
    out = capsys.readouterr().out
    assert "Request ID: 555" in out
    assert "Status: created" in out


def test_create_invalid_date_fails_before_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Невалидная дата → exit 1 ДО построения клиента (фейк-клиент не вызван) (AC #2, #4)."""
    fake = FakeClient(create={"log_request": {"request_id": 1}})
    _wire(monkeypatch, client=fake)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["create", "--date1", "2026-13-99", "--date2", "2026-05-20", "--source", "visits"])
    assert exc.value.code == 1
    # Клиент не строился (token не присвоен фабрикой) — падение раньше сети.
    assert fake.token is None
    assert fake.calls == []


def test_create_inverted_range_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Инвертированный диапазон (date1 > date2 после clamp) → exit 1 (AC #2, #4)."""
    fake = FakeClient(create={"log_request": {"request_id": 1}})
    _wire(monkeypatch, client=fake)

    # date2 2026-05-30 → clamp 2026-05-23; date1 2026-05-25 > 2026-05-23 → ValueError.
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["create", "--date1", "2026-05-25", "--date2", "2026-05-30", "--source", "visits"])
    assert exc.value.code == 1
    assert fake.calls == []


# --- AC #3: status/list/clean/evaluate/info проксируют клиента ---------------


def test_status_proxies_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """status зовёт get_log_request(id) и печатает статус/части (AC #3, #5)."""
    fake = FakeClient(
        get_log_request={
            "request_id": 7,
            "status": "processed",
            "date1": "2026-05-01",
            "date2": "2026-05-02",
            "parts": [{"part_number": 0, "size": 1048576}],
        }
    )
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["status", "--request-id", "7"])

    assert fake.calls_of("get_log_request") == [7]
    out = capsys.readouterr().out
    assert "Status: processed" in out
    assert "Частей: 1" in out


def test_list_proxies_and_prints_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """list зовёт get_log_requests и печатает строку на запрос (AC #3, #5)."""
    fake = FakeClient(
        get_log_requests=[
            {"request_id": 1, "status": "processed", "source": "visits",
             "date1": "2026-05-01", "date2": "2026-05-02", "parts": [{"size": 2097152}]},
        ]
    )
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["list"])

    assert fake.calls_of("get_log_requests") == [None]
    out = capsys.readouterr().out
    assert "processed" in out
    assert "visits" in out
    # Строка данных начинается с request_id (left-justify): первый токен == "1".
    data_rows = [ln for ln in out.splitlines() if "processed" in ln]
    assert data_rows and data_rows[0].split()[0] == "1"


def test_status_and_list_tolerate_string_or_null_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``size`` строкой/``null`` (форма реального API не верифицирована) → без трейсбека.

    Регрессия на патч ревью: ``part.get("size", 0)`` подставлял дефолт только при
    ОТСУТСТВИИ ключа; строка/``null`` давали бы ``TypeError`` мимо ``except`` в ``main``
    (трейсбек, против AC #4). Коэрция ``float(... or 0)`` должна это выдержать.
    """
    fake = FakeClient(
        get_log_request={
            "request_id": 3, "status": "processed",
            "date1": "2026-05-01", "date2": "2026-05-02",
            "parts": [{"part_number": 0, "size": "1048576"}, {"part_number": 1, "size": None}],
        }
    )
    _wire(monkeypatch, client=fake)
    _run(monkeypatch, ["status", "--request-id", "3"])  # не должно бросить SystemExit/трейсбек
    assert "Частей: 2" in capsys.readouterr().out

    fake2 = FakeClient(
        get_log_requests=[
            {"request_id": 1, "status": "processed", "source": "visits",
             "date1": "2026-05-01", "date2": "2026-05-02",
             "parts": [{"size": "2097152"}, {"size": None}]},
        ]
    )
    _wire(monkeypatch, client=fake2)
    _run(monkeypatch, ["list"])  # не должно бросить SystemExit/трейсбек
    assert "processed" in capsys.readouterr().out


def test_list_empty_prints_friendly_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пустой список → понятная строка, exit 0 (пусто — не ошибка) (AC #3, #5)."""
    fake = FakeClient(get_log_requests=[])
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["list"])

    out = capsys.readouterr().out
    assert "Нет активных запросов" in out


def test_clean_proxies_and_prints_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """clean зовёт clean_log_request(id), печатает новый статус из log_request (AC #3, #5)."""
    fake = FakeClient(clean={"log_request": {"status": "cleaned_by_user"}})
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["clean", "--request-id", "9"])

    assert fake.calls_of("clean_log_request") == [9]
    out = capsys.readouterr().out
    assert "cleaned_by_user" in out


def test_evaluate_proxies_with_catalog_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """evaluate берёт поля из каталога и печатает possible/max-дней (AC #3, #5, #8)."""
    fake = FakeClient(
        evaluate={"log_request_evaluation": {"possible": True, "max_possible_day_quantity": 40}}
    )
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["evaluate", "--date1", "2026-05-01", "--date2", "2026-05-02", "--source", "hits"])

    eval_kwargs = fake.calls_of("evaluate_log_request")[0]
    assert eval_kwargs["fields"] == DEFAULT_FIELDS["hits"]
    out = capsys.readouterr().out
    assert "Можно создать запрос: True" in out
    assert "40" in out


def test_info_proxies_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """info зовёт get_counter_info и печатает ключевые поля счётчика (AC #1, #3, #5)."""
    fake = FakeClient(counter_info={"counter": {"id": 42, "name": "Test Counter", "status": "Active"}})
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["info"])

    assert fake.calls_of("get_counter_info") == [None]
    out = capsys.readouterr().out
    assert "42" in out
    assert "Test Counter" in out


# --- AC #4: коды возврата / сообщения / нет утечки токена --------------------


def test_missing_creds_exit1_no_token_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Нет кредов → exit 1 + сообщение с именем переменной; токена в выводе нет (AC #4)."""
    fake = FakeClient(create={"log_request": {"request_id": 1}})
    _wire(
        monkeypatch,
        client=fake,
        creds_error=ValueError("Переменная YANDEX_METRICA_TOKEN отсутствует или пуста"),
    )
    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["create", "--date1", "2026-05-01", "--date2", "2026-05-02", "--source", "visits"])
    assert exc.value.code == 1
    assert "YANDEX_METRICA_TOKEN" in caplog.text
    # Падение на ридере — клиент не построен.
    assert fake.token is None


def test_api_error_exit1_with_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка API (RuntimeError из клиента) → exit 1 + сообщение (AC #4)."""
    fake = FakeClient(get_log_request_error=RuntimeError("404 Client Error: not found"))
    _wire(monkeypatch, client=fake)
    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["status", "--request-id", "999"])
    assert exc.value.code == 1
    assert "not found" in caplog.text


# --- AC #5: человекочитаемый вывод, параметра --format нет -------------------


def test_no_format_flag_rejected() -> None:
    """``--format`` не предусмотрен → argparse SystemExit(2) (AC #5)."""
    parser = LogsApiCLI()._create_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--format", "json", "list"])
    assert exc.value.code == 2


def test_no_counter_id_flag_rejected() -> None:
    """``--counter-id`` не предусмотрен (единый источник кредов — .env) (риск #3)."""
    parser = LogsApiCLI()._create_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--counter-id", "1", "list"])
    assert exc.value.code == 2


# --- AC #6: argparse-гард (голый вызов / невалидный source) ------------------


def test_bare_invocation_exit2(capsys: pytest.CaptureFixture[str]) -> None:
    """Голый вызов без подкоманды → SystemExit(2), usage в stderr, без трейсбека (AC #6)."""
    parser = LogsApiCLI()._create_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([])
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_invalid_source_exit2(capsys: pytest.CaptureFixture[str]) -> None:
    """``--source sessions`` (вне choices) → SystemExit(2), usage (AC #6)."""
    parser = LogsApiCLI()._create_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["create", "--date1", "2026-05-01", "--date2", "2026-05-02", "--source", "sessions"])
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


# --- AC #7: ранний / несуществующий download и status -----------------------


def test_download_not_processed_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Статус не 'processed' → exit 1, ни один файл не записан (AC #7)."""
    fake = FakeClient(get_log_request={"status": "created", "parts": []})
    _wire(monkeypatch, client=fake)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["download", "--request-id", "3", "--output", str(tmp_path)])
    assert exc.value.code == 1
    assert list(tmp_path.glob("*.tsv")) == []
    assert fake.calls_of("download_log_request_part") == []


def test_status_not_found_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой ответ get_log_request ({}) → status падает not-found (AC #7)."""
    fake = FakeClient(get_log_request={})
    _wire(monkeypatch, client=fake)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["status", "--request-id", "404"])
    assert exc.value.code == 1


def test_download_not_found_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Пустой ответ get_log_request ({}) → download падает not-found, ничего не пишет (AC #7)."""
    fake = FakeClient(get_log_request={})
    _wire(monkeypatch, client=fake)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["download", "--request-id", "404", "--output", str(tmp_path)])
    assert exc.value.code == 1
    assert list(tmp_path.glob("*.tsv")) == []


def test_download_missing_part_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--part`` нет среди частей → exit 1, ничего не скачано (AC #7)."""
    fake = FakeClient(
        get_log_request={"status": "processed", "parts": [{"part_number": 0, "size": 10}]}
    )
    _wire(monkeypatch, client=fake)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["download", "--request-id", "5", "--part", "5", "--output", str(tmp_path)])
    assert exc.value.code == 1
    assert fake.calls_of("download_log_request_part") == []


# --- AC #8: квота (отказ create) / no-clobber download ----------------------


def test_create_quota_rejected_exit1_no_token_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """create отклонён API (квота) → exit 1 + сообщение; токен не светится (AC #8, NFR-5)."""
    fake = FakeClient(create_error=RuntimeError("quota exceeded"))
    _wire(monkeypatch, client=fake, token="super-secret-token")
    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["create", "--date1", "2026-05-01", "--date2", "2026-05-02", "--source", "visits"])
    assert exc.value.code == 1
    assert "quota" in caplog.text
    captured = capsys.readouterr()
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err
    assert "super-secret-token" not in caplog.text


def test_download_no_clobber_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Целевой файл уже существует → FileExistsError → exit 1, файл не перезаписан (AC #8)."""
    existing = tmp_path / "logs_777_part0.tsv"
    existing.write_bytes(b"ORIGINAL")
    fake = FakeClient(
        get_log_request={"status": "processed", "parts": [{"part_number": 0, "size": 10}]},
        download=b"NEW-DATA",
    )
    _wire(monkeypatch, client=fake)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["download", "--request-id", "777", "--output", str(tmp_path)])
    assert exc.value.code == 1
    # Существующий файл не тронут, скачивание даже не начиналось.
    assert existing.read_bytes() == b"ORIGINAL"
    assert fake.calls_of("download_log_request_part") == []


# --- Happy-path download: запись частей, выбор части, --clean, дефолтный cwd --


def test_download_all_parts_writes_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """download без --part пишет все части в --output с корректным содержимым (AC #3)."""
    fake = FakeClient(
        get_log_request={"status": "processed",
                         "parts": [{"part_number": 0, "size": 8}, {"part_number": 1, "size": 8}]},
        download=b"col1\tcol2\n",
    )
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["download", "--request-id", "5", "--output", str(tmp_path)])

    part0 = tmp_path / "logs_5_part0.tsv"
    part1 = tmp_path / "logs_5_part1.tsv"
    assert part0.read_bytes() == b"col1\tcol2\n"
    assert part1.read_bytes() == b"col1\tcol2\n"
    assert fake.calls_of("download_log_request_part") == [(5, 0), (5, 1)]
    assert "Скачано частей: 2" in capsys.readouterr().out


def test_download_output_with_suffix_uses_parent_dir_and_stem_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--output`` с суффиксом: каталог = parent, префикс = stem (ветка _resolve_output).

    Буквально переданное имя (``result.tsv``) НЕ создаётся — частей может быть
    несколько; даже для одной части файл это ``result_part0.tsv``, а не ``result.tsv``.
    """
    fake = FakeClient(
        get_log_request={"status": "processed", "parts": [{"part_number": 0, "size": 8}]},
        download=b"DATA",
    )
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["download", "--request-id", "9", "--output", str(tmp_path / "result.tsv")])

    assert not (tmp_path / "result.tsv").exists()
    assert (tmp_path / "result_part0.tsv").read_bytes() == b"DATA"
    assert "result_part0.tsv" in capsys.readouterr().out


def test_download_single_part_and_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--part 1 --clean``: качается только часть 1, затем clean (AC #3)."""
    fake = FakeClient(
        get_log_request={"status": "processed",
                         "parts": [{"part_number": 0, "size": 8}, {"part_number": 1, "size": 8}]},
        download=b"DATA",
        clean={"log_request": {"status": "cleaned_by_user"}},
    )
    _wire(monkeypatch, client=fake)

    _run(monkeypatch, ["download", "--request-id", "5", "--part", "1", "--output", str(tmp_path), "--clean"])

    assert not (tmp_path / "logs_5_part0.tsv").exists()
    assert (tmp_path / "logs_5_part1.tsv").read_bytes() == b"DATA"
    assert fake.calls_of("download_log_request_part") == [(5, 1)]
    assert fake.calls_of("clean_log_request") == [5]


def test_download_default_output_is_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Без --output части пишутся в текущий каталог запуска (cwd), без записи в dev-репо."""
    fake = FakeClient(
        get_log_request={"status": "processed", "parts": [{"part_number": 0, "size": 4}]},
        download=b"DATA",
    )
    _wire(monkeypatch, client=fake)
    monkeypatch.chdir(tmp_path)

    _run(monkeypatch, ["download", "--request-id", "8"])

    assert (tmp_path / "logs_8_part0.tsv").read_bytes() == b"DATA"


# --- Анти-зависимость: модуль не тянет тяжёлые либы и инфру directaiq --------


def test_no_forbidden_imports() -> None:
    """Среди реальных import-узлов нет pandas/polars/numpy и инфры directaiq (NFR-6).

    По AST, не по подстроке: docstring модуля упоминает BaseScript/AuthManager/
    config_manager/polars/pandas как «чего НЕ тащим» → наивный поиск дал бы ложный красный.
    """
    source = Path(cli_mod.__file__).read_text(encoding="utf-8")  # type: ignore[arg-type]
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            imported.update(alias.name for alias in node.names)

    # Тяжёлые библиотеки — по top-level пакету (geopandas/typedyaml не дают ложный красный).
    heavy = {"pandas", "polars", "numpy"}
    heavy_offenders = {n for n in imported if n.split(".")[0] in heavy}
    # Инфра directaiq — по сегменту имени модуля (scripts.utils.base_script и т.п.).
    infra = ("base_script", "auth_manager", "config_manager", "logging_utils")
    infra_offenders = {n for n in imported if any(bad in n for bad in infra)}

    assert not heavy_offenders, f"тяжёлые импорты в logs_api_cli: {heavy_offenders}"
    assert not infra_offenders, f"инфра directaiq в logs_api_cli: {infra_offenders}"
