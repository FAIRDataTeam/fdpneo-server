# FDP v1.x Roadmap (proposal)

Baseline: **0.1.0** (server `v0.1.0` @ green CI, client `v0.1.0` @ green CI, 2026-06-10).
This roadmap collects the work deferred from 0.1 — sourced from architecture
[§15 Open Questions](architecture/README.md#15-open-questions) and the `deferred` /
`v1.x` markers in the code — and proposes a sequence. Sizes: **S** ≤ ½–1 day,
**M** ~2–4 days, **L** > 1 week. Priorities and grouping are a proposal; adjust.

---

## Milestone v1.1 — "serve what you describe" (highest value)

### 1. Access-controlled data delivery — **L** — *headline feature*
Today the data provider authorizes every request against a synthetic **anonymous**
context and serves only open-access distributions
([`data/router.py`](../src/fdp/data/router.py), [`data/__init__.py`](../src/fdp/data/__init__.py)).
For a clinical / HIPAA deployment this is the biggest functional gap: a restricted
distribution can be *described* but never *delivered* through the FDP.

- **Scope:** authorize the data routes against the *real* caller's `RequestContext`
  (the PDP and ODRL Offers already exist); add the authn challenge / 401–403 flows;
  decide streaming-vs-redirect behaviour for restricted `downloadURL`s (a 302 to an
  upstream leaks the URL — restricted data likely needs stream mode + the N-02 SSRF
  guard, already built).
- **Depends on:** existing PDP, identity middleware on `/data`.
- **ADR:** extend [ADR-0010](adr/) (publication state) or a new ADR for data-access
  authorization; revisit the anonymous-read invariant.
- **Security:** re-run the data-provider threat model; pairs with the N-02/N-04 work.

### 2. Metrics per-record ownership scoping — **M**
The dashboard can't yet scope metrics to a steward's own records
([`metrics/api.py`](../src/fdp/metrics/api.py), [`metadata/dashboard.py`](../src/fdp/metadata/dashboard.py))
— it depends on a record-ownership model that isn't built. Unblocks steward-scoped
analytics. Keep the anonymization boundary (ADR-0002) intact — ownership scoping is a
*query filter*, never identifying data in the pipeline.

---

## Milestone v1.2 — community & operability

### 3. IdP role → FDP role mapping — **M–L** — §15.7
Map IdP groups/claims to FDP-internal roles via deployment config. The seam already
exists ([`identity/principal.py`](../src/fdp/identity/principal.py)). Intersects with
AAI config in non-trivial ways → **needs its own design pass + ADR** before coding.

### 4. Schema draft/release lifecycle + version-history browsing — **M** — `schemas.py:29`
Draft→release states and browsing a schema's version history are deferred. Builds on
the existing meta-metadata/versioning model.

### 5. `fdp profile export` — **M** — `cli.py:163`
Currently a no-op stub. Emit a round-trippable profile bundle (inverse of
`profile apply`). Natural pairing with §15.8 (OCI artifact / signed bundles) as a
follow-on.

---

## Milestone v1.3 — protocol & UX polish

### 6. LD-PATCH support (`application/ldpatch`) — **S–M** — §15.5
Add `application/ldpatch` PATCH alongside the existing `application/sparql-update`
PATCH for JSON-LD-native clients. No architectural impact.

### 7. Resource-definition guided flow — **S–M** — §15.9
Optionally *suggest* a resource definition when a SHACL shape is published (guided
one-step flow over today's explicit two-step model). Builds on
[ADR-0009](adr/0009-runtime-resource-definitions.md).

### 8. Smaller deferrals — **S each**
- Remote-vocabulary source for autocomplete & labels
  ([`autocomplete.py`](../src/fdp/metadata/autocomplete.py), [`labels.py`](../src/fdp/metadata/labels.py)).
- Offer-version "consolidate" action to tame noisy edit history (§15.4).
- Profile distribution as OCI artifact / signed bundles (§15.8).

---

## Open decisions to resolve (gate some of the above)

| § | Decision | Affects |
|---|----------|---------|
| 15.2 | Default triple-store deployment guidance | ops / docs |
| 15.3 | SPARQL update-restriction ergonomics (revisit after community feedback) | access module |
| 15.6 | Policy-decision audit-log default (currently on, rotating-hash subject keys) | policy / GDPR |

---

## Cross-cutting carryover (not features, but should land in v1.x)

- **Client N-03** — `safeHref` scheme-allowlist for record-derived `:href`/`:src`
  (DOM-XSS). *Verify whether the client's 2026-06-10 "Security fixes" already covered
  this before scheduling.*
- **Go-live ops (N-05)** — rotate dev credentials, set `FDP_LOG_SUBJECT_SALT`, TLS
  termination + HSTS at the edge; per [`docs/security/deployment-hardening.md`](security/deployment-hardening.md).
- **CI: integration/conformance suites** — CI runs only `unit` + `contract`; the
  testcontainers-backed `integration` (GraphDB/Fuseki/Postgres) and `conformance`
  (FDP/LDP) suites exist but aren't wired into a CI job. Add a gated job.
- **Actions/tooling hygiene** — already bumped `checkout@v5` / `setup-uv@v5`;
  keep `setup-node@v5` on the client and watch for the Sept 2026 Node-20 removal.

---

## Suggested sequence

1. **v1.1**: #1 access-controlled data delivery (start with the ADR), then #2 metrics scoping.
2. **v1.2**: #3 IdP role mapping (design pass first), #4 schema lifecycle, #5 profile export.
3. **v1.3**: #6 LD-PATCH, #7 guided resource definitions, #8 smaller items.
4. Resolve §15.2/15.3/15.6 as their dependent epics come up.
5. Land the cross-cutting carryover opportunistically alongside the above.
