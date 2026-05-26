"""Offline-тесты шаблона хранилища и примитива копирования (история 4.2).

Покрывают две части истории (параллель с 4.1: статический артефакт + примитив-потребитель):

- **Контент статического шаблона** ``templates/external_storage/`` (AC #1, #4): четыре
  обязательных файла на месте; ``.env.example`` несёт обе переменные кредов без вписанных
  значений (страж «не закоммитили секрет» — по конкретным строкам-переменным, а не по всем,
  чтобы комментарии не валили проверку); ``.gitignore`` игнорит секреты/данные/симлинк-пути и
  хранит ``!.env.example``; ``PROJECT.md`` — очевидная болванка с плейсхолдером; **страж
  урезанности** (нет ``.claude/``, нет directaiq/marketing-маркеров — кейс-инсенситивно).
- **Примитив** :func:`copy_storage_template` на ``tmp_path`` (AC #2, #5, #6): копирование
  четырёх файлов; fail-loud **ДО** мутаций при отсутствии/битости шаблона (``storage_root`` не
  создаётся); сохранение заполненного владельцем ``PROJECT.md`` при повторном init.

Анти-зависимость — по реальным import-узлам (``ast``, не подстрока, как ``test_parquet_store``):
``scaffold`` знает только ФС + ``shutil``, без ``paths``/``database_manager``/``duckdb``/
``symlinks``/тяжёлого стека. Live-набор осознанно отсутствует: 4.2 — чистые ФС-операции,
без внешнего API ([[realapi-smoke-tests]] — opt-in live только для Logs API).

Без сети и БД. Корни (``storage_root``/``template_root``) инъектируются на ``tmp_path``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.init.scaffold import (
    DEFAULT_TEMPLATE_ROOT,
    PRESERVE_ON_REPEAT,
    REQUIRED_TEMPLATE_FILES,
    StorageTemplateError,
    copy_storage_template,
)

# Маркеры контента directaiq/маркетинга, которых в урезанном под геймдев шаблоне быть НЕ должно
# (AC #4). Кейс-инсенситивно: цель — поймать случайный вендоринг directaiq-наполнения.
_DIRECTAIQ_MARKERS = (
    "direct_cli",
    "wordstat",
    "prophet",
    "appmetrica",
    "client_context",
    "marketing-intelligence",
)


def _make_template(root: Path) -> dict[str, str]:
    """Собрать на ``root`` минимальный валидный шаблон (4 файла) с уникальным контентом.

    Возвращает отображение имя→контент для последующей сверки идентичности копии.
    """
    root.mkdir(parents=True, exist_ok=True)
    content = {
        ".env.example": "YANDEX_METRICA_TOKEN=\nYANDEX_METRICA_COUNTER_ID=\n",
        ".gitignore": ".env\ndata/\n.writer.lock\n!.env.example\n",
        "CLAUDE.md": "# рабочее пространство игры (тест)\n",
        "PROJECT.md": "<!-- заполни: название игры -->\n",
    }
    for name, text in content.items():
        (root / name).write_text(text, encoding="utf-8")
    return content


# --- Контент реального шаблона (AC #1) — всегда идут -----------------------------------


def test_real_template_has_required_files() -> None:
    """Все 4 обязательных файла существуют в ``templates/external_storage/`` (AC #1)."""
    assert DEFAULT_TEMPLATE_ROOT.is_dir(), f"нет каталога шаблона: {DEFAULT_TEMPLATE_ROOT}"
    for name in REQUIRED_TEMPLATE_FILES:
        assert (DEFAULT_TEMPLATE_ROOT / name).is_file(), f"нет файла шаблона: {name}"


def test_real_env_example_carries_both_vars_without_secrets() -> None:
    """``.env.example`` несёт обе переменные кредов с ПУСТЫМ значением (AC #1, NFR-5).

    Страж «не закоммитили секрет» — по конкретным строкам-переменным (``startswith``),
    а не по всем строкам: комментарий вида ``# … = …`` не должен ложно валить проверку.
    """
    text = (DEFAULT_TEMPLATE_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "YANDEX_METRICA_TOKEN" in text
    assert "YANDEX_METRICA_COUNTER_ID" in text
    seen_token = seen_counter = False
    for line in text.splitlines():
        if line.startswith("YANDEX_METRICA_TOKEN="):
            seen_token = True
            assert line.split("=", 1)[1].strip() == "", "в .env.example вписан секрет токена"
        if line.startswith("YANDEX_METRICA_COUNTER_ID="):
            seen_counter = True
            assert line.split("=", 1)[1].strip() == "", "в .env.example вписан счётчик"
    assert seen_token and seen_counter, "нет строк-переменных вида YANDEX_METRICA_*="


def test_real_gitignore_ignores_secrets_data_and_symlinks() -> None:
    """``.gitignore`` игнорит секреты/данные/`.writer.lock`, хранит `!.env.example`,
    перечисляет симлинк-пути инфры (AC #1, решение D3)."""
    text = (DEFAULT_TEMPLATE_ROOT / ".gitignore").read_text(encoding="utf-8")
    for needle in (".env", "data/", ".writer.lock", "!.env.example"):
        assert needle in text, f".gitignore не игнорит {needle!r}"
    # Симлинкуемые пути инфры (синхрон с templates/paths-to-symlink.csv 4.1):
    for needle in ("scripts", "development-docs", "yandex-docs", "pyproject.toml"):
        assert needle in text, f".gitignore не перечисляет симлинк-путь {needle!r}"


def test_real_project_md_is_nonempty_placeholder() -> None:
    """``PROJECT.md`` непустой и содержит маркер-плейсхолдер болванки (AC #2, FR-21)."""
    text = (DEFAULT_TEMPLATE_ROOT / "PROJECT.md").read_text(encoding="utf-8")
    assert text.strip(), "PROJECT.md пуст"
    assert "заполни" in text.lower() or "<!--" in text, "нет маркера-плейсхолдера"


def test_real_template_is_trimmed_no_directaiq() -> None:
    """Шаблон урезан под геймдев: нет ``.claude/`` и directaiq/marketing-маркеров (AC #4)."""
    assert not (DEFAULT_TEMPLATE_ROOT / ".claude").exists(), "в шаблоне есть .claude/"
    for name in REQUIRED_TEMPLATE_FILES:
        lowered = (DEFAULT_TEMPLATE_ROOT / name).read_text(encoding="utf-8").lower()
        for marker in _DIRECTAIQ_MARKERS:
            assert marker not in lowered, f"в {name} найден directaiq-маркер {marker!r}"


# --- Примитив copy_storage_template на tmp_path (AC #2, #5, #6) -------------------------


def test_copy_creates_all_files_on_fresh_storage(tmp_path: Path) -> None:
    """AC #2: копирование в несуществующий ``storage_root`` создаёт его и все 4 файла."""
    template_root = tmp_path / "template"
    content = _make_template(template_root)
    storage_root = tmp_path / "game"
    assert not storage_root.exists()

    copied = copy_storage_template(storage_root=storage_root, template_root=template_root)

    assert storage_root.is_dir()
    assert len(copied) == len(REQUIRED_TEMPLATE_FILES)
    for name, expected in content.items():
        dest = storage_root / name
        assert dest.is_file(), f"не скопирован {name}"
        assert dest.read_text(encoding="utf-8") == expected
    # Возвращённые пути указывают на скопированные файлы в storage_root.
    assert {p.name for p in copied} == set(content)


def test_missing_template_dir_fails_before_mutations(tmp_path: Path) -> None:
    """AC #5: нет каталога шаблона → ``StorageTemplateError`` ДО создания ``storage_root``."""
    template_root = tmp_path / "no-such-template"
    storage_root = tmp_path / "game"
    with pytest.raises(StorageTemplateError):
        copy_storage_template(storage_root=storage_root, template_root=template_root)
    assert not storage_root.exists(), "storage_root создан до валидации (нарушение AC #5)"


def test_incomplete_template_fails_before_mutations(tmp_path: Path) -> None:
    """AC #5: не хватает обязательного файла → fail-loud, ``storage_root`` не создан/пуст."""
    template_root = tmp_path / "template"
    _make_template(template_root)
    (template_root / "PROJECT.md").unlink()  # битый шаблон: нет обязательного файла
    storage_root = tmp_path / "game"
    with pytest.raises(StorageTemplateError):
        copy_storage_template(storage_root=storage_root, template_root=template_root)
    assert not storage_root.exists(), "storage_root создан при битом шаблоне (нарушение AC #5)"


def test_repeat_init_preserves_filled_project_md(tmp_path: Path) -> None:
    """AC #6: заполненный владельцем ``PROJECT.md`` не затирается; остальные обновляются."""
    template_root = tmp_path / "template"
    content = _make_template(template_root)
    storage_root = tmp_path / "game"
    storage_root.mkdir()
    owner_text = "# Игра «Кысь»\n\nплатформа: web; счётчик 12345\n"
    (storage_root / "PROJECT.md").write_text(owner_text, encoding="utf-8")

    copied = copy_storage_template(storage_root=storage_root, template_root=template_root)

    # PROJECT.md в PRESERVE_ON_REPEAT → текст владельца сохранён.
    assert "PROJECT.md" in PRESERVE_ON_REPEAT
    assert (storage_root / "PROJECT.md").read_text(encoding="utf-8") == owner_text
    # Прочие служебные файлы обновлены из шаблона.
    for name in (".env.example", ".gitignore", "CLAUDE.md"):
        assert (storage_root / name).read_text(encoding="utf-8") == content[name]
    # Контракт возврата: пропущенный PROJECT.md НЕ входит в copied (докстринг copy_storage_template).
    assert "PROJECT.md" not in {p.name for p in copied}
    assert len(copied) == len(REQUIRED_TEMPLATE_FILES) - 1  # 3 файла: все, кроме сохранённого


def test_smoke_copy_real_template(tmp_path: Path) -> None:
    """Smoke: копирование реального шаблона (дефолт ``template_root=None``) не падает."""
    storage_root = tmp_path / "real"
    copied = copy_storage_template(storage_root=storage_root)  # дефолт = DEFAULT_TEMPLATE_ROOT
    assert len(copied) == len(REQUIRED_TEMPLATE_FILES)
    for name in REQUIRED_TEMPLATE_FILES:
        assert (storage_root / name).is_file()


def test_oserror_during_copy_wrapped_in_storage_template_error(tmp_path: Path) -> None:
    """D6/DoD #7: сырой ``OSError`` от ``mkdir``/``shutil`` оборачивается в ``StorageTemplateError``.

    Провоцируем тем, что ``storage_root`` указывает на уже существующий ФАЙЛ (не каталог):
    ``storage_root.mkdir(exist_ok=True)`` бросает ``FileExistsError`` (подкласс ``OSError``).
    Шаблон при этом валиден → ошибка возникает на фазе мутаций и должна быть обёрнута в
    доменное исключение, а не утечь сырой наружу (паттерн ревью 2.1/4.1). Заодно покрывает
    край «``storage_root`` — существующий файл, а не каталог».
    """
    template_root = tmp_path / "template"
    _make_template(template_root)
    storage_root = tmp_path / "game"
    storage_root.write_text("я файл, а не папка\n", encoding="utf-8")  # цель занята файлом

    with pytest.raises(StorageTemplateError):
        copy_storage_template(storage_root=storage_root, template_root=template_root)


# --- Анти-зависимость: только ФС + shutil, без paths/duckdb/symlinks/тяжёлого стека -----


def test_no_forbidden_imports() -> None:
    """``scaffold.py`` импортирует только stdlib ФС-примитивы (ast по import-узлам).

    Не по подстроке (docstring модуля упоминает соседей по границам) — парсим AST и смотрим
    реальные import-узлы по корню имени. Фиксирует риск №1: модуль знает только ФС + shutil.
    """
    import scripts.init.scaffold as mod

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
        "duckdb",
        "requests",
        "config_manager",
        "base_script",
    }
    # Запрещены и внутренние модули проекта вне stdlib-границы 4.2 (риск №1).
    forbidden_prefixes = (
        "scripts.utils.paths",
        "scripts.utils.database_manager",
        "scripts.utils.metrica_client",
        "scripts.init.symlinks",
    )
    offenders = {n for n in imported if n.split(".")[0] in forbidden}
    offenders |= {n for n in imported if n.startswith(forbidden_prefixes)}
    assert not offenders, f"запрещённые импорты в scaffold: {offenders}"
