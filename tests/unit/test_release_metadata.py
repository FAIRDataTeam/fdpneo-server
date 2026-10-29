"""release.json (ADR-0028 §2) stays valid and in step with the package version."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from fdpneo_server import __version__

ROOT = Path(__file__).resolve().parents[2]


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_release_json", ROOT / "scripts" / "check_release_json.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_release_json_matches_the_package_version() -> None:
    data = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    assert _checker().problems(data, __version__, "server") == []


@pytest.mark.unit
def test_checker_reports_each_problem() -> None:
    found = _checker().problems(
        {
            "schema": 2,
            "component": "client",
            "version": "1.0.0",
            "min_upgrade_from": "one",
            "manual_steps": "no",
            "security": None,
            "requires": {"server": "~1"},
            "upgrade_notes": "http://insecure",
        },
        "0.20.0",
        "server",
    )
    assert len(found) == 8
