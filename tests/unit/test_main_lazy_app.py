"""Unit tests for the lazy module-level ``app`` binding (PEP 562).

``import fdpneo_server.main`` must be configuration-free: no settings are
read, no application is built, nothing leaks. The ``app`` attribute still
resolves for ``uvicorn fdpneo_server.main:app`` — built on first access
and cached on the module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, cast

import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit


def _config_free_env() -> dict[str, str]:
    """The current environment with every FDP/Postgres setting removed.

    ``PYTHONPATH`` mirrors the parent's ``sys.path`` so the subprocess can
    import the package however it is installed — it carries no
    application configuration.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FDP_") and key != "POSTGRES_DSN"
    }
    env["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry)
    return env


def test_bare_import_needs_no_configuration() -> None:
    """The downstream acceptance criterion: import with no FDP_* env set."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import fdpneo_server.main; from fdpneo_server.main import create_app",
        ],
        env=_config_free_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_import_does_not_build_an_app() -> None:
    """Import must not construct the app (nor its engines/HTTP clients)."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import fdpneo_server.main as m; assert 'app' not in vars(m)",
        ],
        env=_config_free_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_app_attribute_resolves_lazily_and_is_cached() -> None:
    """``main:app`` still works for uvicorn — same instance on every access."""
    import fdpneo_server.main as main

    # Drop a cached instance so this test exercises the __getattr__ path
    # regardless of what earlier tests touched. The cast keeps newer
    # pyright happy — module ``vars()`` is a plain writable dict at runtime.
    cast("dict[str, Any]", vars(main)).pop("app", None)

    first = main.app
    assert isinstance(first, FastAPI)
    assert main.app is first


def test_unknown_attribute_still_raises() -> None:
    import fdpneo_server.main as main

    with pytest.raises(AttributeError, match="no_such_symbol"):
        _ = main.no_such_symbol  # pyright: ignore[reportAttributeAccessIssue]
