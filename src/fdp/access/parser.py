"""SPARQL query parser + classifier (architecture §9.1, ADR-0004).

Lifts a SPARQL 1.1 string into a typed parse result the rewriter can
authorize against:

* :class:`ParsedRead` — query form (SELECT / ASK / CONSTRUCT / DESCRIBE)
  plus the explicit ``FROM`` / ``FROM NAMED`` / inline ``GRAPH <iri>``
  references the user typed.
* :class:`ParsedUpdate` — the union of explicit graph targets across
  every operation in the update request.

The parser is the access module's gatekeeper. It rejects:

* Malformed SPARQL (400).
* ``SERVICE`` clauses anywhere (400) — architecture §9.2, no federation in v1.
* Update operations whose target graph is implicit (400) — architecture
  §9.3 restricts updates to forms where the target is unambiguous, so the
  rewriter never has to run a ``WHERE`` to discover what would be written.

The parser is pure. The rewriter (3.2) consumes its output to make
authorization decisions; the router (3.3) drives the whole pipeline.

**Operational coverage**

| Op             | Explicit target lives in                      |
|----------------|-----------------------------------------------|
| ``InsertData`` | ``GRAPH <g> { … }`` inside the data block     |
| ``DeleteData`` | same                                          |
| ``DeleteWhere``| same                                          |
| ``Modify``     | ``WITH <g>`` *or* ``GRAPH <g>`` in every      |
|                | template clause                               |
| ``Load``       | ``LOAD <src> INTO GRAPH <g>``                 |
| ``Clear``      | ``CLEAR GRAPH <g>`` (not DEFAULT / NAMED / ALL) |
| ``Drop``       | ``DROP GRAPH <g>``                            |
| ``Create``     | ``CREATE GRAPH <g>``                          |
| ``Copy/Move/Add`` | both source and destination as IRIs         |

Anything else is *ambiguous* and rejected with a 400 that points at
architecture §9.3.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rdflib.plugins.sparql.algebra import translateUpdate
from rdflib.plugins.sparql.parser import parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.plugins.sparql.processor import prepareQuery
from rdflib.term import URIRef

from fdp.shared.errors import BadRequest


class QueryForm(StrEnum):
    """The four SPARQL read forms."""

    SELECT = "SELECT"
    ASK = "ASK"
    CONSTRUCT = "CONSTRUCT"
    DESCRIBE = "DESCRIBE"


_FORM_BY_ALGEBRA_NAME = {
    "SelectQuery": QueryForm.SELECT,
    "AskQuery": QueryForm.ASK,
    "ConstructQuery": QueryForm.CONSTRUCT,
    "DescribeQuery": QueryForm.DESCRIBE,
}


@dataclass(frozen=True)
class ParsedRead:
    """A read query plus the graph references the user typed.

    The three reference sources are kept separate because they have
    distinct semantics under SPARQL 1.1 Protocol §2.1.4: a query that
    contains ``FROM`` or ``FROM NAMED`` *describes its own dataset*, and
    protocol-level ``default-graph-uri`` / ``named-graph-uri`` parameters
    are ignored. Inline ``GRAPH <iri> { … }`` in the WHERE pattern is a
    *graph pattern*, not a dataset clause, so a query with only inline
    refs still defers its dataset to protocol params.

    ``GRAPH ?var { … }`` contributes nothing — the variable iterates over
    whatever named graphs end up in the dataset, not a specific URI the
    user asked for.
    """

    form: QueryForm
    explicit_default_graphs: tuple[str, ...]
    """IRIs from ``FROM <iri>`` clauses."""

    explicit_named_graphs: tuple[str, ...]
    """IRIs from ``FROM NAMED <iri>`` clauses."""

    inline_graph_iris: tuple[str, ...]
    """IRIs referenced via ``GRAPH <iri> { … }`` inside the WHERE pattern."""

    @property
    def has_dataset_clause(self) -> bool:
        """True iff the query body specifies its dataset via FROM / FROM NAMED.

        When true, SPARQL 1.1 Protocol overrides any ``default-graph-uri``
        / ``named-graph-uri`` request parameters with the query's own
        dataset clause.
        """
        return bool(self.explicit_default_graphs or self.explicit_named_graphs)


@dataclass(frozen=True)
class ParsedUpdate:
    """An update request reduced to its explicit graph targets.

    Every operation in the request must declare its target(s) explicitly;
    the parser raises :class:`BadRequest` otherwise. ``targets`` is the
    flat, ordered union across all operations and may contain duplicates
    when the user repeated a graph.
    """

    targets: tuple[str, ...]


_READ_KEYWORDS = frozenset({"SELECT", "ASK", "CONSTRUCT", "DESCRIBE"})
_UPDATE_KEYWORDS = frozenset(
    {"INSERT", "DELETE", "LOAD", "CLEAR", "CREATE", "DROP", "COPY", "MOVE", "ADD", "WITH"}
)
_PROLOGUE_RE = re.compile(
    r"\s*(?:BASE\s*<[^>]*>|PREFIX\s+\w*:\s*<[^>]*>)\s*",
    re.IGNORECASE,
)
_KEYWORD_RE = re.compile(r"\s*(\w+)")
_COMMENT_RE = re.compile(r"#[^\n]*")
_AMBIGUOUS_HINT = (
    "use WITH <graph> or place the triples inside GRAPH <graph> {…} "
    "(see architecture §9.3)"
)


def parse(sparql: str) -> ParsedRead | ParsedUpdate:
    """Parse ``sparql`` into a :class:`ParsedRead` or :class:`ParsedUpdate`.

    Raises :class:`BadRequest` for any failure — empty body, malformed
    SPARQL, SERVICE clauses, unknown forms, or ambiguous update targets.
    The error message is safe to surface to the client.
    """
    body = sparql.strip()
    if not body:
        raise BadRequest("SPARQL request body is empty")
    keyword = _first_keyword(body)
    if keyword in _READ_KEYWORDS:
        return _parse_read(body)
    if keyword in _UPDATE_KEYWORDS:
        return _parse_update(body)
    raise BadRequest(f"unrecognized SPARQL operation: {keyword or '<empty>'}")


# --- read path --------------------------------------------------------------


def _parse_read(body: str) -> ParsedRead:
    try:
        prepared = prepareQuery(body)
    except Exception as err:
        raise BadRequest(f"could not parse SPARQL query: {err}") from err
    algebra = prepared.algebra
    form = _FORM_BY_ALGEBRA_NAME.get(algebra.name)
    if form is None:
        raise BadRequest(f"unsupported SPARQL query form: {algebra.name}")
    _reject_service(algebra)

    default_graphs: list[str] = []
    named_graphs: list[str] = []
    for clause in algebra.datasetClause or ():
        if clause.default is not None:
            default_graphs.append(str(clause.default))
        elif clause.named is not None:
            named_graphs.append(str(clause.named))

    inline_graphs: list[str] = []
    seen_inline: set[str] = set()
    for iri in _collect_inline_graph_iris(algebra):
        if iri not in seen_inline:
            inline_graphs.append(iri)
            seen_inline.add(iri)

    return ParsedRead(
        form=form,
        explicit_default_graphs=tuple(default_graphs),
        explicit_named_graphs=tuple(named_graphs),
        inline_graph_iris=tuple(inline_graphs),
    )


# --- update path ------------------------------------------------------------


def _parse_update(body: str) -> ParsedUpdate:
    try:
        update = translateUpdate(parseUpdate(body))
    except Exception as err:
        raise BadRequest(f"could not parse SPARQL update: {err}") from err
    operations = update.algebra
    targets: list[str] = []
    for op in operations:
        _reject_service(op)
        op_targets = _explicit_targets(op)
        if op_targets is None:
            raise BadRequest(
                f"{op.name} requires explicit graph targets; {_AMBIGUOUS_HINT}"
            )
        targets.extend(op_targets)
    return ParsedUpdate(targets=tuple(targets))


def _explicit_targets(op: CompValue) -> list[str] | None:
    """Return the explicit graph targets of ``op``, or ``None`` if ambiguous."""
    name = op.name
    if name in {"InsertData", "DeleteData", "DeleteWhere"}:
        return _quads_targets(op)
    if name == "Modify":
        with_clause = op.withClause
        if isinstance(with_clause, URIRef):
            return [str(with_clause)]
        # Without WITH, every triple in both templates must be inside a
        # GRAPH (template's ``triples`` list must be empty).
        accumulated: list[str] = []
        for clause in (op.delete, op.insert):
            if clause is None:
                continue
            sub = _quads_targets(clause)
            if sub is None:
                return None
            accumulated.extend(sub)
        return accumulated
    if name in {"Load", "Clear", "Drop", "Create"}:
        target = op.graphiri
        # CLEAR DEFAULT / CLEAR ALL / CLEAR NAMED store the categorical
        # name as a string instead of a URIRef — that's the "ambiguous"
        # tell. LOAD without INTO GRAPH simply has no ``graphiri`` set.
        return [str(target)] if isinstance(target, URIRef) else None
    if name in {"Copy", "Move", "Add"}:
        graphs: list[Any] = list(op.graph or [])
        urls: list[str] = []
        for g in graphs:
            if not isinstance(g, URIRef):
                return None
            urls.append(str(g))
        return urls
    return None


def _quads_targets(node: CompValue) -> list[str] | None:
    """Return per-clause target graphs, or ``None`` for default-graph triples."""
    triples: list[Any] = list(node.triples or [])
    if triples:
        return None
    quads: dict[Any, Any] = dict(node.quads or {})
    targets: list[str] = []
    for graph_iri in quads:
        if not isinstance(graph_iri, URIRef):
            return None
        targets.append(str(graph_iri))
    return targets


# --- helpers ----------------------------------------------------------------


def _first_keyword(body: str) -> str | None:
    """Return the first SPARQL keyword after BASE/PREFIX prologue + comments."""
    stripped = _COMMENT_RE.sub("", body)
    pos = 0
    while True:
        match = _PROLOGUE_RE.match(stripped, pos)
        if match is None or match.end() == pos:
            break
        pos = match.end()
    match = _KEYWORD_RE.match(stripped, pos)
    return match.group(1).upper() if match else None


def _reject_service(node: Any) -> None:
    for descendant in _walk_compvalues(node):
        if descendant.name == "ServiceGraphPattern":
            raise BadRequest("SERVICE clauses are not supported (no federation in v1)")


def _collect_inline_graph_iris(node: Any) -> list[str]:
    found: list[str] = []
    for descendant in _walk_compvalues(node):
        if descendant.name == "Graph":
            term = descendant.term
            if isinstance(term, URIRef):
                found.append(str(term))
    return found


def _walk_compvalues(node: Any) -> Iterator[CompValue]:
    """Yield ``node`` and every nested CompValue descendant."""
    if isinstance(node, CompValue):
        yield node
        for value in node.values():  # pyright: ignore[reportUnknownVariableType]
            yield from _walk_compvalues(value)
    elif isinstance(node, (list, tuple, set, frozenset)):
        for item in node:  # pyright: ignore[reportUnknownVariableType]
            yield from _walk_compvalues(item)
    elif isinstance(node, dict):
        for value in node.values():  # pyright: ignore[reportUnknownVariableType]
            yield from _walk_compvalues(value)


__all__ = [
    "ParsedRead",
    "ParsedUpdate",
    "QueryForm",
    "parse",
]
