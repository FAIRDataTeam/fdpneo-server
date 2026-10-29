"""``FDP_ACCESS_CONTROL_MODE`` seeds the access-control default (ADR-0027)."""

from __future__ import annotations

import pytest

from fdpneo_server.config import AccessControlSettings, Settings


@pytest.mark.unit
def test_default_mode_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDP_ACCESS_CONTROL_MODE", raising=False)
    assert AccessControlSettings().mode == "open"
    assert Settings().access_control.mode == "open"  # type: ignore[call-arg]


@pytest.mark.unit
def test_env_selects_odrl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDP_ACCESS_CONTROL_MODE", "odrl")
    assert Settings().access_control.mode == "odrl"  # type: ignore[call-arg]


@pytest.mark.unit
def test_unknown_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDP_ACCESS_CONTROL_MODE", "rbac")
    with pytest.raises(ValueError):
        AccessControlSettings()
