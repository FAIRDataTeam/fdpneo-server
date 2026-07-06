"""Unit tests for external (remote) label resolution (Phase 21).

Grows across the phase; this first slice covers the ``RemoteLabelSettings``
configuration group (env parsing + the ``effective_enabled`` gate).
"""

from __future__ import annotations

import pytest

from fdp.config import RemoteLabelSettings

pytestmark = pytest.mark.unit


def _settings(**over: object) -> RemoteLabelSettings:
    return RemoteLabelSettings(_env_file=None, **over)  # type: ignore[arg-type]


def test_defaults_are_off_and_deny_all() -> None:
    s = _settings()
    assert s.enabled is False
    assert s.allowed_hosts == []
    assert s.hosts == frozenset()
    assert s.effective_enabled is False


def test_allowed_hosts_parses_csv() -> None:
    s = _settings(allowed_hosts="ror.org, doi.org , orcid.org")
    assert s.allowed_hosts == ["ror.org", "doi.org", "orcid.org"]
    assert s.hosts == frozenset({"ror.org", "doi.org", "orcid.org"})


def test_allowed_hosts_parses_json_array() -> None:
    s = _settings(allowed_hosts='["ror.org", "doi.org"]')
    assert s.allowed_hosts == ["ror.org", "doi.org"]


def test_effective_enabled_requires_switch_and_hosts() -> None:
    # Switch on but no hosts → still inert.
    assert _settings(enabled=True, allowed_hosts=[]).effective_enabled is False
    # Hosts listed but switch off → inert.
    assert _settings(enabled=False, allowed_hosts="ror.org").effective_enabled is False
    # Both → live.
    assert _settings(enabled=True, allowed_hosts="ror.org").effective_enabled is True
