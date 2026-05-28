"""CURIE / slug → IRI expansion shared by the applier and the registry.

Extracted from :mod:`fdp.metadata.profiles.applier` so the registry
(:mod:`fdp.metadata.profiles.registry`) can use it without importing
the applier, which would form a cycle (applier → registry → applier).

The convention here is the one documented on
:func:`fdp.metadata.profiles.apply_profile`:

* Schema / type CURIE: ``fdp:Repository`` → ``<fdp_namespace>Repository``;
  any prefix registered in :data:`fdp.shared.namespaces.PREFIXES`
  resolves through that namespace.
* Offer slug: ``<base_url>/offers/<id>``. (Retained for callers that
  still mint deployment-derived offer URIs; new code should let the
  applier honor the file-declared offer IRI instead.)
* Container slug: ``<base_url>/<id>``. (Legacy — see sub-task 15c.)
* Seed-record slug: ``<base_url>/<seed_id>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rdflib import Namespace

from fdp.shared.errors import BadRequest
from fdp.shared.namespaces import PREFIXES, fdp_namespace

if TYPE_CHECKING:
    from fdp.config import Settings


class IRIExpander:
    """Translates manifest-local identifiers into absolute IRIs."""

    def __init__(self, *, settings: Settings) -> None:
        self._base = str(settings.base_url).rstrip("/")
        self._prefixes = dict(PREFIXES)
        self._fdp = fdp_namespace(settings)

    def schema_iri(self, curie_or_uri: str) -> str:
        """Expand a CURIE like ``fdp:Repository`` or pass through an absolute IRI.

        Used for SHACL shape ids and for child-relation predicates —
        any place a profile entry carries a vocabulary term.
        """
        if ":" not in curie_or_uri or curie_or_uri.startswith(("http://", "https://")):
            return curie_or_uri
        prefix, local = curie_or_uri.split(":", 1)
        if prefix == "fdp":
            return str(self._fdp[local])
        ns: Namespace | None = self._prefixes.get(prefix)
        if ns is None:
            raise BadRequest(
                f"unknown prefix: {prefix}",
                details={"curie": curie_or_uri},
            )
        return str(ns[local])

    def offer_iri(self, offer_id: str) -> str:
        return f"{self._base}/offers/{offer_id}"

    def container_iri(self, container_id: str) -> str:
        return f"{self._base}/{container_id}"

    def seed_record_iri(self, seed_id: str) -> str:
        return f"{self._base}/{seed_id.lstrip('/')}"

    @property
    def base_url(self) -> str:
        return self._base


__all__ = ["IRIExpander"]
