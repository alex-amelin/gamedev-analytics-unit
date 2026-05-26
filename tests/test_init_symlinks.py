"""Offline-тесты декларативного симлинк-контракта + preflight (история 4.1).

Покрывают дисциплину разворачивания, а не только happy-path: валидацию контракта-CSV
(RFC4180 как ``catalog.py``: пустой/дубли/битый заголовок/нет файла/лишняя колонка/хвостовая
пустая строка — AC #1/#8), относительность цели чистым ``os.path.relpath`` (AC #6), ветку
provala preflight детерминированно через ``monkeypatch`` на любой ОС (AC #4), детекцию
отсутствующей цели ДО создания (AC #5), и под capability-gate — реальное создание относительных
симлинков (AC #2/#6), read-through (AC #3), идемпотентную замену (AC #7), fail-loud на реальном
не-симлинке (AC #7-fail) и откат частичного набора при сбое (AC #9). Плюс ast-анти-зависимость:
модуль знает только ФС + контракт, без ``duckdb``/``paths``/``database_manager``/тяжёлого стека
(риск №1).

Кросс-платформенно (``tmp_path``/``pathlib``), CI ubuntu + windows. Реальные симлинк-тесты
гейтятся пробой ``preflight_symlink_capability`` (Windows без Developer Mode → ``skip``, не
красный) — реальное покрытие AC #2/#6/#7/#9 даёт ubuntu-прогон (симлинки нативны). Live-набор
осознанно отсутствует: 4.1 — ФС-операции, без внешнего API ([[realapi-smoke-tests]] — opt-in
live только для Logs API), как 2.1–2.6.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from scripts.init.symlinks import (
    DEFAULT_CONTRACT_PATH,
    SymlinkContractError,
    SymlinkError,
    SymlinkPreflightError,
    SymlinkTargetMissingError,
    _relative_target,
    create_symlinks,
    load_symlink_contract,
    preflight_symlink_capability,
)

# Типовой валидный контракт: две стабильные цели (каталог + файл).
_VALID_CONTRACT = "path,comment\nscripts,код юнита\npyproject.toml,манифест\n"


def _write_contract(tmp_path: Path, body: str) -> Path:
    """Записать контракт-CSV (LF, utf-8) и вернуть путь — инъектируемый шов ``contract_path``."""
    path = tmp_path / "paths-to-symlink.csv"
    path.write_text(body, encoding="utf-8", newline="")
    return path


# --- AC #1, #8: чистая валидация контракта (всегда идёт, без симлинков) -----------------


def test_load_valid_contract_preserves_order(tmp_path: Path) -> None:
    """Валидный контракт → список path в порядке файла (AC #1)."""
    path = _write_contract(tmp_path, _VALID_CONTRACT)
    assert load_symlink_contract(path) == ["scripts", "pyproject.toml"]


def test_comment_with_comma_is_quoted_not_split(tmp_path: Path) -> None:
    """Запятая в закавыченном comment не рвёт парсинг (RFC4180, НЕ str.split) (AC #1)."""
    path = _write_contract(
        tmp_path, 'path,comment\nscripts,"код, каталог, init — один источник"\n'
    )
    assert load_symlink_contract(path) == ["scripts"]


def test_trailing_blank_line_is_not_a_record(tmp_path: Path) -> None:
    """Хвостовая пустая строка LF-файла не валит валидатор «пустой path» (DictReader её не отдаёт).

    Типовой LF-случай: файл оканчивается ``\\n`` (и допустима лишняя пустая строка). csv не
    считает её записью → «пустой path» о неё не спотыкается (У3 ревью).
    """
    path = _write_contract(tmp_path, "path,comment\nscripts,код\n\n")
    assert load_symlink_contract(path) == ["scripts"]


def test_extra_unquoted_column_fails_loud(tmp_path: Path) -> None:
    """Лишняя незакавыченная колонка (сдвиг полей) → SymlinkContractError (restkey-страж) (AC #8)."""
    path = _write_contract(tmp_path, "path,comment\nscripts,один,два\n")
    with pytest.raises(SymlinkContractError, match="лишн|больше"):
        load_symlink_contract(path)


def test_empty_contract_fails_loud(tmp_path: Path) -> None:
    """Только заголовок, ноль записей → SymlinkContractError (вырожденный SSOT) (AC #8)."""
    path = _write_contract(tmp_path, "path,comment\n")
    with pytest.raises(SymlinkContractError, match="пуст"):
        load_symlink_contract(path)


def test_duplicate_path_fails_loud(tmp_path: Path) -> None:
    """Дубль path → SymlinkContractError (коллизия симлинка) (AC #8)."""
    path = _write_contract(tmp_path, "path,comment\nscripts,a\nscripts,b\n")
    with pytest.raises(SymlinkContractError, match="[Дд]убль"):
        load_symlink_contract(path)


def test_bad_header_fails_loud(tmp_path: Path) -> None:
    """Дрейф заголовка (path,note) → SymlinkContractError (AC #8)."""
    path = _write_contract(tmp_path, "path,note\nscripts,a\n")
    with pytest.raises(SymlinkContractError, match="[Зз]аголов"):
        load_symlink_contract(path)


def test_missing_file_fails_loud(tmp_path: Path) -> None:
    """Нет файла / битый симлинк-путь → SymlinkContractError с путём (AC #8)."""
    missing = tmp_path / "нет-такого.csv"
    with pytest.raises(SymlinkContractError, match="не найден"):
        load_symlink_contract(missing)


def test_blank_path_fails_loud(tmp_path: Path) -> None:
    """Пустой/пробельный path → SymlinkContractError (путь без записи = дефект) (AC #8)."""
    path = _write_contract(tmp_path, "path,comment\n   ,описание\n")
    with pytest.raises(SymlinkContractError, match="[Пп]уст"):
        load_symlink_contract(path)


def test_absolute_path_fails_loud(tmp_path: Path) -> None:
    """Абсолютный path → SymlinkContractError (увёл бы линк/цель за пределы репо) (AC #8).

    Path.__truediv__ при абсолютном rel отбросил бы dev_repo_root/storage_root — гард ловит.
    `/etc/passwd` абсолютен и на POSIX, и на Windows (ведущий слеш) → кросс-платформенно.
    """
    path = _write_contract(tmp_path, "path,comment\n/etc/passwd,абсолют\n")
    with pytest.raises(SymlinkContractError, match="относительн"):
        load_symlink_contract(path)


def test_parent_traversal_path_fails_loud(tmp_path: Path) -> None:
    """path с '..' → SymlinkContractError (traversal за пределы хранилища) (AC #8)."""
    path = _write_contract(tmp_path, "path,comment\n../../secret,traversal\n")
    with pytest.raises(SymlinkContractError, match="относительн"):
        load_symlink_contract(path)


# --- AC #6: относительная цель — чистый os.path.relpath, без симлинков ------------------


def test_relative_target_is_not_absolute(tmp_path: Path) -> None:
    """_relative_target для соседнего хранилища даёт ОТНОСИТЕЛЬНУЮ цель, не абсолютную (AC #6).

    Раскладка «хранилище — сосед dev-репо»: storage_root = dev_repo_root.parent / 'game1'.
    Линк game1/scripts → ../<dev-репо>/scripts. Проверяем именно ``not os.path.isabs`` (а не
    строковое равенство — разделители ОС и Windows-префиксы разъедутся).
    """
    dev_repo_root = tmp_path / "gamedev-analytics-unit"
    storage_root = tmp_path / "game1"
    target = _relative_target(dev_repo_root, storage_root, "scripts")
    assert not os.path.isabs(target)
    # Цель ведёт «вверх и в dev-репо» — содержит имя каталога dev-репо.
    assert "gamedev-analytics-unit" in target
    assert "scripts" in target


# --- AC #4: ветка provala preflight (детерминированно, любая ОС) ------------------------


def test_preflight_fails_loud_when_symlink_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.symlink бросает OSError (winerror 1314) → SymlinkPreflightError с инструкцией Dev Mode (AC #4).

    Детерминированно на любой ОС через monkeypatch; после вызова проба за собой не оставляет
    файлов (убрана в finally).
    """

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "ERROR_PRIVILEGE_NOT_HELD (смоделировано)")

    monkeypatch.setattr(os, "symlink", _boom)

    with pytest.raises(SymlinkPreflightError, match="Developer Mode|Режим разработчика"):
        preflight_symlink_capability(probe_dir=tmp_path)

    # Проба убрана в finally — ни цели, ни линка не осталось.
    assert list(tmp_path.iterdir()) == []


# --- AC #5: цель из контракта отсутствует в dev-репо → fail-loud ДО создания ------------


def test_missing_target_fails_before_creating_anything(tmp_path: Path) -> None:
    """Цель контракта отсутствует в dev_repo_root → SymlinkTargetMissingError, ноль симлинков (AC #5).

    run_preflight=False — тестируем именно предвалидацию целей, без зависимости от способности
    платформы создавать симлинки (детерминированно на любой ОС).
    """
    dev_repo_root = tmp_path / "dev"
    dev_repo_root.mkdir()
    (dev_repo_root / "scripts").mkdir()  # есть
    storage_root = tmp_path / "game"
    storage_root.mkdir()
    # Контракт называет «нет-такой-цели», которой нет в dev-репо.
    contract = _write_contract(
        tmp_path, "path,comment\nscripts,есть\nнет-такой-цели,битая запись\n"
    )

    with pytest.raises(SymlinkTargetMissingError, match="отсутствует"):
        create_symlinks(
            dev_repo_root=dev_repo_root,
            storage_root=storage_root,
            contract_path=contract,
            run_preflight=False,
        )

    # Предвалидация до создания — ни одного симлинка не появилось (даже для существующей цели).
    assert list(storage_root.iterdir()) == []


def test_cross_drive_relpath_wrapped_in_symlink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-drive os.path.relpath (разные диски Windows) → доменный SymlinkError, не сырой ValueError.

    На Windows os.path.relpath бросает ValueError, если корни на разных дисках (dev-репо G:\\ vs
    хранилище C:\\) — относительный симлинк между дисками невозможен. Падать правильно, но
    fail-loud доменной ошибкой с инструкцией про общий диск, не криптическим stdlib-исключением.
    monkeypatch детерминирует поведение на любой ОС; run_preflight=False — гард срабатывает до
    os.symlink, способность создавать симлинки не нужна.
    """
    dev_repo_root = tmp_path / "dev"
    dev_repo_root.mkdir()
    (dev_repo_root / "scripts").mkdir()  # цель существует — предвалидация пройдёт
    storage_root = tmp_path / "game"
    contract = _write_contract(tmp_path, "path,comment\nscripts,код\n")

    def _cross_drive(*args: object, **kwargs: object) -> str:
        raise ValueError("path is on mount 'C:', start on mount 'G:' (смоделировано)")

    monkeypatch.setattr(os.path, "relpath", _cross_drive)

    with pytest.raises(SymlinkError, match="разн|диск"):
        create_symlinks(
            dev_repo_root=dev_repo_root,
            storage_root=storage_root,
            contract_path=contract,
            run_preflight=False,
        )


# --- Shipped-контракт dev-репо валиден (D5: 4 существующие стабильные цели) -------------


def test_shipped_contract_loads_and_targets_exist() -> None:
    """Реальный templates/paths-to-symlink.csv грузится и все его цели существуют в dev-репо (AC #1, #5).

    Страж от преждевременных записей: если бы shipped-CSV называл несуществующую цель, gdau-init
    (4.3) упал бы SymlinkTargetMissingError. Состав финализирован в 4.3 (D11, решение Шефа): к
    4 стабильным целям добавлены `uv.lock` (нужен для `uv sync --frozen` в хранилище) и `.mcp.json`
    (канал чтения игры — Epic 3 влит, артефакт существует); `.claude` отложен (dev-скилы, не рантайм).
    """
    rel_paths = load_symlink_contract()  # дефолтный путь = DEFAULT_CONTRACT_PATH
    assert rel_paths == [
        "scripts",
        "development-docs",
        "yandex-docs",
        "pyproject.toml",
        "uv.lock",
        ".mcp.json",
    ]
    dev_repo_root = DEFAULT_CONTRACT_PATH.resolve().parents[1]
    for rel in rel_paths:
        assert (dev_repo_root / rel).exists(), f"shipped-цель отсутствует: {rel}"


# --- Capability-gate: реальное создание симлинков (skip без Dev Mode) -------------------


@pytest.fixture
def symlink_capable() -> None:
    """Гейт реальных симлинк-тестов: нет способности (Windows без Dev Mode) → skip, не красный."""
    try:
        preflight_symlink_capability()
    except SymlinkPreflightError:
        pytest.skip("нет способности создавать симлинки (Windows без Developer Mode)")


def _dev_repo_with_targets(tmp_path: Path) -> Path:
    """Собрать dev-репо с реальными целями: scripts/ (каталог) + pyproject.toml (файл)."""
    dev_repo_root = tmp_path / "dev"
    (dev_repo_root / "scripts").mkdir(parents=True)
    (dev_repo_root / "scripts" / "marker.txt").write_text("я в dev-репо", encoding="utf-8")
    (dev_repo_root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return dev_repo_root


def test_creates_relative_symlinks_and_read_through(
    tmp_path: Path, symlink_capable: None
) -> None:
    """Создаёт реальные ОТНОСИТЕЛЬНЫЕ симлинки; чтение сквозь линк отдаёт dev-репо (AC #2, #3, #6)."""
    dev_repo_root = _dev_repo_with_targets(tmp_path)
    storage_root = tmp_path / "game"
    contract = _write_contract(
        tmp_path, "path,comment\nscripts,код\npyproject.toml,манифест\n"
    )

    created = create_symlinks(
        dev_repo_root=dev_repo_root, storage_root=storage_root, contract_path=contract
    )
    assert sorted(p.name for p in created) == ["pyproject.toml", "scripts"]

    for rel in ("scripts", "pyproject.toml"):
        link = storage_root / rel
        assert link.is_symlink()
        # Относительность: цель в os.readlink НЕ абсолютна (не сравниваем строки — ОС-разделители).
        assert not os.path.isabs(os.readlink(link))

    # AC #3: правка инструмента в dev-репо видна сквозь ссылку (один источник истины).
    assert (storage_root / "scripts" / "marker.txt").read_text(encoding="utf-8") == "я в dev-репо"
    assert (storage_root / "scripts").is_dir()  # dir-симлинк резолвится в каталог


def test_idempotent_rerun_and_replaces_wrong_target(
    tmp_path: Path, symlink_capable: None
) -> None:
    """Повторный вызов не падает FileExistsError; существующий симлинк приводится к контракту (AC #7)."""
    dev_repo_root = _dev_repo_with_targets(tmp_path)
    storage_root = tmp_path / "game"
    contract = _write_contract(tmp_path, "path,comment\npyproject.toml,манифест\n")

    # Подложить симлинк с «неправильной» целью по адресу будущего линка.
    storage_root.mkdir()
    wrong = dev_repo_root / "pyproject.toml"  # любая существующая цель, но создадим иной линк
    bogus_target = tmp_path / "не-та-цель.txt"
    bogus_target.write_text("мимо", encoding="utf-8")
    os.symlink(os.path.relpath(bogus_target, storage_root), storage_root / "pyproject.toml")

    # Первый «контрактный» вызов — замена кривого симлинка на правильный.
    create_symlinks(
        dev_repo_root=dev_repo_root, storage_root=storage_root, contract_path=contract
    )
    link = storage_root / "pyproject.toml"
    assert link.is_symlink()
    assert link.resolve() == wrong.resolve()  # теперь указывает на dev-репо

    # Повторный вызов на готовом наборе не падает FileExistsError (идемпотентность).
    create_symlinks(
        dev_repo_root=dev_repo_root, storage_root=storage_root, contract_path=contract
    )
    assert (storage_root / "pyproject.toml").resolve() == wrong.resolve()


def test_real_nonsymlink_in_place_fails_loud_and_kept(
    tmp_path: Path, symlink_capable: None
) -> None:
    """Реальный файл по адресу линка → SymlinkError, файл НЕ удалён (НЕ rm -rf) (AC #7-fail)."""
    dev_repo_root = _dev_repo_with_targets(tmp_path)
    storage_root = tmp_path / "game"
    storage_root.mkdir()
    # Реальный файл по адресу будущего линка pyproject.toml.
    real_file = storage_root / "pyproject.toml"
    real_file.write_text("мои данные — не трогать", encoding="utf-8")
    contract = _write_contract(tmp_path, "path,comment\npyproject.toml,манифест\n")

    with pytest.raises(SymlinkError, match="реальный файл|отказ удалять"):
        create_symlinks(
            dev_repo_root=dev_repo_root, storage_root=storage_root, contract_path=contract
        )

    # Реальный файл на месте, содержимое цело (риск №4 — не rm -rf).
    assert not real_file.is_symlink()
    assert real_file.read_text(encoding="utf-8") == "мои данные — не трогать"


def test_partial_failure_rolls_back_created_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink_capable: None
) -> None:
    """Сбой создания (K+1)-го симлинка → откат созданных до сбоя; частичного набора нет (AC #9).

    run_preflight=False КРИТИЧНО: иначе проба preflight сама зовёт os.symlink и съест первый
    расход счётчика обёртки, сдвинув арифметику «первых K» (К1 ревью).
    """
    dev_repo_root = tmp_path / "dev"
    dev_repo_root.mkdir()
    for name in ("a", "b", "c"):  # три реальных цели-каталога — предвалидация пройдёт
        (dev_repo_root / name).mkdir()
    storage_root = tmp_path / "game"
    contract = _write_contract(tmp_path, "path,comment\na,1\nb,2\nc,3\n")

    real_symlink = os.symlink
    calls = {"n": 0}

    def _flaky(src: object, dst: object, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:  # passthrough для первого (K=1), сбой на втором
            raise OSError("симлинк сорвался (смоделировано)")
        real_symlink(src, dst, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "symlink", _flaky)

    with pytest.raises((SymlinkError, OSError)):
        create_symlinks(
            dev_repo_root=dev_repo_root,
            storage_root=storage_root,
            contract_path=contract,
            run_preflight=False,
        )

    # Созданный до сбоя линк 'a' откатан; 'b'/'c' не появились — частичного набора нет.
    for name in ("a", "b", "c"):
        assert not (storage_root / name).is_symlink()
        assert not (storage_root / name).exists()


# --- Анти-зависимость: модуль знает только ФС + контракт (риск №1) ----------------------


def test_no_heavy_or_forbidden_infra_imported() -> None:
    """Нет import duckdb/paths/database_manager/тяжёлого стека/directaiq-инфры (риск №1).

    Не по подстроке (docstring модуля упоминает duckdb/paths) — парсим AST и смотрим реальные
    import-узлы. Модуль независим: знает только ФС (os/pathlib/shutil/tempfile) + контракт (csv).
    """
    import scripts.init.symlinks as mod

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

    forbidden_roots = {"pandas", "polars", "numpy", "pyarrow", "config_manager", "base_script", "duckdb"}
    forbidden_full = {"scripts.utils.paths", "scripts.utils.database_manager"}
    offenders = {
        n for n in imported if n.split(".")[0] in forbidden_roots or n in forbidden_full
    }
    assert not offenders, f"запрещённые импорты в symlinks.py: {offenders}"
