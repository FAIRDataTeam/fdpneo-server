"""SPARQL query rewriter (architecture §9.3, §9.5; ADR-0004).

The rewriter is pure: it takes a :class:`ParsedRead` or
:class:`ParsedUpdate` from :mod:`fdpneo_server.access.parser` plus the user's
authorized graph set (computed by :meth:`PDP.authorized_graphs`) and
produces a *dataset projection* the router forwards to the triple store
via the SPARQL 1.1 Protocol's ``default-graph-uri`` and
``named-graph-uri`` request parameters.

Why protocol parameters instead of textual ``FROM NAMED`` injection:

* Lossless — no algebra → string round-trip; the user's query body is
  passed through unchanged.
* Spec-defined — SPARQL 1.1 Protocol §2.1.4 says protocol params *only*
  apply when the query body does **not** itself contain ``FROM`` or
  ``FROM NAMED``. We honor that: when the user supplied a dataset
  clause, we validate the graphs it names and pass empty protocol params.

**Information-leakage rule (§9.5)**

* If the user typed a graph the FDP cannot authorize, we return 403 with
  a "not authorized for graph X" message. The IRI was already in the
  user's hands — refusing it leaks nothing new.
* If the user named no graphs at all, we silently project the entire
  authorized set; the user has no way to distinguish "no such record"
  from "exists but not authorized".

**Decision table for reads**

| Query has FROM/FROM NAMED? | Inline ``GRAPH <iri>``? | Result                                                       |
|----------------------------|-------------------------|--------------------------------------------------------------|
| yes                        | any                     | Empty protocol params; the query's dataset clause is final.  |
| no                         | yes                     | Project just the inline IRIs via ``named-graph-uri``.        |
| no                         | no                     | Project the entire authorized set via ``named-graph-uri``.    |

In every case, every IRI the user typed (FROM, FROM NAMED, or inline
GRAPH) must be in ``authorized_read``.

**Updates** are simpler. Every explicit target enumerated by the parser
must be in ``authorized_modify``; otherwise we raise
:class:`PolicyViolation`. There is no dataset projection — write
targets are addressed directly by the triple store.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fdpneo_server.access.parser import ParsedRead, ParsedUpdate
from fdpneo_server.shared.errors import PolicyViolation


@dataclass(frozen=True)
class RewrittenRead:
    """Dataset projection for a read query, expressed as protocol params.

    Both fields are empty when the user's own SPARQL body specifies the
    dataset via ``FROM`` / ``FROM NAMED``; the triple store will honor
    the in-query dataset clause in that case (per SPARQL 1.1 Protocol).
    """

    default_graph_uris: tuple[str, ...]
    """Values for the ``default-graph-uri`` request parameter."""

    named_graph_uris: tuple[str, ...]
    """Values for the ``named-graph-uri`` request parameter."""


def rewrite_read(
    parsed: ParsedRead,
    *,
    authorized_read: Iterable[str],
) -> RewrittenRead:
    """Authorize a read query and compute its dataset projection.

    Args:
        parsed: Output of :func:`fdpneo_server.access.parser.parse` for a read query.
        authorized_read: Every graph IRI the subject may read, as returned
            by :meth:`PDP.authorized_graphs(ctx, Action.READ)`.

    Raises:
        PolicyViolation: Any explicit IRI the user typed (FROM,
            FROM NAMED, or inline GRAPH) is not in ``authorized_read``.
    """
    authorized = set(authorized_read)
    _check_authorized(parsed.explicit_default_graphs, authorized)
    _check_authorized(parsed.explicit_named_graphs, authorized)
    _check_authorized(parsed.inline_graph_iris, authorized)

    if parsed.has_dataset_clause:
        # SPARQL 1.1 Protocol §2.1.4: FROM / FROM NAMED in the query body
        # overrides any default-graph-uri / named-graph-uri parameters we
        # would send. Skip them to make that explicit.
        return RewrittenRead(default_graph_uris=(), named_graph_uris=())

    if parsed.inline_graph_iris:
        # The user constrained which named graphs to look at; project
        # exactly those (they're already authorized).
        return RewrittenRead(
            default_graph_uris=(),
            named_graph_uris=parsed.inline_graph_iris,
        )

    # Silent intersection: project the entire authorized set.
    return RewrittenRead(
        default_graph_uris=(),
        named_graph_uris=tuple(sorted(authorized)),
    )


def authorize_update(
    parsed: ParsedUpdate,
    *,
    authorized_modify: Iterable[str],
) -> None:
    """Raise :class:`PolicyViolation` if any update target is unauthorized.

    The parser has already rejected ambiguous-target updates; everything
    in ``parsed.targets`` is an explicit graph IRI the user named.
    """
    authorized = set(authorized_modify)
    for target in parsed.targets:
        if target not in authorized:
            raise PolicyViolation(
                f"not authorized to modify graph {target}",
                details={"graph": target, "action": "modify"},
            )


def _check_authorized(refs: Iterable[str], authorized: set[str]) -> None:
    for iri in refs:
        if iri not in authorized:
            raise PolicyViolation(
                f"not authorized for graph {iri}",
                details={"graph": iri, "action": "read"},
            )


__all__ = ["RewrittenRead", "authorize_update", "rewrite_read"]
