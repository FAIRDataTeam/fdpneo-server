"""Built-in default license documents seeded on profile apply (TASKS 14.5).

ADR-0012 makes licenses first-class managed documents at ``{base}/licenses/{id}``.
Every deployment seeds a small, standard set so the FDP can act as a reference
source out of the box and the client's ``dct:license`` picker has sane options.

Each is a deployment-local ``dct:LicenseDocument`` (dereferenceable, reusable)
that links to its canonical license IRI via ``dct:source`` — records may
reference either the local managed IRI or the canonical one. The set is
intentionally tiny (the three most common open licenses); operators add more
through ``PUT /licenses/{id}`` at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.shared.graphs import license_graph_uri
from fdp.shared.namespaces import DCT

if TYPE_CHECKING:
    from collections.abc import Iterator

# (id, title, canonical license IRI). The id is the slug under /licenses/.
_DEFAULT_LICENSES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "cc0-1.0",
        "Creative Commons CC0 1.0 Universal",
        "http://creativecommons.org/publicdomain/zero/1.0/",
    ),
    (
        "cc-by-4.0",
        "Creative Commons Attribution 4.0 International",
        "http://creativecommons.org/licenses/by/4.0/",
    ),
    (
        "cc-by-sa-4.0",
        "Creative Commons Attribution-ShareAlike 4.0 International",
        "http://creativecommons.org/licenses/by-sa/4.0/",
    ),
)


def _license_graph(iri: str, title: str, source: str) -> Graph:
    subject = URIRef(iri)
    graph = Graph()
    graph.add((subject, RDF.type, DCT.LicenseDocument))
    graph.add((subject, DCT.title, Literal(title)))
    graph.add((subject, DCT.source, URIRef(source)))
    return graph


def default_license_graphs(base_url: str) -> Iterator[tuple[str, Graph]]:
    """Yield ``(iri, graph)`` for each built-in default license document."""
    for license_id, title, source in _DEFAULT_LICENSES:
        iri = str(license_graph_uri(base_url, license_id))
        yield iri, _license_graph(iri, title, source)


__all__ = ["default_license_graphs"]
