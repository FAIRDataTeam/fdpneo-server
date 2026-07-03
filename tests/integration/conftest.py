"""Shared integration fixtures.

A **GraphDB**-backed triple store for the end-to-end tests that exercise the
SPARQL endpoint's named-graph *projection* (multi-graph reads). The FDP relies on
repeated-named-graph-uri isolation (audit R-03) for that projection; Oxigraph
does not honour it, so the startup self-test disables multi-graph reads and the
SPARQL endpoint returns 503. GraphDB (the recommended default store, pinned to
the deployment version) honours it, so those tests spin one up with the same
repository configuration the deployment ships.

Single-graph tests (CRUD, containment, persistent identifiers, state-gated GET,
the Postgres-backed search projection) keep using the lighter, per-module
Oxigraph fixtures — they never touch multi-graph SPARQL and don't need this.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

_GRAPHDB_IMAGE = "ontotext/graphdb:10.7.0"
_GRAPHDB_PORT = 7200
_FDP_REPO = "fdp"
# The deployment repository config (no inference, context index on) — the same
# file deploy/stack's graphdb-init POSTs. Keeps test + prod store semantics identical.
_REPO_CONFIG = Path(__file__).resolve().parents[2] / "deploy" / "graphdb" / "fdp-repo-config.ttl"
# GraphDB's ready line (see `docker logs` of a running instance).
_READY_LOG = "Started GraphDB in workbench mode"


@dataclass(frozen=True)
class GraphDBStore:
    """SPARQL 1.1 Protocol endpoints for a GraphDB repository."""

    base: str
    repo: str = _FDP_REPO

    @property
    def query(self) -> str:
        return f"{self.base}/repositories/{self.repo}"

    @property
    def update(self) -> str:
        return f"{self.base}/repositories/{self.repo}/statements"

    @property
    def graph_store(self) -> str:
        return f"{self.base}/repositories/{self.repo}/rdf-graphs/service"


@pytest.fixture
def graphdb_store() -> Iterator[GraphDBStore]:
    """A fresh GraphDB container with the `fdp` repository created.

    Function-scoped so every test gets a clean store (matching the Oxigraph
    fixtures it replaces). Only one GraphDB is alive at a time, so it coexists
    with a modest Docker memory budget.
    """
    container = (
        DockerContainer(_GRAPHDB_IMAGE)
        .with_exposed_ports(_GRAPHDB_PORT)
        .with_env("GDB_JAVA_OPTS", "-Xmx1g -Xms512m")
        .waiting_for(LogMessageWaitStrategy(_READY_LOG).with_startup_timeout(180))
    )
    with container:
        base = (
            f"http://{container.get_container_host_ip()}"
            f":{container.get_exposed_port(_GRAPHDB_PORT)}"
        )
        _create_repository(base)
        yield GraphDBStore(base=base)


def _create_repository(base: str) -> None:
    """POST the deployment repo config to GraphDB; retry until it takes.

    The workbench-ready log fires slightly before the REST API accepts writes,
    so poll with a short deadline and treat "already exists" as success.
    """
    config = _REPO_CONFIG.read_bytes()
    deadline = time.monotonic() + 60
    last_err: object = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.post(
                f"{base}/rest/repositories",
                files={"config": ("fdp-repo-config.ttl", config, "text/turtle")},
                timeout=10.0,
            )
            if resp.status_code in (200, 201, 409):  # 409 == already exists
                return
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as exc:
            last_err = exc
        time.sleep(1.0)
    raise RuntimeError(f"GraphDB '{_FDP_REPO}' repository creation failed: {last_err}")
