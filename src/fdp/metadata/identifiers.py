"""Dual identifier reconciliation on write (ADR-0014, refined by ADR-0017).

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
  triples to the canonical IRI and record the original as a **structured
  alternative identifier**: ``dct:identifier`` (literal) plus an
  ``adms:identifier`` node (``adms:Identifier`` with ``skos:notation`` typed
  ``xsd:anyURI``). The server **never** asserts ``owl:sameAs`` — it only observed
  that the client addressed the record by that IRI, not that the two resources
  are identical (ADR-0017 §1: "sameAs is not the same").
* **Explicit cross-references** — any ``dct:identifier`` / ``adms:identifier`` /
  ``owl:sameAs`` / ``skos:exactMatch`` the client attached to ``<>`` are left
  untouched; a client that truly means identity says so itself and owns the claim.

The function is pure (operates on a copy, mirrors
:func:`fdp.metadata.patch.simulate_update`'s discipline) so the LDP layer stays
the only place that commits.
"""

from __future__ import annotations

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF

from fdp.shared.errors import AmbiguousSubject
from fdp.shared.identifiers import is_under
from fdp.shared.namespaces import ADMS, DCT, SKOS, XSD

__all__ = ["reconcile_identifiers", "record_alternative_identifier"]


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
        typed primary subject has been rebound to ``canonical_iri``, with the
        original recorded as structured alternative identifiers (``dct:identifier``
        + ``adms:identifier``) when the subject was foreign.

    Raises:
        AmbiguousSubject: when the body neither addresses the record canonically
            nor carries exactly one typed IRI subject to rebind (ADR-0016 §1: the
            canonical-subject invariant is unconditional — no "store as authored"
            escape hatch that would key a graph under an IRI it never mentions).
    """
    canon = URIRef(str(canonical_iri).rstrip("/"))
    canon_slash = URIRef(str(canon) + "/")

    # Client used <> (or the canonical absolute IRI): primary subject already
    # canonical. Leave any client-supplied dct:identifier/owl:sameAs as authored.
    if next(graph.triples((canon, None, None)), None) is not None:
        return graph
    if next(graph.triples((canon_slash, None, None)), None) is not None:
        return graph

    # A single typed URIRef subject is the record's primary subject and is
    # rebound to the canonical IRI. Zero, many, or a blank-node-only body is
    # ambiguous — reject it rather than store the graph under a canonical key it
    # never mentions (ADR-0016 §1; the canonical-subject invariant is unconditional).
    typed = {
        s
        for s in graph.subjects(RDF.type, None)
        if isinstance(s, URIRef) and s != canon and s != canon_slash
    }
    if len(typed) != 1:
        raise AmbiguousSubject(
            "request body must address the record as <> or its canonical IRI, or "
            "contain exactly one typed IRI subject to rebind to the canonical IRI; "
            f"found {len(typed)}",
            details={
                "canonical_iri": str(canon),
                "typed_subjects": sorted(str(s) for s in typed),
            },
        )
    primary = next(iter(typed))

    out = Graph()
    for s, p, o in graph:
        ns = canon if s == primary else s
        no = canon if o == primary else o
        out.add((ns, p, no))
    # A foreign identifier the FDP cannot resolve is preserved as a structured
    # alternative identifier (DCAT 3 / ADMS), never as owl:sameAs (ADR-0017 §1).
    # A within-base mis-addressing is just silently corrected to the canonical IRI.
    if not is_under(str(primary), identifier_base):
        record_alternative_identifier(out, canon, str(primary))
    return out


def record_alternative_identifier(graph: Graph, canonical: URIRef, foreign_iri: str) -> None:
    """Record ``foreign_iri`` as a structured alternative identifier of ``canonical``.

    Emits ``dct:identifier`` (a plain literal, the DCAT 3 lightweight form) and a
    typed ``adms:identifier`` node carrying the notation as ``xsd:anyURI`` — what
    the server actually knows ("this is also an identifier of this resource"),
    without ``owl:sameAs``'s full-identity commitment.
    """
    graph.add((canonical, DCT.identifier, Literal(foreign_iri)))
    node = BNode()
    graph.add((canonical, ADMS.identifier, node))
    graph.add((node, RDF.type, ADMS.Identifier))
    graph.add((node, SKOS.notation, Literal(foreign_iri, datatype=XSD.anyURI)))
