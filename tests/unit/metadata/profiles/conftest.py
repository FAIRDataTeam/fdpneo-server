"""Shared fixtures for profile-pipeline unit tests.

A small in-memory profile bundle written to a ``tmp_path`` — gives the
loader real files without depending on the shipped ``profiles/default``
bundle (whose schemas are not authored yet).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

SCHEMA_TTL = """\
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix dcat: <http://www.w3.org/ns/dcat#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

<http://www.w3.org/ns/dcat#Catalog>
    a sh:NodeShape ;
    sh:targetClass dcat:Catalog ;
    sh:property [
        sh:path dct:title ;
        sh:minCount 1 ;
        sh:datatype xsd:string ;
    ] .
"""

OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .

<https://fdp.example/offers/system-default>
    a odrl:Offer ;
    odrl:permission [
        a odrl:Permission ;
        odrl:action odrl:read ;
    ] .
"""

BROKEN_OFFER_TTL = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .

<https://fdp.example/offers/broken>
    a odrl:Offer ;
    odrl:permission [
        a odrl:Permission ;
        odrl:action <http://example.org/unknown-action> ;
    ] .
"""

MANIFEST = """\
apiVersion: fdp/v1
kind: DeploymentProfile

metadata:
  name: test
  version: 0.1.0

schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl

offers:
  - id: system-default
    path: offers/system-default.ttl
    isSystemDefault: true

resourceDefinitions:
  - urlPrefix: ""
    name: Repository
    schema: dcat:Catalog
"""

# A manifest without a resourceDefinitions: block. Used by the
# "validator skips rd_* checks" guard.
MANIFEST_WITHOUT_RDS = """\
apiVersion: fdp/v1
kind: DeploymentProfile

metadata:
  name: test
  version: 0.1.0

schemas:
  - id: dcat:Catalog
    path: schemas/catalog.ttl

offers:
  - id: system-default
    path: offers/system-default.ttl
    isSystemDefault: true
"""


@pytest.fixture
def write_bundle(tmp_path: Path) -> Callable[..., Path]:
    """Return a builder that writes a bundle to a sub-directory of ``tmp_path``."""

    def _write(
        *,
        manifest_text: str = MANIFEST,
        schema_text: str = SCHEMA_TTL,
        offer_text: str = OFFER_TTL,
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        root = tmp_path / "bundle"
        root.mkdir(exist_ok=True)
        (root / "profile.yaml").write_text(manifest_text, encoding="utf-8")
        (root / "schemas").mkdir(exist_ok=True)
        (root / "schemas" / "catalog.ttl").write_text(schema_text, encoding="utf-8")
        (root / "offers").mkdir(exist_ok=True)
        (root / "offers" / "system-default.ttl").write_text(offer_text, encoding="utf-8")
        for relative, body in (extra_files or {}).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return root

    return _write
