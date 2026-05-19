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
from types import TracebackType
from typing import TYPE_CHECKING, Self

import httpx
from rdflib import Graph

if TYPE_CHECKING:
    from fdp.config import TripleStoreSettings


SPARQL_JSON: str = "application/sparql-results+json"
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

    async def query(self, sparql: str, *, accept: str = SPARQL_JSON) -> bytes:
        """Execute a SPARQL query and return the raw response body.

        ``Accept`` defaults to SPARQL Results JSON. Override it with a
        Turtle / RDF/XML / N-Triples media type for ``CONSTRUCT`` and
        ``DESCRIBE`` queries.
        """
        response = await self._client.post(
            str(self._settings.query_endpoint),
            data={"query": sparql},
            headers={"Accept": accept},
        )
        response.raise_for_status()
        return response.content

    async def ask(self, sparql: str) -> bool:
        """Execute an ``ASK`` query and return its boolean result."""
        body = await self.query(sparql, accept=SPARQL_JSON)
        payload: dict[str, object] = json.loads(body)
        return bool(payload.get("boolean", False))

    async def update(self, sparql: str) -> None:
        """Execute a SPARQL Update statement (no response body expected)."""
        response = await self._client.post(
            str(self._settings.update_endpoint),
            content=sparql.encode("utf-8"),
            headers={"Content-Type": SPARQL_UPDATE},
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


__all__ = ["SPARQL_JSON", "SPARQL_UPDATE", "TURTLE", "TripleStoreAdapter"]
