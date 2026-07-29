"""Tests for ``fdpneo_server.metadata.pid.rebase`` — identifier-base adoption migration."""

from __future__ import annotations

import json

import pytest
from rdflib import BNode, Graph, Literal, URIRef

from fdpneo_server.metadata.pid.rebase import _rewrite_term, rebase_identifiers, rebased

OLD = "http://localhost:8000"
NEW = "https://w3id.org/myfdp"


class _FakeAdapter:
    """In-memory stand-in: named graph URI → N-Triples body."""

    def __init__(self, graphs: dict[str, str]) -> None:
        self.graphs = dict(graphs)
        self.dropped: list[str] = []

    async def query(self, sparql: str, *, accept: str = "application/sparql-results+json") -> bytes:
        if sparql.strip().startswith("SELECT"):
            bindings = [{"g": {"value": g}} for g in self.graphs]
            return json.dumps({"results": {"bindings": bindings}}).encode()
        # CONSTRUCT <g> — pull the graph URI out of the interpolated query.
        uri = sparql.split("GRAPH <", 1)[1].split(">", 1)[0]
        return self.graphs.get(uri, "").encode()

    async def replace_graph(self, graph_uri: str, data: str | bytes, *, mime: str) -> None:
        self.graphs[graph_uri] = data.decode() if isinstance(data, bytes) else data

    async def drop_graph(self, graph_uri: str) -> None:
        self.graphs.pop(graph_uri, None)
        self.dropped.append(graph_uri)


def _nt(*triples: str) -> str:
    return "\n".join(triples) + "\n"


@pytest.mark.unit
async def test_moves_graphs_and_rewrites_cross_references() -> None:
    catalog = f"{OLD}/catalog/c1"
    dataset = f"{OLD}/catalog/c1/dataset/d1"
    adapter = _FakeAdapter(
        {
            catalog: _nt(
                f"<{catalog}> <http://www.w3.org/ns/dcat#dataset> <{dataset}> .",
                f'<{catalog}> <http://purl.org/dc/terms/title> "C1" .',
            ),
            dataset: _nt(f"<{dataset}> <http://purl.org/dc/terms/isPartOf> <{catalog}> ."),
        }
    )

    report = await rebase_identifiers(adapter=adapter, old_base=OLD, new_base=NEW)  # type: ignore[arg-type]

    assert report.count == 2
    new_catalog = f"{NEW}/catalog/c1"
    new_dataset = f"{NEW}/catalog/c1/dataset/d1"
    # Old graphs gone, new graphs present.
    assert catalog in adapter.dropped and dataset in adapter.dropped
    assert new_catalog in adapter.graphs
    # Cross-reference rewritten to the new base.
    g = Graph()
    g.parse(data=adapter.graphs[new_dataset], format="nt")
    parts = list(g.objects())
    assert any(str(o) == new_catalog for o in parts)


@pytest.mark.unit
async def test_idempotent_second_pass_is_noop() -> None:
    adapter = _FakeAdapter({f"{NEW}/catalog/c1": _nt(f"<{NEW}/catalog/c1> a <X> .")})
    report = await rebase_identifiers(adapter=adapter, old_base=OLD, new_base=NEW)  # type: ignore[arg-type]
    assert report.count == 0
    assert adapter.dropped == []


@pytest.mark.unit
async def test_same_base_is_noop() -> None:
    adapter = _FakeAdapter({f"{OLD}/catalog/c1": _nt(f"<{OLD}/catalog/c1> a <X> .")})
    report = await rebase_identifiers(adapter=adapter, old_base=OLD, new_base=OLD)  # type: ignore[arg-type]
    assert report.count == 0


@pytest.mark.unit
async def test_dry_run_writes_nothing() -> None:
    catalog = f"{OLD}/catalog/c1"
    adapter = _FakeAdapter({catalog: _nt(f"<{catalog}> a <X> .")})
    report = await rebase_identifiers(
        adapter=adapter,  # type: ignore[arg-type]
        old_base=OLD,
        new_base=NEW,
        dry_run=True,
    )
    assert report.count == 1  # reported
    assert adapter.dropped == []  # but nothing changed
    assert catalog in adapter.graphs


# --- pure IRI-matching helpers --------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (OLD, NEW),  # exact base match
        (f"{OLD}/catalog/c1", f"{NEW}/catalog/c1"),  # path-separated descendant
        (f"{OLD}#frag", f"{NEW}#frag"),  # fragment separator
        (f"{OLD}?q=1", f"{NEW}?q=1"),  # query separator
    ],
)
def test_rebased_rewrites_old_base(value: str, expected: str) -> None:
    assert rebased(value, OLD, NEW) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "https://other.example/catalog/c1",  # unrelated base
        f"{OLD}0/catalog",  # boundary guard: ":8000" must not match ":80000"
        f"{OLD}xyz",  # string-prefix but no path/fragment/query separator
        f"{NEW}/catalog/c1",  # already under the new base
    ],
)
def test_rebased_leaves_unaffected_values(value: str) -> None:
    assert rebased(value, OLD, NEW) is None


@pytest.mark.unit
def test_rewrite_term_rewrites_uriref_under_old_base() -> None:
    assert _rewrite_term(URIRef(f"{OLD}/x"), OLD, NEW) == URIRef(f"{NEW}/x")


@pytest.mark.unit
def test_rewrite_term_leaves_non_matching_terms_untouched() -> None:
    # A Literal that merely *looks* like an old-base IRI is never rewritten.
    lit = Literal(f"{OLD}/looks-like-a-uri")
    assert _rewrite_term(lit, OLD, NEW) == lit
    foreign = URIRef("https://example.org/x")
    assert _rewrite_term(foreign, OLD, NEW) == foreign
    bnode = BNode()
    assert _rewrite_term(bnode, OLD, NEW) == bnode
