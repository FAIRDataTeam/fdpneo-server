"""Unit tests for :func:`fdpneo_server.data.distributions.resolve_distribution`."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from rdflib import Graph, URIRef

from fdpneo_server.data.distributions import resolve_distribution
from fdpneo_server.shared.errors import NotFound
from fdpneo_server.shared.namespaces import DCAT, DCT

DIST = "https://fdp.example/data/dist-1"
RIGHTS = "https://fdp.example/offers/public"


@dataclass
class _FakeRepo:
    graph: Graph

    async def get_graph(self, record_uri: str) -> Graph:
        del record_uri
        return self.graph


def _graph(
    *, with_download: bool = True, with_access: bool = True, with_rights: bool = True
) -> Graph:
    g = Graph()
    subject = URIRef(DIST)
    g.add((subject, DCAT.Distribution, URIRef("https://example.org/type")))  # marker triple
    if with_download:
        g.add((subject, DCAT.downloadURL, URIRef("https://files.example.org/d1.csv")))
    if with_access:
        g.add((subject, DCAT.accessURL, URIRef("https://fdp.example/data/dist-1/sparql")))
    if with_rights:
        g.add((subject, DCT.rights, URIRef(RIGHTS)))
    return g


# --- happy paths -----------------------------------------------------------


@pytest.mark.unit
async def test_resolve_returns_all_three_properties() -> None:
    repo = _FakeRepo(_graph())
    info = await resolve_distribution(DIST, repository=repo)  # type: ignore[arg-type]
    assert info.iri == DIST
    assert info.download_url == "https://files.example.org/d1.csv"
    assert info.access_url == "https://fdp.example/data/dist-1/sparql"
    assert info.rights_iri == RIGHTS


@pytest.mark.unit
async def test_resolve_has_download_and_access_flags_track_presence() -> None:
    info = await resolve_distribution(
        DIST,
        repository=_FakeRepo(_graph(with_download=False)),  # type: ignore[arg-type]
    )
    assert info.has_download is False
    assert info.has_access is True

    info = await resolve_distribution(
        DIST,
        repository=_FakeRepo(_graph(with_access=False)),  # type: ignore[arg-type]
    )
    assert info.has_download is True
    assert info.has_access is False


@pytest.mark.unit
async def test_resolve_returns_none_for_missing_optional_fields() -> None:
    info = await resolve_distribution(
        DIST,
        repository=_FakeRepo(  # type: ignore[arg-type]
            _graph(with_download=False, with_access=False, with_rights=False)
        ),
    )
    assert info.download_url is None
    assert info.access_url is None
    assert info.rights_iri is None


# --- 404 ------------------------------------------------------------------


@pytest.mark.unit
async def test_resolve_raises_not_found_for_empty_graph() -> None:
    with pytest.raises(NotFound) as exc:
        await resolve_distribution(DIST, repository=_FakeRepo(Graph()))  # type: ignore[arg-type]
    assert DIST in str(exc.value)


# --- multi-value tolerance ------------------------------------------------


@pytest.mark.unit
async def test_resolve_picks_one_object_when_multiple_present() -> None:
    """Multi-valued downloadURL is out of v1 scope; the resolver picks one."""
    g = _graph()
    g.add((URIRef(DIST), DCAT.downloadURL, URIRef("https://files.example.org/alt.csv")))
    info = await resolve_distribution(DIST, repository=_FakeRepo(g))  # type: ignore[arg-type]
    assert info.download_url in (
        "https://files.example.org/d1.csv",
        "https://files.example.org/alt.csv",
    )
