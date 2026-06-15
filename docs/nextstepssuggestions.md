# FDP Server — Next Steps Suggestions

> Advisory roadmap as of v0.3.0 (2026-06-15). Grounded in the current source tree,
> the 14 ADRs, the architecture goals/non-goals (§2.2), and the open-questions list (§15).
> Not a commitment — a prioritized view of where the highest-value work is.

## Where the server stands

The v1 goal set is largely delivered: full LDP + PATCH, DCAT v3, SHACL-driven schemas,
ODRL access control with materialized Agreements, the access-controlled SPARQL endpoint,
anonymous metrics, deployment profiles, persistent identifiers (ADR-0014), plus work that
went beyond the original architecture doc — full-text search, autocomplete, a
user-management facade, and API keys. This is a coherent, near-1.0 system.

> Note: `CLAUDE.md` is now behind the code (it predates search, PID, profiles,
> user-management, and API keys). Worth refreshing alongside this work.

The question is therefore: what closes the gap to a defensible 1.0, and what sets up the
v1.x / v2 direction.

## 1. Functionality — close the highest-value non-goals

The non-goals list (arch §2.2) is effectively the backlog. In priority order:

- **Access-controlled data distribution.** Today `data/` only serves anonymous-read
  distributions. The PDP and PEP pattern already exist — extending the data provider to
  call `policy.authorize` for restricted downloads is the most user-visible gap and is
  architecturally low-risk. **Do this first.**
- **FDP-to-FDP federation / Index registration.** The biggest strategic direction
  (deliberately deferred, not dropped). Even ahead of a full Index service, a server-side
  "register with an Index" affordance + a stable metadata harvest endpoint is the unlock.
  Multi-increment epic — deserves its own ADR.
- **Handle/DOI minting** (open Q under ADR-0014). PID infrastructure exists for W3ID;
  minting integrations are the natural follow-on for real FAIR F1 in production.
- **ODRL Duty enforcement** and **property-level access control** — real but lower urgency.
  Keep deferred unless a deployment asks.

Also live and cheap:
- **LD-PATCH** (open Q #5, "no architectural impact").
- **IdP role-to-FDP-role mapping** (open Q #7) — the most common operator friction point;
  worth a design pass for v1.x.

## 2. Code organization — split the metadata module

Clearest structural issue. LOC by module:

```
metadata: 12189   ← ~60% of all module code
metrics:   2256
identity:  1849
shared:    1730   ← getting heavy for a "kernel"
policy:    1215
access:     830
storage:    626   ← thin for what it's supposed to own
```

`metadata/` has become a god-module: it owns LDP, records, SHACL **and** search, PID
minting, profile management, autocomplete, licenses, dashboard, instances. This violates
the bounded-context spirit in CLAUDE.md. Candidate extractions:

- **`profiles/`** as its own context (deployment/bootstrap concern, not record CRUD) — or
  at minimum stop it importing record internals.
- **`search/`** as its own context (a read-side projection; should consume events, not
  reach into metadata internals). **Best first extraction** — proves the event-bus seam.
- Reconsider **`pid/`** — spans identifiers + GitHub/W3ID I/O; the external-I/O parts may
  belong nearer `shared` or a dedicated module.

Other smells:
- `shared/` (1730 LOC) is drifting from "genuinely cross-cutting" — `ssrf`,
  `security_headers`, `sparql_safety`, `limits` are arguably HTTP-edge or access concerns.
- `storage/` (626 LOC) looks thin for "all RDF I/O goes through the adapter" — confirm no
  module bypasses it now that metadata is so large.

**Do not big-bang this.** Extract one context, prove the seam, repeat.

## 3. Security

Primitives are in place (SSRF guard, security headers, SPARQL parse-not-interpolate,
API keys, named-graph projection for query-layer access control). Harden next:

- **Authorization cache correctness** — the materialized auth index (§9.4) is the riskiest
  correctness surface. Need explicit tests for invalidation on policy/record change, and
  the information-leakage rules (§9.5) tested adversarially.
- **Security review of the SPARQL rewriter** specifically — the boundary where a bug =
  data disclosure. `access/` has unit tests for parser/rewriter/router but no adversarial
  or integration suite against a live store.
- **Policy-decision audit log** (open Q #6) — confirm the rotating-hash subject keys
  preserve the metrics anonymization invariant (a structural promise).
- **Rate limiting / abuse** on SPARQL and search endpoints — confirm `shared/limits.py`
  is wired to the expensive paths.

## 4. Testing & ops — coverage is lopsided

The pyramid is inverted relative to risk. The 12k-LOC metadata module and the
access-control layer — the two highest-risk areas — have thin integration coverage.

- **Conformance**: only `test_ldp.py` exists. No FDP-specs conformance suite, despite
  spec conformance being goal #1. Should be a first-class test target.
- **Contract**: one OpenAPI shape test. A separate client repo depends on the contract —
  invest here to prevent silent breakage.
- **Integration for `access/` and `policy/`** against a real triple store (testcontainers)
  — currently policy has 1 integration test, access has none.
- **Backup/restore** (open Q under ADR-0014) — now buildable on stable identifiers; this
  is table-stakes for production operators.

## Recommended sequencing

1. **Access-controlled data distribution** — high value, low risk, reuses the PDP.
2. **FDP-specs conformance suite + access-layer integration tests** — de-risks everything
   else; you're claiming spec conformance as goal #1.
3. **Extract `search/` out of `metadata/`** — first bounded-context split.
4. **Open the federation / Index epic** with a fresh ADR — the defining v2 direction.
