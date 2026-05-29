"""Unit tests for the profile manifest loader."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fdp.metadata.profiles import load_profile
from fdp.shared.errors import BadRequest


@pytest.mark.unit
def test_load_profile_parses_manifest_and_loads_referenced_files(
    write_bundle: Callable[..., Path],
) -> None:
    bundle = write_bundle()
    profile = load_profile(bundle)

    assert profile.name == "test"
    assert profile.version == "0.1.0"
    assert len(profile.schemas) == 1
    assert profile.schemas[0].entry.id == "dcat:Catalog"
    # The Turtle was parsed into an rdflib graph.
    assert len(profile.schemas[0].graph) > 0
    assert len(profile.offers) == 1
    assert profile.offers[0].entry.is_system_default is True
    assert profile.manifest.resource_definitions[0].name == "Repository"


@pytest.mark.unit
def test_load_profile_produces_a_stable_checksum(
    write_bundle: Callable[..., Path],
) -> None:
    bundle = write_bundle()
    a = load_profile(bundle)
    b = load_profile(bundle)
    assert a.manifest_checksum == b.manifest_checksum
    assert len(a.manifest_checksum) == 64  # sha256 hex


@pytest.mark.unit
def test_load_profile_rejects_missing_manifest(tmp_path: Path) -> None:
    (tmp_path / "bundle").mkdir()
    with pytest.raises(BadRequest) as exc:
        load_profile(tmp_path / "bundle")
    assert "profile manifest not found" in exc.value.message


@pytest.mark.unit
def test_load_profile_rejects_invalid_yaml(
    write_bundle: Callable[..., Path],
) -> None:
    bundle = write_bundle(manifest_text="{not: valid: yaml")
    with pytest.raises(BadRequest):
        load_profile(bundle)


@pytest.mark.unit
def test_load_profile_rejects_unknown_top_level_keys(
    write_bundle: Callable[..., Path],
) -> None:
    bundle = write_bundle(
        manifest_text="""\
apiVersion: fdp/v1
kind: DeploymentProfile
metadata: {name: test, version: 0.1.0}
unknownKey: oops
"""
    )
    with pytest.raises(BadRequest):
        load_profile(bundle)


@pytest.mark.unit
def test_load_profile_rejects_missing_referenced_file(
    write_bundle: Callable[..., Path], tmp_path: Path
) -> None:
    bundle = write_bundle()
    (bundle / "offers" / "system-default.ttl").unlink()
    with pytest.raises(BadRequest) as exc:
        load_profile(bundle)
    assert "missing file" in exc.value.message
