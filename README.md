# gamedev-analytics-unit

Воспроизводимый юнит приёма и анализа данных Яндекс Метрики через Logs API.

Стек закреплён локом (`uv.lock`), код живёт под `scripts/`, возможности выставляются
скриптуемыми CLI-командами (`gdau-logs`, `gdau-init`).

## Быстрый старт (dev)

```bash
uv sync                       # поднять окружение по локу
uv run pytest                 # тесты
uv run mypy scripts           # проверка типов
uv run gdau-logs              # CLI приёма Logs API (наполняется в Epic 1–2)
uv run gdau-init              # разворачивание per-game хранилища (Epic 4)
```

> Требуется Python ≥ 3.13 и [`uv`](https://docs.astral.sh/uv/). `uv sync` сам поднимет нужный Python по `.python-version`.
