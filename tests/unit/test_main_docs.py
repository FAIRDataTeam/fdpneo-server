"""Tests for interactive-docs gating in non-dev environments (audit R-04)."""

from __future__ import annotations

import pytest

from fdpneo_server.main import _docs_enabled, create_app


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment", "expose", "expected"),
    [
        ("development", False, True),
        ("development", True, True),
        ("staging", False, False),
        ("production", False, False),
        ("production", True, True),  # explicit opt-in
    ],
)
def test_docs_enabled_logic(environment: str, expose: bool, expected: bool) -> None:
    assert _docs_enabled(environment, expose) is expected


@pytest.mark.unit
def test_docs_routes_absent_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdpneo_server.config import get_settings

    prod = get_settings().model_copy(update={"environment": "production", "expose_api_docs": False})
    monkeypatch.setattr("fdpneo_server.main.get_settings", lambda: prod)

    app = create_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/docs" not in paths
    assert "/redoc" not in paths
