# ADR-0024: Opt-in write tools in the MCP sidecar (`fdp-mcp`)

**Status:** Accepted
**Date:** 2026-08-26
**Supersedes:** the "no mutation tools" clause of [ADR-0018](0018-agent-consumption-mcp-server.md) §5 only. Every other ADR-0018 decision stands.
**See also:** ADR-0010 (publication state), ADR-0011 (API keys), ADR-0014 (write-body subject rebinding), ADR-0022 (in-band affordance advertisement), `mcp/docs/mcp-tool-surface.md` §5.9–§5.12, `mcp/docs/fdp-api-gaps.md` G-08…G-10.

## Context

ADR-0018 placed agent consumption in a standalone MCP sidecar and fixed its
first tool surface as strictly read-only: eight tools, "no mutation tools, no
external dereferencing, no network operations in v1". That was the right first
increment — it made the sidecar safe to deploy next to any FDP with no new
attack surface — and it shipped (`fdp-mcp` v0.1.0–v0.2.0).

The next thing agents are asked to do is *author* metadata: draft a dataset
record from a paper or a data-management plan, fix a stale title, publish a
record once a steward has checked it. FDPneo already exposes exactly the public
surface this needs — full LDP write (`POST` to a container, `PUT`/`PATCH`/
`DELETE` under `If-Match`, SHACL-on-write with a structured 422 report) and a
publication-state transition endpoint (ADR-0010) — all behind the same
bearer-credential and ODRL policy decisions the read path uses.

Two forces pull against simply adding tools:

1. **Blast radius.** A sidecar that can write is a different deployment
   posture from one that cannot. Many operators will want the read-only
   sidecar forever.
2. **Portability.** The FDP specification standardises *reading* far better
   than writing. The Java reference implementation has no `PATCH` and a
   different state-transition endpoint; FDPneo's own advertised
   `hasStateTransition` link currently points at a URL that does not resolve.
   A write surface that quietly hard-codes FDPneo would break ADR-0018's
   "targets any spec-compliant FDP" promise and its API-honesty rule.

## Decision

### 1. Four explicit write tools, as an opt-in extension of the contract

`create_record` (LDP `POST`), `update_record` (`PUT` full replace, or
`application/sparql-update` `PATCH`), `delete_record` (`DELETE`), and
`set_record_state` (`DRAFT`/`PUBLISHED`/`ARCHIVED`). They are specified
implementation-agnostically in the tool-surface contract (1.1.0-draft,
§5.9–§5.12) as a **write extension**: a conforming bridge may omit them
entirely and, when it offers them, must do so only behind an explicit operator
opt-in. `sparql_query` remains read-only forever; writes are never smuggled
through the query tool.

### 2. Gating: `FDP_MCP_ENABLE_WRITE`, default off; anonymous writes fail fast

With the flag off (the default, and the default in every deploy profile) the
write tools are **not registered** — a read-only deployment is
indistinguishable from v0.2.0. With it on, a write call on an anonymous
session is answered `authentication_required` **before any network call**.
This is deliberately framed as a fail-fast, not an authorization decision: a
write cannot succeed without a credential for the FDP to evaluate, so the
bridge refuses to spend the round trip. Every authenticated call is forwarded
verbatim and the FDP alone allows or denies it (401/403 relayed as
`upstream_unauthorized`). ADR-0018 §3 — no authorization logic in the bridge,
no credential storage/logging — is unchanged.

### 3. `If-Match` is the caller's responsibility

`update_record` and `delete_record` require `if_match`; `get_record` now
returns the record's `etag`. The bridge is stateless and never manufactures a
precondition it did not observe, so lost updates are impossible by
construction and the "read before you write" discipline is visible to the
agent. A stale validator is `precondition_failed` (412/428).

### 4. Portable core, optional extras, honest degradation

`POST`/`PUT`/`DELETE` are the portable core every conformant FDP exposes and
fail with errors. SPARQL-update `PATCH` and state transitions are **optional
capabilities** (`ldp_patch`, `state_transition`) that return the contract's
`unsupported` result when the target lacks them. `set_record_state` follows
the record's advertised `hasStateTransition` affordance first (ADR-0022) and
only then tries the FDPneo path convention — a fallback isolated in one
function and tied to gap entries G-08/G-09 so it can be deleted once the link
resolves. The Java RI's `/meta/state` is not attempted.

### 5. Client-side validation is syntactic; the FDP validates semantics

Before any request the bridge parses the RDF body (must be valid, non-empty,
exactly one primary subject — the same rule FDPneo enforces server-side under
ADR-0014) and the SPARQL update (only `INSERT DATA`/`DELETE DATA`/`DELETE
WHERE`/`DELETE…INSERT…WHERE`; `LOAD`, `SERVICE`, graph-management verbs, and
graphs outside the FDP origin are refused — `LOAD` in particular would make
the FDP fetch an arbitrary URL on the bridge's behalf, defeating strict
egress by proxy). The body is then sent **verbatim**; the bridge never
reserialises, never runs SHACL, and offers no dry-run. SHACL failures are the
FDP's 422 relayed as `validation_failed` with the FDP's `details` intact so
the agent can repair the body.

### 6. Gap report entries

Designing the extension surfaced three API-honesty gaps, recorded in
`mcp/docs/fdp-api-gaps.md` for Phase 19 triage: **G-08** the advertised
`hasStateTransition` link does not resolve (FDPneo bug); **G-09** the
state-transition endpoint and request shape are not standardised across
implementations; **G-10** FDPneo's OpenAPI omits `PATCH`, `/state`, and any
`securitySchemes`.

## Alternatives considered

- **Keep the sidecar read-only forever** (ADR-0018 §5 as written). Rejected:
  authoring is the next real agent use case and the public surface already
  supports it; the opt-in flag preserves the read-only deployment exactly.
- **Always-on write tools, relying purely on FDP authorization.** Rejected:
  correct in principle (the FDP decides anyway) but it changes the safety
  posture of every existing deployment and removes the operator's ability to
  run a provably read-only sidecar.
- **Auto-fetch `If-Match` inside `update_record`/`delete_record`.** Rejected:
  hides the read-before-write step from the agent, invites blind overwrites
  between the fetch and the write, and makes the bridge hold state it did not
  observe from the caller.
- **A `dry_run` / bridge-side SHACL validation.** Rejected: the FDP has no
  validate-only endpoint, and reproducing its SHACL (shape resolution,
  closure, profile) in the bridge is business logic the thin-adapter rule
  forbids. `get_schema` plus the relayed 422 report covers the need.
- **Target FDPneo's write API directly.** Rejected: breaks "any spec-compliant
  FDP" and the API-honesty rule; the portable-core/optional-extras split keeps
  the contract honest and records the divergences as gaps.

## Consequences

- **Easier:** agents can draft, correct, publish, and retire metadata through
  the same sidecar, with SHACL feedback in a machine-readable form; operators
  choose the posture per deployment with one flag.
- **Harder / to watch:** the write path is new attack surface and must stay
  behind the flag, the anonymous fail-fast, the client-side `LOAD`/`SERVICE`
  refusal, and the unchanged egress guard; the FDPneo state-URL fallback is a
  product convention living in the bridge until G-08 is fixed; the Java RI
  degrades to `unsupported` for `patch` mode and state transitions.
- **Server follow-ups (Phase 19 triage):** fix the `hasStateTransition` link
  target (G-08); add `PATCH`, `/state`, and `securitySchemes` to the generated
  OpenAPI (G-10); propose the transition request shape for the FDP spec (G-09).
- The contract moves to 1.1.0-draft (MINOR: additive). `mcp/CLAUDE.md`,
  `TASKS.md`, and the README reflect "read-only by default".
