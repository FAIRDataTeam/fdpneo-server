# ADR-0018: Agent consumption via a standalone MCP sidecar (`fdp-mcp`)

**Status:** Accepted (server-side support implemented in v0.6.0; bridge is `fdp-mcp` v0.1.0)
**Date:** 2026-07-03
**See also:** [Agent Consumption Vision](../architecture/agent-consumption-vision.md)
(motivation, demo strategy, phasing), ADR-0011 (API keys), ADR-0004/ADR-0007
(SPARQL/graph security model), ADR-0010 (publication state).

> Supersedes an earlier draft of this ADR (same number, never accepted) that
> proposed embedding the MCP server in the monolith. The trade-off analysis is
> retained under *Alternatives considered*.
>
> **Superseded in part by [ADR-0024](0024-mcp-opt-in-write-tools.md)
> (2026-08-26):** the "no mutation tools" clause of §5 is replaced by an
> opt-in write extension (four tools behind `FDP_MCP_ENABLE_WRITE`). All other
> decisions here — sidecar placement, public surface only, credential
> pass-through with no authorization logic, strict egress, API honesty — stand.

## Context

The FDP ecosystem provisions rich, SHACL-validated, DCAT-typed metadata but
has few consumers of it. The vision document identifies AI agents as the
natural first-class consumer and the Model Context Protocol (MCP) as the
de-facto standard for exposing capabilities to them. The goal is a consumption
layer that lets any off-the-shelf MCP client converse with FDP metadata —
records, schemas, access conditions — grounded in real data.

Two placements were analysed: **embedded** (a new bounded context in the
FastAPI monolith) and **sidecar** (a standalone bridge service consuming the
FDP through its public HTTP surface). Both were worked through in full; the
deciding forces were:

- **Reach over packaging.** The live FDP network today is overwhelmingly *not*
  FDPneo — it is dominated by the Java reference implementation (and the
  ERDERA VP network runs on it). An embedded server would make only FDPneo
  deployments agent-ready; a bridge over the *specified public surface* makes
  the existing network agent-ready, today, without asking anyone to migrate.
  For a demonstrator whose purpose is to inspire the community, that reach is
  worth more than single-binary convenience.
- **API honesty.** A bridge can use only what the public, specified surface
  exposes. Building it is therefore a continuous audit of our own spec
  compliance: every place the bridge needs a private interface is a
  documented gap in the public API, feeding the server roadmap. An embedded
  implementation would silently paper over exactly those gaps.
- **Isolation and lifecycle.** MCP is a young protocol; its churn should land
  in a small adapter's release cycle, not the metadata server's. A separate
  process also isolates the long-lived-connection ingress from LDP traffic.
- **Accepted costs.** A second deployable (mitigated by shipping it in the
  standard compose profile), credential pass-through for non-anonymous access,
  and a capability ceiling equal to the public API — which per the above is a
  feature, not only a cost.

## Decision

### 1. Build `fdp-mcp`, a standalone sidecar, as a third repo beside `server` and `client`

- New top-level component `mcp/` in the fdp-neo folder, treated as its own
  repository with its own Docker image, release cycle, `CLAUDE.md`, and
  `TASKS.md` — mirroring the server/client split.
- The bridge targets **any spec-compliant FDP**, not FDPneo specifically. It
  consumes only the public, documented surface: LDP containers and records
  (content-negotiated GET), the `/sparql` endpoint, the search API, and the
  root/`fdp_info`-relevant records. FDPneo-private endpoints are forbidden;
  where the public surface is insufficient, the bridge records the gap (see
  Decision 4) rather than working around it.
- v1 configuration: **one target FDP per bridge instance** (`FDP_BASE_URL`).
  Multi-source and Index-level operation are the vision document's increment
  C and will get their own ADR.

### 2. Stack: Python + the official MCP SDK

Python 3.12+, official `mcp` Python SDK (streamable-HTTP and stdio
transports), `httpx` for the FDP client layer, `rdflib` for RDF handling, and
the server's exact toolchain (`uv`, `ruff`, `pyright`, `pytest`). Rationale:
the bridge is I/O-bound glue where language performance is immaterial, and one
team maintains both codebases — sharing conventions, RDF expertise, and audit
tooling serves the maintenance-and-evolution goal better than adopting the
(more mature) TypeScript SDK and a second toolchain. Revisit only if the
Python SDK stops tracking the protocol.

### 3. Credentials: anonymous by default, per-session pass-through otherwise

- No credentials → the bridge calls the FDP anonymously; the FDP's own
  authorization (publication state, ODRL, named-graph projection) decides
  visibility. The bridge adds **no authorization logic of its own** — the FDP
  remains the sole PDP/PEP.
- An MCP client may supply an FDP API key (ADR-0011) as its bearer token; the
  bridge forwards it verbatim on that session's FDP requests. The bridge
  **never stores, caches, or logs credentials**; memory-only, session-scoped.
- Egress allowlist: the bridge speaks only to the configured `FDP_BASE_URL`.
  No other outbound requests, ever (mirrors the server's SSRF discipline).

### 4. The API-honesty rule and the gap report

`mcp/docs/fdp-api-gaps.md` is a first-class, continuously maintained
deliverable: every capability the tool surface needs that the public FDP
surface cannot provide cleanly (missing endpoint, missing serialization,
underspecified contract, reference-implementation divergence) is recorded
there with the concrete tool it blocks. The server repo triages this report
into its roadmap (server `TASKS.md` Phase 19). The bridge never resolves a gap
by using private surface.

### 5. Read-only tool surface v1, specified implementation-agnostically

Tools: `fdp_info`, `list_records`, `get_record`, `get_schema`, `search`,
`sparql_query` (read-only query forms), `get_access_conditions`,
`list_data_services`. The binding contract — names, parameters, JSON result
shapes, error envelope, pagination, JSON-LD framing — lives in
`mcp/docs/mcp-tool-surface.md` and is written so any party could implement it
against any FDP; `fdp-mcp` is the reference implementation. No mutation tools,
no external dereferencing, no network operations in v1.

### 6. Distribution: ship in the standard deploy profile

The server's compose/deploy profiles (architecture §12) gain an `fdp-mcp`
service wired to the FDP container, so a default FDPneo deployment still comes
up agent-ready — preserving as much of the "out of the box" adoption story as
a sidecar allows. Operators of non-FDPneo FDPs run the same image pointed at
their instance: one `docker run` with `FDP_BASE_URL`.

## Alternatives considered

**Embedded in the monolith (rejected).** A new `agent/` bounded context
calling internal service interfaces, mounted at `/api/mcp`. Pros: single
deployable, on by default; security parity for free (tools as PEPs over the
same `identity`/`policy` machinery, no credential forwarding); in-process
access to capabilities not yet on the public surface; one release cycle and a
cheap parity test suite. Cons, which decided it: agent-readiness limited to
FDPneo while the live network is mostly the Java reference implementation;
private-interface access hides public-API gaps instead of surfacing them; MCP
protocol churn lands in the core server; a long-lived-connection ingress and
its vulnerabilities share the server's process and blast radius. The embedded
option's genuine strengths (default-on, security parity) are partially
recovered by Decisions 6 and 3 respectively.

**MCP client configuration in `fdp-client` (rejected).** Demonstrates
consumption only inside our own UI; the goal is to meet users in the agents
they already use.

**Wait for standardisation (rejected).** No other candidate protocol has
comparable client reach. Churn risk is contained by the sidecar's independent
release cycle and the implementation-agnostic contract (Decision 5).

**TypeScript implementation (rejected for now).** Most mature MCP SDK, but a
second toolchain for the same small team; see Decision 2.

## Consequences

**Easier:**

- The demonstrator can run against the **existing** FDP network — including
  the ERDERA VP's FDPs — with zero server-side changes and no migration ask.
- Continuous spec-compliance audit of FDPneo itself via the gap report; the
  public API improves for *all* consumers, not just this one.
- MCP churn, resource isolation, and security review are contained in a small
  adapter codebase; the metadata server's attack surface is unchanged.
- The tool-surface spec plus a reference bridge is a much stronger
  standardisation offer to the FDP community than "install our server."

**Harder / accepted costs:**

- Two deployables. Mitigated by Decision 6, but operators who hand-roll
  deployments must add the sidecar deliberately.
- Credential pass-through must be implemented and reviewed carefully (no
  storage, no logging, session scoping) — a class of bug the embedded option
  did not have.
- Capability ceiling = public API: some tools will be blocked on server
  roadmap work (the gap report makes this visible and schedulable, but it is
  still latency).
- Cross-cutting concerns (rate limits, result shaping, error mapping) exist
  in two codebases with two versions to keep coherent; the shared contract
  document is the coherence mechanism.
