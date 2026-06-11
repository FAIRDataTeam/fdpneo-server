"""Unit tests for the profile validator."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fdp.metadata.profiles import load_profile, validate_profile
from tests.unit.metadata.profiles.conftest import (
    BROKEN_OFFER_TTL,
    MANIFEST,
    SCHEMA_TTL,
)


@pytest.mark.unit
def test_validate_clean_profile_reports_no_issues(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    report = validate_profile(profile)
    assert report.ok is True
    assert report.issues == []


@pytest.mark.unit
def test_validate_rejects_extends(write_bundle: Callable[..., Path]) -> None:
    manifest = MANIFEST.replace(
        "kind: DeploymentProfile\n",
        "kind: DeploymentProfile\nextends: other-profile\n",
    )
    profile = load_profile(write_bundle(manifest_text=manifest))
    report = validate_profile(profile)
    codes = {i.code for i in report.issues}
    assert "extends_unsupported" in codes


@pytest.mark.unit
def test_validate_catches_offer_outside_fdp_profile(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle(offer_text=BROKEN_OFFER_TTL))
    report = validate_profile(profile)
    codes = {i.code for i in report.issues}
    assert "offer_not_in_fdp_profile" in codes


@pytest.mark.unit
def test_validate_catches_offer_without_odrl_offer_triple(
    write_bundle: Callable[..., Path],
) -> None:
    # No odrl:Offer declared in the file at all.
    profile = load_profile(
        write_bundle(offer_text="@prefix odrl: <http://www.w3.org/ns/odrl/2/> .\n")
    )
    report = validate_profile(profile)
    codes = {i.code for i in report.issues}
    assert "no_odrl_offer" in codes


@pytest.mark.unit
def test_validate_catches_duplicate_schema_ids(
    write_bundle: Callable[..., Path],
) -> None:
    manifest = MANIFEST.replace(
        "schemas:\n  - id: dcat:Catalog\n    path: schemas/catalog.ttl\n",
        (
            "schemas:\n"
            "  - id: dcat:Catalog\n"
            "    path: schemas/catalog.ttl\n"
            "  - id: dcat:Catalog\n"
            "    path: schemas/catalog.ttl\n"
        ),
    )
    profile = load_profile(write_bundle(manifest_text=manifest))
    report = validate_profile(profile)
    codes = {i.code for i in report.issues}
    assert "duplicate_schema_id" in codes


@pytest.mark.unit
def test_validate_catches_schema_slug_collision(
    write_bundle: Callable[..., Path],
) -> None:
    # Two distinct ids whose local names kebab-case to the same storage slug
    # (dcat:Catalog and dct:Catalog → "catalog") would collide at
    # {base}/fdp-api/schemas/catalog (task 10.5).
    manifest = MANIFEST.replace(
        "    path: schemas/catalog.ttl\n",
        "    path: schemas/catalog.ttl\n  - id: dct:Catalog\n    path: schemas/catalog2.ttl\n",
    )
    profile = load_profile(
        write_bundle(manifest_text=manifest, extra_files={"schemas/catalog2.ttl": SCHEMA_TTL})
    )
    report = validate_profile(profile)
    codes = {i.code for i in report.issues}
    assert "duplicate_schema_slug" in codes
    # The two distinct ids are NOT flagged as duplicate *ids*.
    assert "duplicate_schema_id" not in codes
