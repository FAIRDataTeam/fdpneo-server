# ADR-0019: Self-describing record–schema binding and schema versioning

**Status:** Proposed
**Date:** 2026-07-03
**Amends:** [ADR-0007](0007-one-graph-per-record.md) (record graph gains `dct:conformsTo`),
[ADR-0009](0009-runtime-resource-definitions.md) (resource definitions narrow to a type/profile index).
**Prerequisite for:** [ADR-0016](0016-backup-restore-migration.md) §4 (import-with-validation).

## Context

FDPneo binds a record to its SHACL shape **implicitly**: a record's type / URL
prefix → its ResourceDefinition (ADR-0009) → `ldp:constrainedBy` → the schema
graph. The record's own graph carries no validation-bearing binding. Validation
is resolved at write time by walking the RD registry.

That is not self-describing. A consumer reading a record — from the API, or from
a faithful quad dump (ADR-0016), or mid-import from another FDP — cannot tell what
the record claims to conform to, or validate it, without *our* internal registry.
FAIR asks the opposite: a record should declare its conformance so any agent can
resolve and check it. The reference FDP already does this, via
`dct:conformsTo` → a `prof:Profile` (W3C Profiles Vocabulary) → the validating
shape.

Two further gaps compound it:

- **No version fidelity.** Schemas are mutable in place — the same storage IRI,
  with `owl:versionInfo` bumped on each `PUT`. Bindings are never version-pinned,
  so there is no record of *which* shape version validated a given record, and a
  later shape edit silently changes what "conformant" means for existing records.
- **Import cannot validate.** ADR-0016's backup/restore is already faithful for
  FDPneo↔FDPneo (schemas and RDs are named-graph records and travel in the quad
  dump). But adoption import from a foreign/reference FDP needs the incoming
  records to be self-describing to validate them on the way in. This ADR is the
  prerequisite that decision deferred to.

The maintainer's intent (this ADR's driver): **records should be self-describing
at rest**, carrying their own conformance as a first-class FAIR property.

## Decision

### 1. `dct:conformsTo` is the primary, server-maintained validation binding

Every record carries `dct:conformsTo <profile>` in its **own graph**. This is the
authoritative binding used for validation — at write time, on read (Signposting
`describedby`/`type` already surface it), and on import.

The value is a **`prof:Profile`**, not a bare shape:

```
<record>  dct:conformsTo  <profile> .
<profile> a prof:Profile ;
          prof:hasResource [ prof:hasRole role:validation ;
                             prof:hasArtifact <shape> ] .   # a SHACL shape
```

v1 wraps a **single** validation resource (the SHACL shape). Modelling it as a
profile from day one lets it grow additional resources later
(`role:vocabulary`, `role:guidance` human docs, a JSON-LD context) — the rich FAIR
profile — without changing the binding.

The server **stamps and maintains** `conformsTo` on write, exactly as the LDP
layer already maintains containment forward-links: resolve the record's type → RD
→ profile, then inject/refresh the triple in the record graph. A client may assert
*additional* `dct:conformsTo`, but the validation-bearing one is server-owned so
it cannot drift.

### 2. ResourceDefinition becomes a type→profile index, not the validator

RDs (ADR-0009) still own url prefixes, container configuration, the OpenAPI
surface, and the **default profile for a type** (`ldp:constrainedBy` now
references a profile). But *validation* resolves through the record's own
`conformsTo`, not by walking the RD at read/import time.

**Constraint (v1):** a record's profile must equal its type's RD default profile;
the server enforces this on write. Per-record profile choice (a record conforming
to any published profile) is **deferred** — it opens type-safety questions best
handled after the core binding lands.

### 3. Stable binding in the record, exact version in the meta graph

The two goals — the binding must stay *current*, and it must be *faithfully
reproducible* — are resolved by putting them in different places:

- **Record graph:** `dct:conformsTo` → the **stable** profile IRI
  (`{base}/fdp-api/profiles/<slug>`), which always resolves to the current
  version. The public binding never goes stale.
- **Meta graph (`<record>/meta`):** the **exact** profile/shape version validated
  at write time (`fdp-o:validatedAgainst <…/profiles/<slug>/<version>>`).
  Provenance, faithful, and it travels in the dump — so a restore/import can
  reproduce the original validation.

### 4. Immutable, versioned profile & schema identity

Publishing a profile/schema **snapshots an immutable version IRI**
(`{base}/fdp-api/profiles/<slug>/<version>`, and likewise for the underlying
schema) and moves a `dcat:hasCurrentVersion` pointer on the stable IRI. Prior
versions are retained — they are what `validatedAgainst` resolves to and what
makes a dump reproducible. This replaces today's mutate-in-place schema edit with
snapshot-then-repoint. Version identity uses DCAT 3 versioning
(`dcat:version` / `dcat:hasCurrentVersion` / `dcat:previousVersion`).

### 5. Profiles are named-graph records (unchanged storage posture)

Profiles live under the reserved namespace like schemas do today, so they are
captured by the ADR-0016 quad dump, are dereferenceable, and list/edit as
first-class records. No new storage tier.

### 6. Migration

A one-time, idempotent command (reads/writes through the adapter, like
`fdp pid rebase`):

- wrap each existing schema in a `prof:Profile` (single `role:validation`
  resource) and snapshot it as version 1;
- for each existing record, resolve type → RD → profile and stamp
  `dct:conformsTo` + write `validatedAgainst` (the version 1 IRI) into its meta
  graph.

Existing records are indistinguishable from records written after this ADR once
migrated; no data is discarded.

## Alternatives considered

- **Keep the implicit RD binding** — rejected: records are not self-describing;
  foreign import cannot validate without reconstructing our RDs; not FAIR at rest.
- **Bare `dct:conformsTo → shape` (skip PROF)** — simpler, but loses the profile
  bundling FAIR consumers and the FDP specs expect, and diverges from the
  reference FDP. PROF is cheap to model now and awkward to retrofit later.
- **Pin the record's `conformsTo` to the version** — maximal faithfulness, but the
  *public* binding goes stale on every schema edit and each record needs a
  re-point migration to "upgrade." Stable-in-record + version-in-meta (§3) gets
  both properties without that churn.
- **Decouple profile from type now (per-record profile choice)** — deferred (§2):
  more flexible but reopens type-safety and container semantics; not needed for
  the self-describing or import goals.

## Consequences

- **Self-describing, FAIR at rest.** A record — or a dump of one — declares and
  can be validated against its profile without the originating FDP's registry.
- **Validation path changes.** Shape resolution goes through the record's
  `conformsTo` → profile → `role:validation` artifact; the server still enforces
  the type's default profile on write (§2).
- **Explicit, immutable schema versioning.** More storage (retained versions) and
  a changed schema-edit workflow (snapshot + move `hasCurrentVersion`).
- **ADR-0009 narrows; ADR-0007 record graph gains `conformsTo`** (still one graph
  per record). ADR-0017 Signposting already emits `type`/`describedby`; a future
  increment can add a profile link relation.
- **Unlocks ADR-0016 §4**: import brings profiles first, then validates each
  incoming record against its own `conformsTo` (as a report, not a hard reject —
  see ADR-0016 §3). Export is self-contained.
- **Client coordination** (fdp-client, separate repo): render `conformsTo` /
  profile in record detail; a validation view can resolve the profile's shape.

## References

- W3C *Profiles Vocabulary* (PROF) and *Profiles Ontology*; the profile-role
  vocabulary (`role:validation`, …); DCAT 3 versioning; DCMI `dct:conformsTo`.
- Builds on / amends [ADR-0007](0007-one-graph-per-record.md),
  [ADR-0009](0009-runtime-resource-definitions.md); relates to
  [ADR-0014](0014-persistent-identifiers.md) (identity),
  [ADR-0016](0016-backup-restore-migration.md) (import),
  [ADR-0017](0017-alternative-identifiers-and-signposting.md) (Signposting).
- Code to touch: the metadata write path (`conformsTo` stamping) and its meta
  writer (`validatedAgainst`); shape resolution / the container registry; the
  schema service (versioned identity) plus a new Profile resource type; the
  resource SHACL shape; the migration command; docs (`04-request-lifecycle`,
  a conformance note).
