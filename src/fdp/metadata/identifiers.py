"""Dual identifier reconciliation on write (v0.3.0, ADR-0014).

The FDP mints a **canonical** IRI for every record under the persistent
identifier base (the request path, canonicalized — see
:mod:`fdp.shared.identifiers`). A client may nonetheless submit a body whose
primary subject is a *foreign* identifier it already owns (a DOI, an ARK, another
organisation's IRI). The FDP cannot make that identifier resolve to itself, so it
must not become the record's dereferenceable subject — but it is valuable
provenance and should be preserved.

This module implements the **dual identifier model**:

* **Within-base identifiers** — a client "brings its own identifier" simply by
  choosing the ``PUT`` path or the ``POST`` ``Slug``; that becomes the canonical
  IRI. No special handling here (the normal write path already does it).
* **Foreign primary subject** — if the submitted graph's single typed primary
  subject is an absolute IRI *not* under the identifier base, rebind those
  triples to the canonical IRI and record the original as ``owl:sameAs`` so the
  cross-reference survives.
* **Explicit cross-references** — any ``dct:identifier`` / ``owl:sameAs`` /
  ``skos:exactMatch`` the client attached to ``<>`` are left untouched.

The function is pure (operates on a copy, mirrors
:func:`fdp.metadata.patch.simulate_update`'s discipline) so the LDP layer stays
the only place that commits.
"""

from __future__ import annotations

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from fdp.shared.identifiers import is_under
from fdp.shared.namespaces import OWL

__all__ = ["reconcile_identifiers"]


def reconcile_identifiers(graph: Graph, *, canonical_iri: str, identifier_base: str) -> Graph:
    """Reconcile a submitted record graph against its canonical IRI.

    Args:
        graph: The parsed request body (already parsed with the canonical IRI as
            base, so a relative ``<>`` resolves to ``canonical_iri``).
        canonical_iri: The record's canonical, identifier-base-rooted IRI.
        identifier_base: The persistent PID namespace; used to decide whether the
            submitted subject is "ours" (within base) or foreign.

    Returns:
        ``graph`` unchanged when the client addressed the record canonically
        (via ``<>`` or the canonical IRI). Otherwise a fresh graph whose single
        typed primary subject has been rebound to ``canonical_iri``, with an
        ``owl:sameAs`` back-link added when the original subject was foreign.
    """
    canon = URIRef(str(canonical_iri).rstrip("/"))
    canon_slash = URIRef(str(canon) + "/")

    # Client used <> (or the canonical absolute IRI): primary subject already
    # canonical. Leave any client-supplied dct:identifier/owl:sameAs as authored.
    if next(graph.triples((canon, None, None)), None) is not None:
        return graph
    if next(graph.triples((canon_slash, None, None)), None) is not None:
        return graph

    # Conservative heuristic: a single typed URIRef subject is the record's
    # primary subject. Zero or many → ambiguous (e.g. a record describing several
    # resources); leave the graph as authored rather than guess.
    typed = {
        s
        for s in graph.subjects(RDF.type, None)
        if isinstance(s, URIRef) and s != canon and s != canon_slash
    }
    if len(typed) != 1:
        return graph
    primary = next(iter(typed))

    out = Graph()
    for s, p, o in graph:
        ns = canon if s == primary else s
        no = canon if o == primary else o
        out.add((ns, p, no))
    # A foreign identifier the FDP cannot resolve becomes a cross-reference; a
    # within-base mis-addressing is just corrected to the canonical IRI.
    if not is_under(str(primary), identifier_base):
        out.add((canon, OWL.sameAs, primary))
    return out
