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

import re
from typing import TYPE_CHECKING

from rdflib import Graph, Namespace, URIRef

from fdp.shared.errors import BadRequest
from fdp.shared.graphs import schema_graph_uri
from fdp.shared.namespaces import PREFIXES, fdp_namespace

if TYPE_CHECKING:
    from fdp.config import Settings

# camelCase boundary: a lowercase/digit immediately followed by an uppercase
# letter. Used to kebab-case a vocabulary local name (``DataService`` →
# ``data-service``) when deriving a storage slug.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Placeholder URN a modular shape uses to identify itself and reference the base
# shapes it composes (``urn:fdp-schema:catalog``, ``urn:fdp-schema:resource``).
# The applier expands it to the deployment storage IRI at apply time (the shape's
# subject and its sh:node targets must be the real storage IRI for pySHACL to
# resolve composition — see expand_schema_refs).
SCHEMA_REF_PLACEHOLDER = "urn:fdp-schema:"


def _local_name(identifier: str) -> str:
    """The terminal name of a CURIE or absolute IRI.

    ``dcat:Catalog`` → ``Catalog``; ``http://www.w3.org/ns/dcat#Catalog`` →
    ``Catalog``; ``https://w3id.org/fdp/o#Repository`` → ``Repository``.
    Fragment (``#``) and path (``/``) win over the CURIE colon so an absolute
    IRI's scheme (``http:``) is never mistaken for a prefix.
    """
    for sep in ("#", "/"):
        if sep in identifier:
            return identifier.rsplit(sep, 1)[-1]
    if ":" in identifier:
        return identifier.split(":", 1)[1]
    return identifier


def schema_slug(identifier: str) -> str:
    """Stable storage slug for a profile schema id (CURIE or absolute IRI).

    Kebab-cases the vocabulary local name so a profile schema lands at a tidy,
    user-schema-shaped IRI under the reserved schemas namespace:
    ``fdp:Repository`` → ``repository``, ``dcat:DataService`` → ``data-service``.
    The slug is the single naming convention shared by the applier (which
    *writes* the schema there) and the registry (which points resource
    definitions' ``ldp:constrainedBy`` at the same IRI), so both always agree.
    """
    kebab = _CAMEL_BOUNDARY.sub("-", _local_name(identifier))
    return re.sub(r"[^A-Za-z0-9]+", "-", kebab).strip("-").lower()


class IRIExpander:
    """Translates manifest-local identifiers into absolute IRIs."""

    def __init__(self, *, settings: Settings) -> None:
        # Records and managed documents are minted under the *identifier* base
        # (the persistent PID namespace, ADR-0014), which defaults to base_url
        # when no PID namespace is configured — so local deployments are
        # unchanged. base_url remains the serving origin.
        self._base = settings.resolved_identifier_base
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

    def schema_storage_iri(self, curie_or_uri: str) -> str:
        """Where a profile schema is *stored* and addressed: the schemas namespace.

        Distinct from :meth:`schema_iri` (the vocabulary/class IRI). A profile
        schema declared as ``dcat:Catalog`` is stored at
        ``{base}/fdp-api/schemas/catalog`` — the same namespace a runtime
        ``PUT /schemas/{id}`` uses — so it lists and edits as an ordinary
        user-provided schema. The shape's node IRI and ``sh:targetClass`` inside
        the graph stay the class IRI, so SHACL matching is unaffected.
        """
        return str(schema_graph_uri(self._base, schema_slug(curie_or_uri)))

    def offer_iri(self, offer_id: str) -> str:
        return f"{self._base}/offers/{offer_id}"

    def container_iri(self, container_id: str) -> str:
        return f"{self._base}/{container_id}"

    def seed_record_iri(self, seed_id: str) -> str:
        return f"{self._base}/{seed_id.lstrip('/')}"

    @property
    def base_url(self) -> str:
        return self._base


def expand_schema_refs(graph: Graph, base_url: str) -> Graph:
    """Expand ``urn:fdp-schema:<slug>`` placeholders to storage IRIs.

    Returns a copy of ``graph`` with every placeholder term — in any position
    (the shape's own subject, its ``sh:node``/list references) — rewritten to
    ``{base}/fdp-api/schemas/<slug>``. Blanket substitution is safe because the
    placeholder URN is only ever shape identity/refs, never a ``sh:targetClass``
    or ``sh:class`` (those keep the real vocabulary IRI). This is what makes a
    composed shape resolvable: pySHACL looks up ``sh:node <X>`` by the referenced
    shape's subject, and the provider fetches it, so both must be the same real
    storage IRI.
    """
    base = base_url.rstrip("/")

    def remap(term: object) -> object:
        if isinstance(term, URIRef) and str(term).startswith(SCHEMA_REF_PLACEHOLDER):
            slug = str(term)[len(SCHEMA_REF_PLACEHOLDER) :]
            return URIRef(str(schema_graph_uri(base, slug)))
        return term

    out = Graph()
    for s, p, o in graph:
        out.add((remap(s), remap(p), remap(o)))  # type: ignore[arg-type]
    return out


__all__ = ["SCHEMA_REF_PLACEHOLDER", "IRIExpander", "expand_schema_refs", "schema_slug"]
