# CLAUDE.md

Воспроизводимый юнит приёма и анализа данных Яндекс Метрики через Logs API
(Logs API → Parquet → DuckDB-view'ы; MCP для чтения). Один оператор — агент.

## 📖 Правила реализации — обязательно к прочтению

**Перед написанием любого кода прочитай [`_bmad-output/project-context.md`](_bmad-output/project-context.md)** —
там собраны критические, неочевидные правила (стек, каталог-SSOT, целостность базы,
тесты, workflow, анти-паттерны). Следуй им дословно; в спорной ситуации выбирай более
строгий вариант.

## Принцип

**Простота, понятность, стабильность. Усложнять только по реальной потребности.**
Не тащить тяжёлую инфраструктуру `directaiq` (queue/disk-guard/cron/`BaseScript`/`config_manager`)
и аналитический стек (`pandas`/`polars`/…).

## Команды (всё через `uv run`)

```bash
uv sync                 # окружение строго по uv.lock (CI: uv sync --frozen)
uv run pytest           # offline-тесты (моки)
uv run pytest -m live   # live-smoke против РЕАЛЬНОГО Logs API (нужны креды в .env)
uv run mypy scripts     # типы (strict)
uv run gdau-logs        # CLI приёма Logs API
uv run gdau-init        # разворачивание per-game хранилища
```

## Инварианты (детали — в project-context.md)

- **Каталог `development-docs/schema-catalog.csv` = единственный источник истины.** Поле без записи = дефект; типы — маппингом ClickHouse→DuckDB из справочника, не угадывать.
- **Целостность базы:** запись только через `.writer.lock` + temp→rename; сверка строк = жёсткий fail (не warning); сырьё Parquet — строками, типизация только во view (`TRY_CAST`).
- **storage-имена — `snake_case`;** родные `ym:s:*` живут только в каталоге.
- **На каждый логический компонент — спека `docs/<component>.md`** человеческим языком (часть Definition of Done).
- **Новая история → новая ветка;** секреты и данные (`*.parquet`/`*.duckdb`/`data/`/`.env`) не коммитятся.
- **Тесты внешнего API — не только моки:** обязателен opt-in live-smoke против реального Logs API.

## Карта документов

- `_bmad-output/project-context.md` — правила для агентов (этот вход ссылается на него).
- `_bmad-output/planning-artifacts/architecture.md` — полные архитектурные решения.
- `_bmad-output/planning-artifacts/prds/.../prd.md` — требования (FR/NFR).
- `development-docs/schema-catalog.csv` — каталог схемы (SSOT).
- `yandex-docs/metrika-api/` — официальные справочники Logs API.
- `docs/` — человекочитаемые спеки компонентов.
