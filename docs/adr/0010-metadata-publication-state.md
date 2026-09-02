# ADR-0010: Metadata publication state as a visibility gate over the ODRL decision

**Status:** Accepted
**Date:** 2026-06-02

## Context

The reference implementation (FAIRDataPoint, Java/Spring) tracks a publication *state* per metadata record — `DRAFT | PUBLISHED | ARCHIVED` — via `MetadataStateService`. The client renders a publish button and gates visibility per state: a curator works on a record in `DRAFT`, makes it `PUBLISHED` when ready, and `ARCHIVED` when it is retired. Anonymous and other unprivileged readers must see only `PUBLISHED` records; the owner and admins see all of theirs regardless of state. Without this, every record is world-visible the instant it is created, which is wrong for a curation workflow.

This FDP already has a fully-formed authorization story that the state model has to fit into, not fight:

- **ODRL is the policy language (ADR-0006).** Every read/write decision is an ODRL evaluation: the PDP resolves the Offer in force for a resource (its own `dct:rights`, an inherited one up the `dct:isPartOf` chain, or the system-default) and evaluates it for the subject + action.
- **Decisions are cached in a materialized index (architecture §9.4).** `authz_index` is keyed `(subject_key, action, graph_uri) → permit/deny`. The SPARQL endpoint's dataset projection is a *bulk* read of that index: `authorized_graphs(subject, read)` returns the permitted graph set, which the rewriter turns into `named-graph-uri` parameters (ADR-0004).
- **A single structural filter already layers over that cache.** `is_internal_graph_uri` (ADR-0009) strips `…/meta`, `…/audit`, and resource-definition graphs from `authorized_graphs` *after* the cache lookup — internal graphs never reach a public projection even if a decision row says `permit`.

The open question recorded in the task (12.2) was whether to *"thread `state` into the `authorize()` call."* Taken literally, that means making the cached ODRL decision depend on state. The problem: state is orthogonal to policy and changes on a different cadence (a publish/archive is a frequent curation act, not a policy edit). Folding it into the decision means either widening the cache key with state, or invalidating every cached row for a record on every transition — churn, and a second source of truth for "is this visible" smeared across the policy cache.

## Decision

**1. Publication state lives in the record's meta graph, not in Postgres.** A record's state is a single triple `<record> fdp:metadataState "DRAFT|PUBLISHED|ARCHIVED"` in its sibling `<record>/meta` graph. It travels with the record — captured by backups, `fdp profile export`, and the one-graph-per-record model (ADR-0007) — exactly like `dct:created` and `owl:versionInfo`. It is **not** a Postgres table; that would split "what is this record's state" away from the record and require an invalidation protocol between the two stores. The meta-metadata SHACL shape requires the field (`sh:in (DRAFT PUBLISHED ARCHIVED)`, `minCount 1`), so every record always carries exactly one state.

**2. State is enforced as a structural visibility gate layered *over* the ODRL decision, not folded *into* it.** This is the load-bearing choice. The ODRL cache stays exactly as it is — policy only, no state. State is a second, independent check applied at the read PEPs, in the same architectural slot as `is_internal_graph_uri`:

- **Point reads** (LDP `GET`/`HEAD`, `/expanded`, `/page`, the data provider): after the ODRL decision permits `read`, the gate consults the record's state. `PUBLISHED` → visible. `DRAFT`/`ARCHIVED` → visible only if the subject is an admin or the ODRL policy permits them `modify` on that record (i.e. they are the owner). A non-visible record returns **404**, not 403, so a draft's existence does not leak.
- **The SPARQL projection** (`visible_read_graphs(ctx)`): the candidate sets come from the store — the `published` records, plus (for authenticated callers) the unpublished records — and any candidate without a cached ODRL decision is evaluated and cached on the spot (`authorize_many`). Visible = `read_permitted(published) ∪ (read ∩ modify)_permitted(unpublished)`; admins get every non-internal record graph, matching the REST rule. For anonymous, the modify pass is skipped, so the projection collapses to read-permitted-and-published — anonymous SPARQL sees only published graphs. *(Amended in 0.16: the projection originally read only the already-cached decisions — `authorized_graphs` — which made a subject's SPARQL view depend on what they had happened to fetch over REST; an authenticated user could see fewer published records than anonymous. The projection is now deterministic over the store-derived candidate set.)*

Because state is never cached in `authz_index`, a transition takes effect **immediately** with **zero cache invalidation**: the next read re-reads state from the meta graph (point reads) or re-queries the published set (SPARQL). The `published` set is read live from the triple store per SPARQL request in v1; caching it is a pure-performance follow-up, not a correctness one.

**3. The transition surface is `POST /{record}/state` with a small, explicit state machine.** Body `{"to": "PUBLISHED"}`. Allowed transitions and who may perform them:

| From → To | Authorized |
|---|---|
| `DRAFT → PUBLISHED` | owner (ODRL `modify`) or admin |
| `PUBLISHED → DRAFT` (unpublish) | owner or admin |
| `PUBLISHED → ARCHIVED` | owner or admin |
| `ARCHIVED → DRAFT` | admin only |

Any other transition, and a no-op same-state request, is `409`. "Owner" is not a new concept: it is "the ODRL policy in force permits this subject `modify` on this record", reusing the PDP. The transition writes only the meta graph (it is not a record-content edit, so it does not bump the content ETag or re-validate the record body), emits a `RecordStateChanged` event, and is audited (`record_audit.operation = "state_change"`).

**4. New records default to `DRAFT`; bootstrap/profile-seeded records are `PUBLISHED`.** A record created through the LDP layer starts life as `DRAFT` so a curator can work on it before exposing it. Records written by the profile applier — the root Repository container and any seed records — are seeded `PUBLISHED`, because the root must be anonymously readable for the FDP to be usable at all. State is preserved across ordinary content edits: a `PUT`/`PATCH` that changes a record's triples does **not** reset its state — only the transition API changes it (mirroring how `dct:created` and the creator are preserved across modifications).

*Amendment (2026-09, v0.15):* a creating `PUT`/`POST` MAY carry
`Prefer: publication-state=PUBLISHED` to mint the record already visible.
This is an authorized shortcut through the same state machine, not a bypass:
creation already required a PDP `modify` permit on the IRI, and
`DRAFT → PUBLISHED` is owner-or-admin — the creator *is* the owner — so the
create-permit subsumes the publish-permit. The default stays `DRAFT`; the
preference is ignored on updates; `ARCHIVED` is rejected. This differs from
the `write_imported` "CLI-only, never an HTTP flag" stance deliberately:
that rule protects *provenance* (server-stamped, un-gameable by clients),
whereas publication state is client-managed by design — the transition API
already lets the same caller reach the same state one request later. Every
201 now also carries an `FDP-Metadata-State` header (and
`Preference-Applied` when the preference was honored), so API clients see
the birth state instead of discovering `DRAFT` when the record 404s for
everyone else. Motivated by bulk API population of a live deployment, where
each 201 was followed by an invisible record and no signal why.

## Alternatives considered

**Thread state into `authorize()` / the `authz_index` cache (the literal task wording).** Rejected. It couples an orthogonal, frequently-changing attribute to the policy cache. Either the cache key grows a state column (and every lookup must know the current state first — so you read state anyway), or every transition must invalidate all of a record's cached decisions across all subjects. Both add machinery to get a result the layered gate gives for free, and both create a second place where "is this visible" is decided. The layered approach also keeps the ODRL evaluator a pure function of Offer + context + action, which ADR-0006 relies on.

**Store state in a Postgres `record_state` table.** Rejected for the same reason ADR-0009 rejected storing resource definitions in Postgres: state is metadata *about a record*, not operational bookkeeping like metrics or job state. A table splits the record's truth across two stores, needs a migration and an invalidation protocol, and is lost by a triple-store-only backup or `profile export`. The meta graph already exists for exactly this class of field.

**Return 403 (not 404) for a draft an anonymous caller cannot see.** Rejected. 403 confirms the record exists, which leaks the existence of unpublished work. 404 is consistent with the information-leakage rule already applied in the SPARQL rewriter (§9.5) and the `/expanded` ancestor walk (drop, don't reveal).

**Make state a record-content triple (in the record graph) rather than meta.** Rejected. State is server-managed lifecycle metadata, peer to `dct:modified`/`owl:versionInfo`, and putting it in the record graph would (a) subject it to the record's own SHACL shape and content ETag, making a publish look like a content edit, and (b) let a client set it directly via `PUT`/`PATCH`, bypassing the transition state machine and its authorization. Meta-graph placement keeps the transition the only way to change it.

## Consequences

**Easier:**

- A transition is immediate and cheap: write one meta triple, emit one event. No authz-cache invalidation, no policy recomputation.
- The ODRL cache and evaluator are untouched — state cannot introduce a policy-cache-coherence bug because it is not in the policy cache.
- State is captured by backups and `profile export` for free, and validated by the existing meta-metadata SHACL pipeline.
- Visibility has one extra structural filter sitting in the same slot as the internal-graph filter, so the two compose and are tested the same way.

**Harder:**

- Correctness now depends on the state gate being applied at *every* read PEP — LDP `GET`/`HEAD`, `/expanded`, `/page`, the data provider, and the SPARQL projection. This is mitigated by funnelling all of them through one `lifecycle` helper (one place to get the rule right, one place to test it), exactly as ADR-0009 did for internal-graph exclusion.
- The SPARQL projection gains one live triple-store query per read for the `published` set. Bounded by deployment size and a clear caching follow-up; correctness does not depend on caching it.
- A point read of a `DRAFT`/`ARCHIVED` record by a non-owner costs one extra `authorize(…, modify, …)` call (cache-backed) to distinguish owner from stranger. `PUBLISHED` reads — the common case — pay nothing extra.

**Required of operators:**

- Nothing new at the infrastructure level. Because this lands during development with no production content, the meta-shape change is adopted by a wipe + re-bootstrap (`POST /admin/reset` or `fdp profile apply --force`) rather than a data migration.

## Related decisions

- [ADR-0006](0006-odrl-profile-permission-prohibition.md) — ODRL is the policy language; this ADR keeps the evaluator a pure function and state out of the policy cache.
- [ADR-0004](0004-sparql-access-via-named-graph-projection.md) — the named-graph projection the state filter intersects into.
- [ADR-0007](0007-one-graph-per-record.md) — one graph per record; state rides in the record's meta sibling.
- [ADR-0009](0009-runtime-resource-definitions.md) — the `is_internal_graph_uri` structural filter whose pattern the state gate follows.
