"""Декларативный симлинк-контракт dev-репо↔хранилище + preflight + создание ссылок.

Роль. При разворачивании per-game хранилища (Epic 4) этот модуль связывает папку игры
с инструментами юнита dev-репо ПО ЯВНОМУ СПИСКУ (``templates/paths-to-symlink.csv``):
код ``scripts/``, каталог схемы ``development-docs/``, справочники ``yandex-docs/``,
манифест ``pyproject.toml``, лок зависимостей ``uv.lock`` и конфиг MCP-сервера
``.mcp.json`` (состав финализирован в 4.3) приходят в хранилище ОТНОСИТЕЛЬНЫМИ ссылками,
а не копиями (FR-20). Так фикс/обновление в dev-репо сразу видно всем играм без копий и
рассинхрона, а перенос папки между Windows/Linux копированием не рвёт ссылки (NFR-2).

Три операции:
- :func:`load_symlink_contract` — прочитать и провалидировать контракт-CSV (SSOT,
  RFC4180 через ``csv.DictReader`` как ``catalog.py``);
- :func:`preflight_symlink_capability` — проверить, что платформа вообще умеет создавать
  симлинки (Windows без Developer Mode — нет), fail-loud с инструкцией ДО любых мутаций;
- :func:`create_symlinks` — создать относительные ссылки по контракту с предвалидацией
  целей (битый контракт падает «насухо») и откатом частичного набора при сбое (TOCTOU).

Границы (что НЕ здесь). Резолюция корня хранилища из ``GDAU_DATA_ROOT`` — забота 4.3:
корни ``dev_repo_root``/``storage_root`` приходят ПАРАМЕТРАМИ (шов инъекции, как ``conn``/
``path`` в 2.x). Копирование шаблона хранилища и ``PROJECT.md`` — 4.2. Оркестрация полного
init (имя → шаблон → ссылки → ``.env`` → ``uv sync`` → ``gdau.duckdb`` → ``git init``) и откат
ВСЕГО хранилища — 4.3. Модуль в сеть не ходит, ``gdau.duckdb`` не открывает, данные/партиции
не пишет, ``.writer.lock`` не берёт, TSV не парсит — только ФС-операции над контрактом и
симлинками. Зависит лишь от stdlib; НЕ импортирует ``paths``/``database_manager``/``duckdb``/
``metrica_client``/``parquet_store``.
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path, PureWindowsPath

logger = logging.getLogger(__name__)

# Точный ожидаемый заголовок контракта (форма directaiq templates/paths-to-symlink.csv):
# `path` — путь относительно корня (одинаков для dev-репо и хранилища); `comment` —
# человекочитаемое назначение (может содержать запятые → обязателен RFC4180-парсинг).
# Валидация на этот кортеж ловит дрейф колонок fail-loud (как catalog.CATALOG_COLUMNS).
CONTRACT_COLUMNS = ("path", "comment")

# Ключ для значений сверх заголовка: csv.DictReader складывает избыток в список под restkey.
# Явная строка (а не дефолтный None) → строка с лишней незакавыченной колонкой (сдвиг полей,
# напр. незакавыченная запятая в comment) детектируется fail-loud, а не теряет хвост молча.
# Зеркало catalog._RESTKEY. Имя нарочно «невозможное» для колонки контракта.
_RESTKEY = "__extra_columns__"

# Дефолтный путь к контракту резолвится ОТ МОДУЛЯ, а не от cwd: контракт — артефакт dev-репо
# (`templates/`), он путешествует с кодом (в per-game хранилище приходит симлинком), а НЕ
# данные оператора. `.resolve()` проходит сквозь симлинк хранилища в dev-репо, где `templates/`
# и `scripts/` — реальные соседи. parents[2]: symlinks.py → init → scripts → корень репо.
# Зеркало catalog.DEFAULT_CATALOG_PATH.
DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "paths-to-symlink.csv"
)

__all__ = [
    "SymlinkContractError",
    "SymlinkTargetMissingError",
    "SymlinkPreflightError",
    "SymlinkError",
    "CONTRACT_COLUMNS",
    "DEFAULT_CONTRACT_PATH",
    "load_symlink_contract",
    "preflight_symlink_capability",
    "create_symlinks",
]


class SymlinkContractError(ValueError):
    """Дефект контракта-CSV: нет файла / битый заголовок / дубли / пустой path / пустой контракт.

    Наследует :class:`ValueError` — дефект данных контракта (как ``catalog`` для каталога схемы).
    """


class SymlinkTargetMissingError(SymlinkContractError):
    """Цель из контракта отсутствует в dev-репо (битый контракт, AC #5).

    Подкласс :class:`SymlinkContractError` — частный случай дефекта контракта (цель названа,
    но её нет), отдельный класс для прицельной ловли в тестах/4.3.
    """


class SymlinkPreflightError(RuntimeError):
    """Платформа не умеет создавать симлинки (Windows без Developer Mode, AC #4).

    Наследует :class:`RuntimeError` — инцидент окружения, не дефект данных. Сообщение несёт
    инструкцию включить Developer Mode.
    """


class SymlinkError(RuntimeError):
    """Сбой создания/замены конкретного симлинка (AC #7-fail / AC #9).

    Обёртка сырого :class:`OSError` от ``os.symlink``; также — реальный не-симлинк по пути линка
    (отказ удалять) и сбой посреди цикла (перед откатом). Сырой ``OSError`` наружу не выпускаем.
    """


def load_symlink_contract(contract_path: Path | None = None) -> list[str]:
    """Прочитать и провалидировать декларативный контракт симлинков из CSV (fail-loud, AC #1, #8).

    ``contract_path`` — инъектируемый шов: тесты передают мини-фикстуру, в проде берётся
    :data:`DEFAULT_CONTRACT_PATH`. Парсинг — через :class:`csv.DictReader` (RFC4180, НЕ
    ``str.split(",")``): описания в ``comment`` могут содержать запятые. Валидирует: заголовок
    == :data:`CONTRACT_COLUMNS` (дрейф колонок → ошибка); отсутствие лишних незакавыченных
    колонок (страж ``restkey`` как ``catalog``); непустой/непробельный ``path``; отсутствие
    дублей ``path`` (коллизия); ненулевое число записей. Возвращает упорядоченный ``list[str]``
    значений ``path`` в порядке файла (детерминизм).

    Отсутствие файла / битый симлинк → :class:`SymlinkContractError` с путём. Хвостовую пустую
    строку (типовую для LF-файла) ``DictReader`` записью не отдаёт → «пустой path» о неё не
    спотыкается; чинить вручную (``splitlines``/фильтры) не нужно.
    """
    resolved = contract_path if contract_path is not None else DEFAULT_CONTRACT_PATH
    if not resolved.is_file():
        raise SymlinkContractError(
            f"Контракт симлинков не найден (файл отсутствует или битый симлинк): {resolved}"
        )

    # newline="" — требование csv: иначе встроенные переводы строк в кавычках comment
    # разобьются. encoding="utf-8": в описаниях кириллица и «—».
    with resolved.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, restkey=_RESTKEY)
        if reader.fieldnames != list(CONTRACT_COLUMNS):
            raise SymlinkContractError(
                f"Заголовок контракта не соответствует ожидаемому. "
                f"Ожидалось {list(CONTRACT_COLUMNS)}, получено {reader.fieldnames}"
            )

        paths: list[str] = []
        seen: set[str] = set()
        # Нумеруем с 2: заголовок — строка 1; номер в сообщениях помогает диагностике.
        for line_no, row in enumerate(reader, start=2):
            # Колонок БОЛЬШЕ заголовка → избыток ушёл в список под restkey: незакавыченная
            # запятая в comment сдвинула бы поля — fail-loud, а не молчаливая потеря хвоста.
            extra = row.get(_RESTKEY)
            if extra:
                raise SymlinkContractError(
                    f"Строка {line_no}: колонок больше, чем в заголовке "
                    f"({len(CONTRACT_COLUMNS)}); лишнее: {extra!r}. "
                    f"Проверь незакавыченные запятые/лишние поля"
                )
            value = (row.get("path") or "").strip()
            if not value:
                raise SymlinkContractError(
                    f"Строка {line_no}: пустой path в контракте (путь без записи = дефект)"
                )
            # path — строго относительный, без '..': rooted-путь (Path.__truediv__ отбросил бы
            # dev_repo_root/storage_root) или '..' молча увели бы линк/цель за пределы репо.
            # Контракт доверенный, но 4.3 дописывает записи вручную → ловим опечатку рано.
            # PureWindowsPath — строжайшая интерпретация на ЛЮБОЙ ОС: распознаёт и '/', и '\\',
            # и диск (C:\\, C:foo), и UNC; .anchor непуст у любого rooted-пути. (os.path.isabs
            # с 3.13 НЕ считает '/etc' абсолютным на Windows — drive-relative — потому не он.)
            win_path = PureWindowsPath(value)
            if win_path.anchor or ".." in win_path.parts:
                raise SymlinkContractError(
                    f"Строка {line_no}: path должен быть относительным без '..': {value!r}"
                )
            if value in seen:
                raise SymlinkContractError(
                    f"Строка {line_no}: дубль path в контракте: {value!r} (коллизия симлинка)"
                )
            seen.add(value)
            paths.append(value)

    # Пустой контракт (валидный заголовок, ноль записей данных) — вырожденный SSOT: дефект,
    # а не «нечего связывать». Fail-loud (project-context: «в спорной — строже»).
    if not paths:
        raise SymlinkContractError(
            f"Контракт симлинков пуст — нет ни одной записи (только заголовок): {resolved}"
        )

    return paths


def preflight_symlink_capability(probe_dir: Path | None = None) -> None:
    """Проверить способность платформы создавать симлинки реальной пробой (AC #4, fail-loud).

    В ``probe_dir`` (по умолчанию свежий :func:`tempfile.mkdtemp`; инъектируется для тестов)
    создаёт временную цель-файл и пробует ``os.symlink`` на неё. Успех → ``None``. ``OSError``/
    ``NotImplementedError`` (на Windows без Developer Mode — ``winerror == 1314``,
    ``ERROR_PRIVILEGE_NOT_HELD``) → :class:`SymlinkPreflightError` с инструкцией включить
    Developer Mode либо запустить от администратора.

    Способность проверяется РЕАЛЬНОЙ попыткой, а не флагом ``os.name``/``sys.platform`` (надёжнее).
    Пробу (линк, цель, временный каталог) убираем в ``finally`` под ``suppress(OSError)`` — не
    оставляем мусора даже на провале. 4.3 зовёт preflight первым шагом разворачивания симлинков,
    ДО создания первого симлинка, — чтобы непригодная платформа падала «насухо».
    """
    base = Path(tempfile.mkdtemp()) if probe_dir is None else probe_dir
    target = base / "_gdau_symlink_probe_target"
    link = base / "_gdau_symlink_probe_link"
    try:
        target.write_text("probe", encoding="utf-8")
        os.symlink(target, link)  # без Dev Mode на Windows → OSError winerror=1314
    except (OSError, NotImplementedError) as exc:
        raise SymlinkPreflightError(
            "Система не умеет создавать символические ссылки. На Windows включи "
            "Developer Mode (Параметры → Конфиденциальность и безопасность → Для "
            "разработчиков → Режим разработчика) либо запусти от администратора. "
            f"Причина: {exc}"
        ) from exc
    finally:
        if probe_dir is None:
            # Свой временный каталог — убрать целиком, без протечки temp.
            shutil.rmtree(base, ignore_errors=True)
        else:
            # Чужой каталог (инъектирован тестом) — убрать только свои пробные артефакты.
            with suppress(OSError):
                if link.is_symlink() or link.exists():
                    os.unlink(link)
            with suppress(OSError):
                target.unlink()


def _relative_target(dev_repo_root: Path, storage_root: Path, rel: str) -> str:
    """Относительная цель симлинка ``rel`` от каталога самого линка (AC #6, чистая строка).

    ``os.path.relpath`` — чистая строковая операция (ФС не трогает, символы не резолвит) →
    тестируется детерминированно без способности создавать симлинки. Относительная (а не
    абсолютная) цель переживает перенос пары «dev-репо + хранилище-сосед» между машинами/ОС
    (NFR-2; осознанное расхождение с directaiq, делавшим абсолютные цели).
    """
    return os.path.relpath(dev_repo_root / rel, start=(storage_root / rel).parent)


def create_symlinks(
    *,
    dev_repo_root: Path,
    storage_root: Path,
    contract_path: Path | None = None,
    run_preflight: bool = True,
) -> list[Path]:
    """Создать относительные симлинки хранилища на dev-репо по контракту (AC #2, #5, #6, #7, #9).

    Корни ``dev_repo_root``/``storage_root`` инъектируются (резолюцию ``GDAU_DATA_ROOT`` делает
    4.3). Порядок: при ``run_preflight`` — :func:`preflight_symlink_capability` первым делом
    (самодостаточный fail-loud ДО мутаций, AC #4; 4.3 может звать preflight отдельно и передать
    ``run_preflight=False``); загрузка контракта; ПРЕДВАЛИДАЦИЯ всех целей (отсутствует в
    dev-репо → :class:`SymlinkTargetMissingError` ДО создания первого линка, AC #5, падаем
    «насухо»); затем по порядку для каждой записи:

    - относительная цель ``os.path.relpath`` от родителя линка (AC #6);
    - существующий путь: СИМЛИНК → идемпотентная замена (``os.unlink`` + создать заново, AC #7);
      реальный файл/каталог → :class:`SymlinkError` (отказ удалять, НЕ ``rm -rf``);
    - ``os.symlink(..., target_is_directory=...)`` (на Windows обязателен для dir-цели);
      сырой ``OSError`` → :class:`SymlinkError` с путём.

    Сбой посреди цикла (TOCTOU; AC #9) → откат симлинков, созданных ЭТИМ вызовом (в обратном
    порядке, под ``suppress(OSError)``), и проброс исходной ошибки — частичного набора не
    остаётся. Пред-существующие симлинки/каталоги не трогаются; полный откат хранилища (шаблон/
    ``.env``/БД) — забота 4.3, здесь не дублируется. Возвращает список созданных линков (для
    лога/диагностики 4.3).
    """
    if run_preflight:
        preflight_symlink_capability()

    rel_paths = load_symlink_contract(contract_path)

    # Предвалидация ВСЕХ целей ДО создания первого линка (AC #5): битый контракт (цель названа,
    # но её нет в dev-репо) падает «насухо», не оставив частичного набора.
    for rel in rel_paths:
        target = dev_repo_root / rel
        if not target.exists():  # Path.exists() следует за симлинками — битая цель → False
            raise SymlinkTargetMissingError(
                f"Цель контракта отсутствует в dev-репо: {target} "
                f"(запись {rel!r} контракта симлинков)"
            )

    created: list[Path] = []
    try:
        for rel in rel_paths:
            link = storage_root / rel
            # Вложенная запись контракта → создать промежуточных родителей под линк.
            link.parent.mkdir(parents=True, exist_ok=True)
            # os.path.relpath бросает ValueError, если корни на разных дисках/точках
            # монтирования (Windows: dev-репо G:\ vs хранилище C:\) — относительный симлинк
            # между дисками невозможен в принципе. Падаем fail-loud доменной ошибкой
            # (не сырой ValueError — инвариант «stdlib-исключение не наружу»).
            try:
                rel_target = _relative_target(dev_repo_root, storage_root, rel)
            except ValueError as exc:
                raise SymlinkError(
                    f"Невозможно построить относительную ссылку для записи {rel!r}: "
                    f"dev-репо ({dev_repo_root}) и хранилище ({storage_root}) на разных "
                    f"дисках/точках монтирования. Размести хранилище на том же диске, что "
                    f"и dev-репо (требование относительных симлинков, NFR-2). Причина: {exc}"
                ) from exc

            # Существующий путь (AC #7): симлинк → идемпотентная замена; реальный → fail-loud.
            if link.is_symlink():
                os.unlink(link)
            elif link.exists():
                raise SymlinkError(
                    f"По пути линка лежит реальный файл/каталог, не симлинк — отказ удалять "
                    f"(разбери вручную): {link}"
                )

            target_is_dir = (dev_repo_root / rel).is_dir()
            try:
                os.symlink(rel_target, link, target_is_directory=target_is_dir)
            except OSError as exc:
                raise SymlinkError(
                    f"Не удалось создать симлинк {link} → {rel_target}: {exc}"
                ) from exc
            created.append(link)
            logger.info("Создан симлинк %s → %s", link, rel_target)
    except BaseException:
        # Откат частичного набора (AC #9): снять созданные ЭТИМ вызовом линки в обратном порядке;
        # пред-существующие не трогаем. Затем пробрасываем исходную ошибку.
        for done in reversed(created):
            with suppress(OSError):
                os.unlink(done)
        raise

    return created
