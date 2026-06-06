# ADR-0012: First-class ODRL policy and license documents

**Status:** Accepted
**Date:** 2026-06-05

## Context

The FDP authorizes access through ODRL (ADR-0006): an `odrl:Offer` of permissions/prohibitions, attached to a record via `dct:rights`, evaluated by the PDP. But ODRL is **second-class** today:

- Offers are **profile-seeded only** — written once at bootstrap by the applier at their *intrinsic* IRI (the `odrl:Offer` subject in the bundle TTL). There is no runtime CRUD.
- They carry **no descriptive metadata** (title, description, who authored it, what it's for), **no lifecycle** (draft/published/archived), and **no versioning** as a managed surface.
- They are **not discoverable or searchable** — the search indexer deliberately skips `odrl:Offer`-typed records, and there is no catalog.
- The same gap applies to **licenses**: `dct:license` references are bare IRIs (often external), with no FDP-managed, reusable license documents.

By contrast, the *other* RDF-config surface — **SHACL schemas** — was made first-class: a `/schemas` admin API (Phase 10.1) over schemas stored as ordinary RDF records at stable deployment IRIs, with versioning and validation, and the resource-definition layer (ADR-0009) that manages them as runtime-mutable RDF. The client is gaining a **visual ODRL editor** (client Phase 5) and needs the same backing for policies that the SHACL editor has for schemas: a place to **author, store, validate, version, publish, search, and reuse** access conditions — and for an FDP to act as a **reference source of access conditions and licenses** that other FDPs can discover and reference.

The reuse case is concrete: one curated "embargo-2-years" offer or "CC BY 4.0" license should be authored once and referenced by many records (DRY within an FDP) and, ideally, by records in *other* FDPs (a dereferenceable, citable IRI).

## Decision

**1. Two managed subsystems: `/policies` (enforced) and `/licenses` (descriptive).** They are kept separate because they play different roles and have different validation and consumption:

- **Policies** — `odrl:Offer` documents validated against the **FDP ODRL profile** (the existing `policy/parser.py`), **enforced** by the PDP. A record opts into one via `dct:rights`.
- **Licenses** — license documents (an `odrl:Set`/`odrl:Policy` license expression, or a `dct:LicenseDocument`) validated against a **license SHACL shape**, **descriptive** only. A record references one via `dct:license`; the PEP never evaluates it.

Folding both into one `kind`-tagged surface was considered and rejected: "enforced vs descriptive" is a real boundary (different validators, different consumers, different blast radius if malformed), and keeping them apart keeps the PEP's input set unambiguous (only `/policies` documents are ever evaluated).

**2. Stored as RDF records at stable deployment IRIs, like schemas.** Managed documents live one-graph-per-record (ADR-0007) under reserved deployment-relative namespaces `{base}/policies/{id}` and `{base}/licenses/{id}` (mirroring `{base}/schemas/{id}`). The stable IRI is the unit of identity: edits bump `owl:versionInfo` at the *same* IRI so references stay valid, and the IRI is **dereferenceable** (content-negotiated RDF) for cross-FDP reference. Profile `offers:` are seeded as managed policies at bootstrap; everything is runtime-mutable thereafter. These are **public reference documents** (anonymous-readable, like schemas) — *not* internal graphs — but their `…/meta` and `…/audit` siblings remain internal (ADR-0009's `is_internal_graph_uri`).

**3. Each document carries its own metadata; no separate registry.** A policy/license record holds the ODRL/license graph **plus** descriptive metadata (`dct:title`, `dct:description`, keywords, `odrl:profile` for policies) and the usual meta-metadata sibling (creator/created/modified/version + **publication state**). This is the "metadata of the policy" analog: as SHACL schemas carry their own metadata and are listed by a flat `/schemas` catalog, policies/licenses carry theirs and are listed by flat `/policies` / `/licenses` catalogs. We do **not** introduce a resource-definition-style typed registry — resource definitions exist to drive *URL routing and typed containers*, which policies and licenses do not need.

**4. Lifecycle reuses publication state (ADR-0010).** `DRAFT → PUBLISHED → ARCHIVED` via the existing `POST /{record}/state`:

- **Draft** — authored/edited in the ODRL editor; not offered for assignment.
- **Published** — assignable (`dct:rights`/`dct:license`), dereferenceable, discoverable, searchable.
- **Archived** — **retained and still resolvable/enforced for records that already reference it** (archiving a policy must never silently break the records that depend on it), but not offered for *new* assignment. This is the one place a policy's "archived" differs from a content record's: the document stays authoritative for its existing dependents.

Versioning is `owl:versionInfo` bumped at the stable IRI (as for schemas), so a `dct:rights` reference resolves across edits.

**5. Validation mirrors `/schemas`.** `PUT /policies/{id}` parses the body and validates it against the FDP ODRL profile, rejecting out-of-profile constructs with a structured violation envelope; `PUT /licenses/{id}` validates against the license shape. `POST /{…}/{id}/validate` is a dry-run. The editor surfaces these inline and is not the source of truth on validity (the server is).

**6. Referencing and enforcement are unchanged at the resolver.** A record sets `dct:rights <…/policies/{id}>`; the existing `GraphBackedOfferResolver` already fetches and parses an offer graph by IRI, so it works on managed-policy IRIs with no change. The **system-default offer** becomes a managed policy IRI. On a policy write, the PDP authorization cache is invalidated for the resources whose effective offer is that policy (a synchronous hook, like the schema validator-cache invalidation).

**7. Searchable and dereferenceable — the "reference source" capability.** Managed policies and licenses are indexed in the Phase-7 search index as `policy` / `license` content types (title/description/keywords/kind), so `POST /search {types:["policy"]}` and per-kind facets work; the indexer's current blanket skip of `odrl:Offer` is narrowed to skip only *un-managed*/seed offers, not documents under `…/policies/`. `GET /policies` and `GET /licenses` are the discovery catalogs. Published documents are dereferenceable by IRI. When the FDP Index protocol ships (Phase 8), the harvest can surface these catalogs so other FDPs discover reusable conditions.

**8. Cross-FDP reuse: discover and reference now; resolve-and-enforce remote later.** v1 delivers the *publishing* side — stable dereferenceable IRIs + discovery catalogs + search — so another FDP can find a condition and reference its IRI. Actively **dereferencing and enforcing a *remote* FDP's policy** at decision time (outbound fetch on the authz path, with trust, caching, and availability concerns) is deliberately deferred and will be opt-in + allow-listed when added — the same posture as remote schema sync (10.2) and remote-vocabulary labels. Local references are the common path and are fully supported.

**9. Delete is reference-guarded.** `DELETE /policies/{id}` / `DELETE /licenses/{id}` are admin-only and refused with `409` if any record still references the document via `dct:rights` / `dct:license` — mirroring schema-delete-refused-if-referenced-by-a-resource-definition.

## Alternatives considered

**One `kind`-tagged subsystem.** Simpler surface, but blurs the enforced/descriptive boundary and the PEP's input set; rejected in favour of two explicit subsystems.

**A typed policy registry (resource-definition analog).** Rejected: resource definitions earn their keep by driving URL routing and typed containers; policies/licenses are managed *documents* (the schema model), and a flat catalog plus per-document metadata covers organising and templating without a second config surface to keep coherent.

**Full remote policy resolution + enforcement now.** Rejected for v1: it puts an allow-listed outbound fetch on the authorization hot path and couples decisions to a remote FDP's availability and trust posture. Deferred behind the publish/discover capability, which is where the immediate value is.

**Keep offers second-class (status quo).** Rejected: it blocks the ODRL editor, prevents reuse and discovery, and leaves access conditions without the lifecycle, metadata, and versioning every other managed surface already has.

## Consequences

**Easier:**

- The client ODRL editor gets a real backend symmetric with the SHACL editor: author → validate-against-profile → publish → version → search → reuse.
- Curated access conditions and licenses are authored once and reused (DRY) within an FDP and citable across FDPs; the FDP can act as a reference registry of conditions.
- Policies/licenses inherit lifecycle, meta-metadata, ETags, content negotiation, and search for free by being ordinary records (ADR-0007/0009/0010, Phase 6.2/7/12).
- The PDP and offer resolver are unchanged for the common (local-IRI) case.

**Harder / to build:**

- Two new admin surfaces + two reserved storage namespaces; the search indexer must include managed policies/licenses while still excluding their internal siblings and un-managed seed offers.
- A synchronous PDP-cache invalidation on policy write (new hook, same class as the existing schema/RD invalidations).
- The "archived but still enforced for existing dependents" nuance is policy-specific and must be tested.
- Cross-FDP **trust and availability** are deferred but the IRI/dereference design must not foreclose them.

**Required of operators:** nothing new at the infrastructure level. Bundled-profile offers become seeded managed policies; a small default license set ships with the profile.

## Related decisions

- [ADR-0006](0006-odrl-profile-permission-prohibition.md) — the FDP ODRL profile these policies validate against and the PDP enforces.
- [ADR-0007](0007-one-graph-per-record.md) — one graph per record, applied to policy/license documents.
- [ADR-0009](0009-runtime-resource-definitions.md) — the "runtime-mutable RDF config records, public but isolated by namespace" model this mirrors.
- [ADR-0010](0010-metadata-publication-state.md) — the publication-state lifecycle reused for draft/published/archived policies.
- Phase 7 (search index — adds `policy`/`license` content types), Phase 8 (Index harvest surfaces the catalogs), Phase 10.1 (`/schemas` — the parallel admin surface this follows).
