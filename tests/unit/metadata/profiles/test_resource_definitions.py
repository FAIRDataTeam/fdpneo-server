"""Unit tests for the ``resourceDefinitions[]`` manifest block (sub-task 15a)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fdp.metadata.profiles import load_profile, validate_profile
from tests.unit.metadata.profiles.conftest import (
    MANIFEST_WITHOUT_RDS,
    SCHEMA_TTL,
)

# --- shared fixtures -----------------------------------------------------


_BASE_MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile

metadata:
  name: test
  version: 0.1.0

schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl
  - id: dcat:Dataset
    path: schemas/dataset.ttl

offers:
  - id: system-default
    path: offers/system-default.ttl
    isSystemDefault: true

resourceDefinitions:
  - urlPrefix: ""
    name: Repository
    schema: dcat:Catalog
    children:
      - relationUri: dcat:dataset
        target: dataset
        title: Datasets
  - urlPrefix: dataset
    name: Dataset
    schema: dcat:Dataset
"""


def _bundle_with_two_schemas(write_bundle: Callable[..., Path], manifest_text: str) -> Path:
    """Like the default fixture, but seeds *both* schemas/catalog.ttl and dataset.ttl."""
    return write_bundle(
        manifest_text=manifest_text,
        extra_files={"schemas/dataset.ttl": SCHEMA_TTL.replace("dcat:Catalog", "dcat:Dataset")},
    )


# --- manifest parsing ----------------------------------------------------


@pytest.mark.unit
def test_manifest_parses_resource_definitions(
    write_bundle: Callable[..., Path],
) -> None:
    bundle = _bundle_with_two_schemas(write_bundle, _BASE_MANIFEST)
    profile = load_profile(bundle)
    rds = profile.manifest.resource_definitions
    assert [r.url_prefix for r in rds] == ["", "dataset"]
    assert rds[0].is_root is True
    assert rds[0].name == "Repository"
    assert rds[0].children[0].relation_uri == "dcat:dataset"
    assert rds[0].children[0].target == "dataset"
    assert rds[0].children[0].title == "Datasets"


@pytest.mark.unit
def test_manifest_resource_definitions_default_to_empty_list(
    write_bundle: Callable[..., Path],
) -> None:
    """A bundle without resourceDefinitions: still loads cleanly."""
    profile = load_profile(write_bundle(manifest_text=MANIFEST_WITHOUT_RDS))
    assert profile.manifest.resource_definitions == []


@pytest.mark.unit
def test_manifest_rejects_unknown_keys_in_resource_definition(
    write_bundle: Callable[..., Path],
) -> None:
    manifest = _BASE_MANIFEST.replace(
        "  - urlPrefix: dataset\n    name: Dataset\n    schema: dcat:Dataset\n",
        "  - urlPrefix: dataset\n    name: Dataset\n    schema: dcat:Dataset\n    bogus: 1\n",
    )
    from fdp.shared.errors import BadRequest

    with pytest.raises(BadRequest):
        load_profile(_bundle_with_two_schemas(write_bundle, manifest))


# --- validator checks ----------------------------------------------------


@pytest.mark.unit
def test_validator_accepts_well_formed_resource_definitions(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(_bundle_with_two_schemas(write_bundle, _BASE_MANIFEST))
    report = validate_profile(profile)
    rd_codes = {i.code for i in report.issues if i.code.startswith("rd_")}
    assert rd_codes == set()


@pytest.mark.unit
def test_validator_flags_missing_root(write_bundle: Callable[..., Path]) -> None:
    manifest = _BASE_MANIFEST.replace('  - urlPrefix: ""\n', "  - urlPrefix: not-root\n")
    profile = load_profile(_bundle_with_two_schemas(write_bundle, manifest))
    report = validate_profile(profile)
    assert "rd_missing_root" in {i.code for i in report.issues}


@pytest.mark.unit
def test_validator_flags_multiple_roots(write_bundle: Callable[..., Path]) -> None:
    manifest = _BASE_MANIFEST.replace(
        "  - urlPrefix: dataset\n    name: Dataset\n    schema: dcat:Dataset\n",
        '  - urlPrefix: ""\n    name: SecondRoot\n    schema: dcat:Dataset\n',
    )
    profile = load_profile(_bundle_with_two_schemas(write_bundle, manifest))
    report = validate_profile(profile)
    assert "rd_multiple_roots" in {i.code for i in report.issues}


@pytest.mark.unit
def test_validator_flags_duplicate_url_prefix(
    write_bundle: Callable[..., Path],
) -> None:
    manifest = _BASE_MANIFEST + (
        "  - urlPrefix: dataset\n    name: SecondDataset\n    schema: dcat:Dataset\n"
    )
    profile = load_profile(_bundle_with_two_schemas(write_bundle, manifest))
    report = validate_profile(profile)
    assert "rd_duplicate_url_prefix" in {i.code for i in report.issues}


@pytest.mark.unit
def test_validator_flags_undeclared_schema(
    write_bundle: Callable[..., Path],
) -> None:
    manifest = _BASE_MANIFEST.replace("schema: dcat:Dataset", "schema: dcat:Unknown")
    profile = load_profile(_bundle_with_two_schemas(write_bundle, manifest))
    report = validate_profile(profile)
    assert "rd_schema_not_declared" in {i.code for i in report.issues}


@pytest.mark.unit
def test_validator_flags_child_target_not_declared(
    write_bundle: Callable[..., Path],
) -> None:
    manifest = _BASE_MANIFEST.replace("target: dataset", "target: ghost")
    profile = load_profile(_bundle_with_two_schemas(write_bundle, manifest))
    report = validate_profile(profile)
    assert "rd_child_target_not_declared" in {i.code for i in report.issues}


@pytest.mark.unit
def test_validator_skips_rd_checks_when_block_is_absent(
    write_bundle: Callable[..., Path],
) -> None:
    """Bundles without resourceDefinitions: emit no rd_* issues."""
    profile = load_profile(write_bundle(manifest_text=MANIFEST_WITHOUT_RDS))
    report = validate_profile(profile)
    rd_codes = {i.code for i in report.issues if i.code.startswith("rd_")}
    assert rd_codes == set()
