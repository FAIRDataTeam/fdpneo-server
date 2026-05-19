# ADR-0006: ODRL profile — Permissions and Prohibitions only

**Status:** Accepted
**Date:** 2026-05-18

## Context

The FDP enforces access control on metadata records using W3C ODRL policies. The ODRL Information Model 2.2 is large: Permission, Prohibition, and Duty rules; Sets, Offers, Agreements, Requests, Tickets, Assertions; rich constraint vocabulary including temporal, spatial, party, purpose, count, payment, and many others.

Implementing the full surface in v1 would produce a system nobody can confidently author policies for because the editor would expose features the evaluator might or might not enforce, and no real policies need most of the surface anyway.

The FDP also needs an audit story. ODRL distinguishes Offers (proposed access conditions, not yet accepted) from Agreements (concluded grants between specific parties), and this distinction is the right place to hang the audit trail.

## Decision

The FDP defines a profile — a documented subset of ODRL — that policies must conform to. Policies using features outside the profile are rejected at write time.

**Supported policy types:** `odrl:Offer` and `odrl:Agreement`.

**Supported rules:** `odrl:Permission` and `odrl:Prohibition`. Duties are not supported in v1.

**Action vocabulary:**

| Action | Semantics |
|---|---|
| `odrl:read` | View a record or schema |
| `odrl:modify` | Update a record or schema |
| `odrl:delete` | Remove a record or schema |
| `odrl:distribute` | Download or query a distribution |

**Supported constraints:** party identity, role membership, organization/group membership, time windows.

**Excluded constraints:** purpose, spatial, industry, payment, count, percentage.

**Lifecycle:**
- A record's `dct:rights` references an Offer (versioned, immutable).
- On `PERMIT`, the PDP materializes an Agreement that records assigner, assignee, the specific Offer version, action, and timestamp.
- Agreements are stored in the record's audit graph for audit.

## Alternatives considered

**Implement full ODRL.** Rejected. The implementation cost is large, the evaluator becomes hard to reason about, the editor becomes a kitchen sink, and no community has shown they need the full surface.

**Define a custom access-control vocabulary, not ODRL.** Considered. The community is already aligned around ODRL for the use case; using something else would impose a learning cost and undermine interoperability with other FAIR tooling.

**Support Duty rules declaratively (record but not enforce).** Considered. The temptation is real — stewards want to express obligations like "must cite". Rejected for v1 because expressing them without enforcement misleads stewards into thinking the system handles them. If we add Duty support in a future version, we add it with enforcement.

**Use Set policies rather than Offers.** Rejected. Offers carry the lifecycle semantics we need for audit (Agreement materialization references the Offer version in force). Sets are a generic policy container without that affordance.

## Consequences

**Easier:**
- The evaluator is small and testable. Adding a new constraint operator is a contained change.
- The visual editor surfaces only constructs the server will accept. Stewards do not author policies that surprise them at write time.
- Audit semantics are well-defined: Agreement → Offer version, immutable.

**Harder:**
- Communities that want features outside the profile (purpose constraints, count constraints) must wait for a profile extension or use a different system.
- Profile evolution requires care: every relaxation must be evaluated for evaluation cost, editor impact, and audit semantics.

**Evolution path:**
- v1.x can extend the profile incrementally, with each addition going through its own design review.
- v2 may consider Duty support when a workflow exists to act on obligations.
