"""SPARQL 1.1 Protocol adapter.

**Responsibilities**

* Issue SPARQL queries, ASK queries, and SPARQL Update statements against
  a configured endpoint.
* Read and write whole named graphs through the SPARQL Graph Store
  Protocol when the operator has configured a graph store endpoint.

**Non-responsibilities**

* Vendor-specific operations (GraphDB repository management, cluster
  sync) — those sit behind capability flags on the settings object and
  are not implemented in this base adapter (ADR-0005).
* Business logic. Callers translate domain concepts into SPARQL.

All HTTP traffic goes through a single :class:`httpx.AsyncClient` owned by
the adapter; call :meth:`close` to release it, or use the adapter as an
async context manager.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Self

import httpx
from rdflib import Graph

if TYPE_CHECKING:
    from fdp.config import TripleStoreSettings


SPARQL_JSON: str = "application/sparql-results+json"
SPARQL_QUERY: str = "application/sparql-query"
SPARQL_UPDATE: str = "application/sparql-update"
TURTLE: str = "text/turtle"

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


class TripleStoreAdapter:
    """Thin async port over the SPARQL 1.1 Protocol."""

    def __init__(
        self,
        settings: TripleStoreSettings,
        client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._client = client

    @classmethod
    def from_settings(cls, settings: TripleStoreSettings) -> Self:
        """Build an adapter with a fresh ``httpx.AsyncClient``.

        Use this in production. Tests can construct the adapter directly,
        passing a client wired to a transport mock such as ``respx``.
        """
        auth: httpx.Auth | None = None
        if settings.username and settings.password:
            auth = httpx.BasicAuth(
                settings.username,
                settings.password.get_secret_value(),
            )
        client = httpx.AsyncClient(auth=auth, timeout=_DEFAULT_TIMEOUT)
        return cls(settings, client)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # --- SPARQL Protocol ----------------------------------------------------

    async def query(
        self,
        sparql: str,
        *,
        accept: str = SPARQL_JSON,
        default_graph_uris: Sequence[str] = (),
        named_graph_uris: Sequence[str] = (),
    ) -> bytes:
        """Execute a SPARQL query and return the raw response body.

        ``Accept`` defaults to SPARQL Results JSON. Override it with a
        Turtle / RDF/XML / N-Triples media type for ``CONSTRUCT`` and
        ``DESCRIBE`` queries.

        The query is sent via SPARQL 1.1 Protocol "query via POST directly"
        (§2.1.3): the SPARQL text is the request body
        (``application/sparql-query``) and ``default_graph_uris`` /
        ``named_graph_uris`` ride in the URL query string as repeated
        ``default-graph-uri`` / ``named-graph-uri`` parameters. We avoid the
        URL-encoded-form variant (§2.1.2) because some backends (Oxigraph)
        reject *repeated* dataset parameters in a form body. Per §2.1.4 the
        server ignores these parameters when the query body itself contains
        ``FROM`` / ``FROM NAMED``.
        """
        params = _dataset_params(default_graph_uris, named_graph_uris)
        response = await self._client.post(
            str(self._settings.query_endpoint),
            content=sparql.encode("utf-8"),
            headers={"Accept": accept, "Content-Type": SPARQL_QUERY},
            params=httpx.QueryParams(params) if params else None,
        )
        response.raise_for_status()
        return response.content

    async def query_stream(
        self,
        sparql: str,
        *,
        accept: str,
        default_graph_uris: Sequence[str] = (),
        named_graph_uris: Sequence[str] = (),
    ) -> AsyncIterator[bytes]:
        """Stream a SPARQL query response, yielding response-body chunks.

        Designed for large ``CONSTRUCT`` / ``DESCRIBE`` payloads so we
        don't buffer the entire RDF dump in memory. Consume via
        ``async for chunk in adapter.query_stream(...)``; when the
        consumer exits, the underlying connection is released.

        Uses the same "query via POST directly" form as :meth:`query`.
        """
        params = _dataset_params(default_graph_uris, named_graph_uris)
        async with self._client.stream(
            "POST",
            str(self._settings.query_endpoint),
            content=sparql.encode("utf-8"),
            headers={"Accept": accept, "Content-Type": SPARQL_QUERY},
            params=httpx.QueryParams(params) if params else None,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                yield chunk

    async def ask(self, sparql: str) -> bool:
        """Execute an ``ASK`` query and return its boolean result."""
        body = await self.query(sparql, accept=SPARQL_JSON)
        payload: dict[str, object] = json.loads(body)
        return bool(payload.get("boolean", False))

    async def update(
        self,
        sparql: str,
        *,
        using_graph_uris: Sequence[str] = (),
        using_named_graph_uris: Sequence[str] = (),
    ) -> None:
        """Execute a SPARQL Update statement (no response body expected).

        ``using_graph_uris`` and ``using_named_graph_uris`` are forwarded
        as URL query-string parameters per SPARQL 1.1 Protocol §2.2.1; the
        triple store uses them as the dataset for ``WHERE`` clauses in
        ``INSERT``/``DELETE`` updates that don't specify their own
        ``USING`` / ``WITH``. The router scopes update WHEREs to the
        user's authorized read set to prevent the WHERE from observing
        unauthorized data even when the write target is authorized.
        """
        params = _update_params(using_graph_uris, using_named_graph_uris)
        response = await self._client.post(
            str(self._settings.update_endpoint),
            content=sparql.encode("utf-8"),
            headers={"Content-Type": SPARQL_UPDATE},
            params=httpx.QueryParams(params) if params else None,
        )
        response.raise_for_status()

    # --- Graph Store Protocol ----------------------------------------------

    async def ingest_graph(
        self,
        graph_uri: str,
        data: bytes | str | Graph,
        *,
        mime: str = TURTLE,
    ) -> None:
        """Append triples to a named graph via Graph Store Protocol ``POST``."""
        endpoint = self._require_graph_store_endpoint()
        body = self._serialize(data, mime)
        response = await self._client.post(
            endpoint,
            params={"graph": graph_uri},
            content=body,
            headers={"Content-Type": mime},
        )
        response.raise_for_status()

    async def replace_graph(
        self,
        graph_uri: str,
        data: bytes | str | Graph,
        *,
        mime: str = TURTLE,
    ) -> None:
        """Replace all triples in a named graph via Graph Store Protocol ``PUT``."""
        endpoint = self._require_graph_store_endpoint()
        body = self._serialize(data, mime)
        response = await self._client.put(
            endpoint,
            params={"graph": graph_uri},
            content=body,
            headers={"Content-Type": mime},
        )
        response.raise_for_status()

    async def drop_graph(self, graph_uri: str) -> None:
        """Drop a named graph.

        Uses Graph Store Protocol ``DELETE`` when a graph store endpoint is
        configured; falls back to ``DROP SILENT GRAPH`` via SPARQL Update
        otherwise.
        """
        if self._settings.graph_store_endpoint is not None:
            response = await self._client.delete(
                str(self._settings.graph_store_endpoint),
                params={"graph": graph_uri},
            )
            # A missing graph is not an error: dropping is idempotent, matching
            # the SPARQL ``DROP SILENT GRAPH`` fallback below. Records without a
            # materialized sibling (e.g. a managed schema/policy/license never
            # gets an audit graph) would otherwise 404 the whole delete.
            if response.status_code == 404:
                return
            response.raise_for_status()
            return
        await self.update(f"DROP SILENT GRAPH <{graph_uri}>")

    # --- Helpers -----------------------------------------------------------

    def _require_graph_store_endpoint(self) -> str:
        if self._settings.graph_store_endpoint is None:
            raise ValueError(
                "TripleStoreSettings.graph_store_endpoint is required for "
                "ingest_graph / replace_graph. Configure FDP_TRIPLESTORE_GRAPH_STORE_ENDPOINT."
            )
        return str(self._settings.graph_store_endpoint)

    @staticmethod
    def _serialize(data: bytes | str | Graph, mime: str) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
        # rdflib.Graph
        return data.serialize(format=_rdflib_format_for(mime)).encode("utf-8")


def _dataset_params(
    default_graph_uris: Sequence[str],
    named_graph_uris: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """Build the URL query-string dataset parameters for a query.

    Repeated ``default-graph-uri`` / ``named-graph-uri`` pairs per SPARQL
    1.1 Protocol §2.1.4, carried in the URL query string (not the body) so
    repeated values are accepted by every tested backend.
    """
    params: list[tuple[str, str]] = []
    for graph_uri in default_graph_uris:
        params.append(("default-graph-uri", graph_uri))
    for graph_uri in named_graph_uris:
        params.append(("named-graph-uri", graph_uri))
    return tuple(params)


def _update_params(
    using_graph_uris: Sequence[str],
    using_named_graph_uris: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    params: list[tuple[str, str]] = []
    for graph_uri in using_graph_uris:
        params.append(("using-graph-uri", graph_uri))
    for graph_uri in using_named_graph_uris:
        params.append(("using-named-graph-uri", graph_uri))
    return tuple(params)


def _rdflib_format_for(mime: str) -> str:
    """Map a SPARQL media type to the rdflib format identifier."""
    mapping = {
        "text/turtle": "turtle",
        "application/n-triples": "nt",
        "application/rdf+xml": "xml",
        "application/ld+json": "json-ld",
        "application/n-quads": "nquads",
        "application/trig": "trig",
    }
    return mapping.get(mime, "turtle")


__all__ = ["SPARQL_JSON", "SPARQL_QUERY", "SPARQL_UPDATE", "TURTLE", "TripleStoreAdapter"]
