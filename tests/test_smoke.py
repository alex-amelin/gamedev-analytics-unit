"""Smoke-тест каркаса (история 1.1).

Двойная роль:
- AC #5: подтверждает, что пакет ``scripts`` импортируется (``import scripts.utils``).
- AC #6: даёт ≥1 собранный тест, чтобы ``pytest`` не вернул exit code 5
  («no tests collected»), который CI трактует как красный.
"""

from __future__ import annotations


def test_scripts_package_importable() -> None:
    """Пакет scripts и подпакет utils резолвятся в установленном окружении."""
    import scripts.utils  # noqa: F401


def test_entry_point_modules_importable() -> None:
    """Модули, на которые ссылаются entry points gdau-logs/gdau-init, импортируются."""
    import scripts.init.init_project
    import scripts.tools.logs_api_cli

    assert callable(scripts.tools.logs_api_cli.main)
    assert callable(scripts.init.init_project.main)
