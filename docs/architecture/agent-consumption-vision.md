# FDP Server — Agent Consumption Vision (metadata as a consumable surface)

**Status:** Discussion draft (advisory; ADR-0018 carries the binding decisions)
**Date:** 2026-07-03
**Scope:** How the FDP ecosystem exposes its metadata to *consumers* — in
particular AI agents — so that the eleven years of provisioning work produce
visible downstream value. The first increment is `fdp-mcp`, a standalone MCP
sidecar (the `mcp/` repo beside `server/` and `client/`).

> This document motivates and frames the work; the placement and design
> decisions for the first increment are recorded in
> [ADR-0018](../adr/0018-agent-consumption-mcp-server.md), and the
> implementation tasks in `mcp/TASKS.md` (bridge) and server `TASKS.md`
> Phase 19 (server-side support). Where this document and the ADR disagree,
> the ADR wins.

---

## 1. Framing: the consumption gap

The FDP ecosystem has solved metadata **provisioning**: organisations deploy
FDPs, publish DCAT-typed, SHACL-validated metadata, and register with
community indexes. What is missing is **consumption** — applications that read
that metadata and deliver value a user can feel. Without consumers, provisioning
quality has no feedback loop: metadata is produced to satisfy policy, not to
satisfy a user, and it shows.

One production system demonstrates what consumption looks like when it works:
the **ERDERA Virtual Platform**. Its human-facing VP Portal queries the VP
Index (an FDP Index instance) as its source of truth for *which API endpoints
support which Beacon 2 query types*, and configures itself dynamically from
that metadata. This is metadata as a **control plane** — machine-actionability
driving service discovery and query orchestration, not just browsing. The
pattern is generic; today it lives partly in onboarding documentation and in
the portal's code rather than in a reusable machine-readable artifact.

The strategic observation for 2026: **the natural first-class consumer of FAIR
metadata is an AI agent.** Structured, semantically typed, SHACL-constrained
metadata is exactly the grounding LLM-based agents need to answer questions
about data holdings without hallucinating. Users already converse with agents
daily; a consumption layer that meets them there rides an existing behaviour
instead of trying to create a new one (the historical failure mode of research
portals).

## 2. The goal: a demonstrator that steers adoption

The purpose of this work is not only the feature itself but a **demonstrator**
that shows other communities what an FDP network makes possible, and makes the
capability trivially cheap to adopt. Design constraints that follow:

1. **Value at n=1.** The first increment must be valuable on a *single* FDP,
   before any network effect: "point the bridge at your FDP and converse with
   your metadata." This defeats the cold-start problem that kills federated
   demos.
2. **Works on the network that exists.** The bridge consumes only the public,
   specified FDP surface, so it works against *any* spec-compliant FDP — the
   large installed base of Java reference implementations included — with no
   migration ask. One `docker run` with `FDP_BASE_URL`. FDPneo deployments
   additionally get it wired into the standard deploy profile, so they come
   up agent-ready by default (ADR-0018 §6).
3. **Forkable and documented.** A short "connect Claude / any MCP client to
   your FDP" guide, a scripted three-minute demo, and a published tool-surface
   spec. People copy what they can run.
4. **Generic, provably.** The demo must include at least one non-rare-disease
   FDP answering the same class of questions with zero code changes, so the
   result reads as "platform," not "ERDERA feature."
5. **API honesty as a by-product.** Because the bridge may use only public
   surface, building it continuously audits FDP spec compliance; every gap it
   hits is recorded (`mcp/docs/fdp-api-gaps.md`) and triaged into the server
   roadmap. Consumption work improves provisioning — the feedback loop this
   whole programme is missing, closed at the API level.

## 3. Architecture: three layers

```
┌────────────────────────────────────────────────────────────┐
│ 3. Consumers: agent chat (flagship demo), VP-Portal-class  │
│    apps, notebooks, any MCP client                         │
├────────────────────────────────────────────────────────────┤
│ 2. Consumption interface                                   │
│    a. fdp-mcp sidecar: MCP bridge over the public surface  │
│       of ONE FDP (THIS increment, ADR-0018; mcp/ repo)     │
│    b. Index-level MCP: network discovery + query dispatch  │
│       (later; depends on Phase 8 FDP Index protocol)       │
│    connective tissue: the capability profile (§5)          │
├────────────────────────────────────────────────────────────┤
│ 1. Provisioning: FDP servers + FDP Index                   │
│    (exists; any spec-compliant FDP — FDPneo, Java          │
│    reference impl, the ERDERA VP network)                  │
└────────────────────────────────────────────────────────────┘
```

**Why MCP.** The Model Context Protocol is the current de-facto standard for
exposing tools/resources to LLM agents, with first-party client support in the
major assistant products and official SDKs. Classical applications are not
excluded: the bridge's tools are thin adapters over the same public HTTP
surface any application uses, so nothing becomes MCP-only.

**Why a sidecar.** Decided in ADR-0018: reach (works against the whole
existing network, not just FDPneo), API honesty (§2.5), protocol-churn
isolation, and process isolation. The costs — a second deployable, credential
pass-through, capability ceiling at the public API — are accepted and
mitigated there.

**Two levels, phased.** The FDP-level bridge (2a) answers questions about
*one* repository and ships first. The Index-level server (2b) adds
network-scoped operations — discover sources, resolve which endpoints support
which query types, dispatch and aggregate (e.g. Beacon 2) queries — and is
where the ERDERA control-plane pattern generalises. It is deliberately later:
it depends on the FDP Index protocol work (Phase 8, not built) and on the
capability profile (§5).

## 4. FDP-level MCP: tool surface v1 (read-only)

Grounded in what the public FDP surface already exposes. The bridge holds **no
authorization logic**: it calls the FDP anonymously or with the MCP client's
own forwarded API key, and the FDP's publication-state, ODRL, and named-graph
machinery decides visibility exactly as for any HTTP client (ADR-0018 §3).
Names indicative; the binding spec is `mcp/docs/mcp-tool-surface.md`.

| Tool | Backing public surface | Purpose for an agent |
|---|---|---|
| `fdp_info` | root FDP record | What is this repository? Who runs it? What does it contain, in what profile? |
| `list_records(kind?, parent?, page?)` | LDP containers | Enumerate catalogs, datasets, distributions, and other resource kinds (kinds are runtime-defined per FDP — enumerate, do not hard-code). |
| `get_record(iri)` | content-negotiated LDP GET | Full metadata of one record, with signposting links where the FDP emits them. |
| `get_schema(kind)` | published SHACL shapes (`constrainedBy` / shape endpoints) | The shape for a kind — tells the agent what fields *mean* and what to expect. |
| `search(query, filters?)` | FDP search API | Keyword/faceted discovery inside this FDP. |
| `sparql_query(query)` | public `/sparql` endpoint | Structured queries; read-only forms only — the FDP's own access control is the enforcement layer. |
| `get_access_conditions(iri)` | ODRL policies / licenses in record metadata (`dct:rights`, `dct:license`) | "Can I use this data, under what terms, and whom do I contact?" — the question that matters most to a human asking through the agent. |
| `list_data_services()` | DCAT `DataService` records | What can be *queried or invoked* here, with which endpoint types — the seed of the capability profile (§5). |

Deliberately **not** in v1: any write/mutation tool, dereferencing of URLs
other than the configured FDP (strict egress allowlist), and any Index/network
operation. Where a tool cannot be implemented against a given FDP because the
public surface lacks something (e.g. no search endpoint on the Java reference
implementation), the tool degrades explicitly and the gap goes in the report —
that signal is a deliverable, not a failure.

## 5. The capability profile (the reusable ERDERA artifact)

The knowledge "endpoint E supports query type Q" — today in ERDERA onboarding
documentation and the portal's code — should be promoted into a small,
machine-readable profile: DCAT `DataService` + `dcat:endpointDescription` /
`dcat:servesDataset`, plus a controlled vocabulary for conformance claims
(e.g. `dct:conformsTo <beacon2-individuals-spec>`). SHACL-validatable like any
other FDP schema, publishable as a schema package, and consumed identically by
the VP-Portal-class apps and by the Index-level MCP dispatcher.

This is arguably the single most reusable artifact of the whole programme —
it is what turns "an app that works on ERDERA" into "a pattern any community
can instantiate." It is scoped as a specification deliverable alongside, not
inside, the v1 bridge increment (v1 includes only the `list_data_services`
tool that surfaces whatever `DataService` metadata exists).

## 6. The demo script (golden path, ~3 minutes)

1. **n=1:** point an off-the-shelf MCP client (e.g. Claude) at an `fdp-mcp`
   instance bridging a live FDP. Ask: *"What datasets does this repository
   hold about X? Which are openly licensed? How would I get access to the
   restricted ones?"* The agent answers from `search`, `get_record`,
   `get_access_conditions` — with IRIs as citations, no hallucinated holdings.
2. **Structure:** ask a question that needs the schema (*"which registries
   record age at diagnosis?"*) — the agent reads the SHACL shape via
   `get_schema`, then queries via `sparql_query`.
3. **Reach:** re-point the same bridge image at a *Java reference
   implementation* FDP from the existing network. Same conversation, zero
   code changes — the moment that proves this is an ecosystem capability, not
   an FDPneo feature.
4. **Network (act two, later):** the same conversation against the Index-level
   MCP over the ERDERA VP Index — discovery across sources, Beacon 2 dispatch
   to the endpoints whose metadata claims support, aggregated counts, drafted
   access request.

## 7. Adoption plan

- **One-command bring-up** against any FDP: publish the `fdp-mcp` image with a
  single required setting (`FDP_BASE_URL`) and a one-page "connect your agent"
  doc (client config snippets for the common MCP clients).
- **Default-wired in FDPneo deploy profiles** (ADR-0018 §6), so FDPneo
  installs remain agent-ready out of the box.
- **Publish the tool-surface spec** (`mcp/docs/mcp-tool-surface.md`) as an
  implementation-agnostic contract; `fdp-mcp` is its reference implementation.
  The spec — not the codebase — is what should eventually go to the FDP
  specifications community for standardisation.
- **Flagship demos on the existing network** — including ERDERA FDPs — are
  possible from increment A, because the bridge needs no server-side changes.
- **The gap report as community currency:** publish `fdp-api-gaps.md`
  findings; divergences between implementations discovered by the bridge are
  exactly the interoperability issues the FDP spec community should be
  resolving.
- Later: a lightweight **"agent-ready FDP" conformance check** in
  `docs/conformance/`, mirroring the existing conformance-note practice.

## 8. Phasing

| Increment | Contents | Depends on |
|---|---|---|
| **A (now)** | `fdp-mcp` sidecar (mcp/ repo), read-only tool surface v1, tool-surface spec, gap report, docs + demo script; server-side: deploy-profile wiring + gap triage (server Phase 19) | ADR-0018 |
| **B** | Capability profile specification + schema package; `DataService` authoring guidance | A (feedback from real agent use) |
| **C** | Index-level MCP: source discovery, capability resolution, query dispatch/aggregation | Phase 8 (FDP Index protocol), B, its own ADR |
| **D** | Access-request workflow tools (draft/submit requests against ODRL Offers) | A; policy/lifecycle design work |

## 9. Open questions

1. **MCP authorization alignment.** v1 accepts anonymous sessions and
   forwards client-supplied FDP API keys (ADR-0018 §3). The MCP spec's
   OAuth-based authorization flow should be revisited once client support
   stabilises — track, don't block.
2. **Cross-implementation surface variance.** How much of the tool surface is
   implementable against the Java reference implementation as deployed today
   (search? shapes discovery?) — the first gap-report entries will answer
   this, and drive what the tool spec marks *required* vs *optional*.
3. **Tools vs. MCP resources/prompts.** v1 is tools-only for uniform client
   support; revisit resources (records as MCP resources) when client
   behaviour converges.
4. **Result shaping.** Agents consume JSON more reliably than Turtle; the
   bridge returns framed JSON-LD by default with raw serialisations on
   request. Validate this choice against real agent transcripts in the demo.
5. **Where the Index-level MCP lives** (an `fdp-mcp` mode, a second sidecar
   beside the Index, or part of the Index deployment) — decide with an ADR
   when increment C starts.
6. **Session identity beyond API keys** — pass-through works for FDPs that
   accept bearer keys; anonymous-only for those that don't. Is that
   acceptable for the demo networks?
