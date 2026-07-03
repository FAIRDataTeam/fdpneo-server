# ADR-0016: Faithful backup/restore and instance migration

**Status:** Proposed
**Date:** 2026-07-03

## Context

FAIR principle **F1** requires identifiers that are *persistent* — they must
survive not only a host move (solved by ADR-0014's identifier-base/serving
split) but also instance replacement: backing up a deployment and restoring it
elsewhere, or migrating from another FDP implementation, must preserve every
record's canonical IRI and its provenance.

ADR-0014 explicitly deferred this. Its dual identifier model *enables* a
faithful restore (a client brings a within-base identifier via `PUT` path or
`POST` `Slug`), but replaying records through the LDP API today is **lossy and
unsafe**:

- **Provenance is falsified.** `MetaWriter` unconditionally stamps
  `dct:created = now` on create, `dct:modified = now`, creator = the writing
  subject, and publication state = `DRAFT`. A restored record loses its real
  history and disappears from public view.
- **Audit history is not replayable.** Per-record `<record>/audit` graphs and
  the Postgres `record_audit` rows accumulate only through live operations.
- **Replay is not idempotent or safe.** `POST` with a `Slug` matching an
  existing record silently *replaces* it — no existence check, no `If-Match`,
  authorization checked only against the container, and a `RecordCreated`
  event emitted for what is in fact an overwrite.
- **The canonical-subject invariant has an escape hatch.**
  `reconcile_identifiers` bails out ("store as authored") when the body has
  zero or multiple typed subjects, so a graph whose subject is a foreign IRI
  or a blank node can be stored under a canonical graph key it never mentions
  — silently violating the F1 guarantee ADR-0014 promises.

Meanwhile the reference FDP (FAIRDataPoint) mints host-bound IRIs and offers
no bring-your-own-identifier at all, so deployments migrating from it need a
one-time adoption path that re-roots their records under a persistent base.

Storage is favourable: per ADR-0007 every record lives in its own named graph
with `<record>/meta` and `<record>/audit` siblings, all keyed under the
identifier base. A quad-level dump of the store is therefore inherently
faithful — the problem is purely one of tooling and of the write paths that
would otherwise re-stamp provenance.

## Decision

### 1. Close the write-path holes (LDP contract changes)

- **Ambiguous primary subject → `400`.** `reconcile_identifiers` loses its
  "store as authored" fallback. A write body must either address the record
  as `<>` / its canonical IRI, or contain exactly one typed IRI primary
  subject (which is rebound; foreign ones recorded as structured alternative
  identifiers per [ADR-0017](0017-alternative-identifiers-and-signposting.md)).
  Zero typed subjects, several typed subjects, or a blank-node-only body is
  rejected with a `400` explaining the requirement. The canonical-subject
  invariant becomes unconditional.
- **`POST` `Slug` collision → `409 Conflict`.** `POST` never overwrites: if
  the slug-derived member IRI already exists, respond `409` and let the
  client pick another slug or use `PUT` (with its `If-Match` discipline)
  deliberately. Predictable for migration scripts; no silent data loss.

### 2. `fdp dump` — storage-level export

A CLI command (admin-operated, like `fdp pid`) that produces a versioned
archive:

- **`records.nq`** — every named graph in the store (record + meta + audit
  siblings), serialized as N-Quads. The named-graph keying *is* the record
  identity, so nothing is interpreted or transformed on the way out.
- **`manifest.json`** — dump-format version, `identifier_base`, application
  version, graph count, per-file checksums, timestamp.
- **`audit.jsonl`** *(optional)* — the Postgres `record_audit` rows, so the
  operational audit trail can travel with the metadata.

The dump reads through the `TripleStoreAdapter` directly (same posture as
`fdp pid rebase`); the LDP layer is not involved.

### 3. `fdp restore` — faithful, verbatim import

The inverse CLI command:

- Loads the quads verbatim through the adapter — **no** `MetaWriter`
  stamping, so `dct:created`, `dct:modified`, creator, version, publication
  state, and the audit graphs survive exactly. Faithful restore is the
  default and only mode; "re-publication" (fresh timestamps) is what the
  ordinary LDP API is for.
- **Precondition:** the target's `identifier_base` equals the manifest's.
  On mismatch the command refuses and points at `--rebase` (below). This is
  deliberate: identifier persistence means a restore should *never* need new
  IRIs — a moved deployment re-points its redirector (ADR-0014), it does not
  rename records.
- Refuses a non-empty store unless `--merge` (skip existing graphs) or
  `--overwrite` is passed explicitly.
- Afterwards: reindex search (the `metadata_search` projection is derived
  state) and, when `audit.jsonl` is present, insert the audit rows.
- `--dry-run` reports what would be written, mirroring `rebase`.

### 4. `fdp import` — adoption migration from a foreign source

For sources whose records are *not* under the target's identifier base:

- **From an FDPneo dump under a different base** — restore composed with the
  existing rebase rewrite (`pid/rebase.py`'s term rewriting applied
  in-flight): every IRI under the old base is re-rooted to
  `identifier_base`, cross-record links included. One-time, like `rebase`.
- **From a reference-FDP instance** — walk the source's LDP tree (or consume
  its export), map each host-bound IRI to `identifier_base` + the same path,
  and carry provenance across: the source's `dct:issued`/`dct:modified`
  become the meta graph's `dct:created`/`dct:modified` instead of import
  time. When the old host will keep resolving, the old IRI is preserved on
  each record as a structured alternative identifier (`adms:identifier` +
  `dct:identifier`, per [ADR-0017](0017-alternative-identifiers-and-signposting.md));
  `owl:sameAs` is added only on explicit operator assertion. When the old
  host will not keep resolving, the mapping is recorded once in the import
  report instead of polluting every record with a dead cross-reference.

### 5. Privileged provenance writes stay off the HTTP surface

Restore and import need to write meta graphs with *supplied* timestamps,
creator, and state. That capability lives as an internal repository path
(used only by the CLI commands above), **not** as an LDP header or query
flag. The HTTP contract stays exactly ADR-0014's: canonical subject always,
server-stamped provenance always. This keeps the F1 resolution guarantee
un-gameable by API clients while still making the operator workflows
possible.

### 6. Document rebase's Postgres boundary

`fdp pid rebase` (and rebase-on-import) rewrites the triple store only.
`metadata_search` must be reindexed afterwards (derived state);
`record_audit` rows intentionally keep the IRIs that were current when the
events happened (they are history, not live references). Both facts move
from folklore into the command's docs and completion output.

## Alternatives considered

- **Restore by replaying the LDP API** — rejected: lossy provenance (even
  with fixes), non-atomic containment maintenance re-runs, state reset to
  `DRAFT`, and orders of magnitude slower. The API is for clients; the
  store is the backup boundary.
- **An HTTP "import mode" (header/flag) honouring supplied provenance** —
  rejected for now: it reopens the falsified-provenance hole to any client
  the flag leaks to, and every consumer of meta graphs would need to trust
  two stamping regimes. Can be revisited as an admin-only endpoint if
  operator demand materialises.
- **Honouring foreign IRIs as record subjects on import** — rejected,
  reaffirming ADR-0014: the FDP cannot make a foreign IRI dereference to
  itself, so it must not be the canonical subject; structured alternative
  identifiers preserve the linkage, and FAIR Signposting's `cite-as` keeps
  the foreign PID citable ([ADR-0017](0017-alternative-identifiers-and-signposting.md)).
- **Keeping the lenient "store as authored" fallback** — rejected: silent
  acceptance of a non-canonical subject is worse than a clear `400`; the
  strictness is what makes dump/restore round-trips verifiable.

## Consequences

- **F1 across instance replacement.** Identifiers and provenance survive
  backup/restore and implementation migration; the identifier base never
  changes after adoption.
- **Two visible LDP contract changes.** Ambiguous bodies now `400`; `Slug`
  collisions now `409`. Both are strictly-safer behaviours; clients that
  relied on silent overwrite must switch to `PUT` + `If-Match`.
- **A new operator surface.** `fdp dump` / `restore` / `import` join
  `fdp pid`; the dump format is versioned from day one so future changes
  stay readable.
- **Reference-FDP migration becomes a supported, documented path** rather
  than ad-hoc scripting.
- **Search reindex becomes part of the restore/rebase runbook.**

## References

- FAIR F1; LDP 1.0 §5.2.3 (slug handling); W3ID.
- Builds on [ADR-0007](0007-one-graph-per-record.md) (graph-per-record makes
  quad dumps faithful), [ADR-0010](0010-metadata-publication-state.md)
  (state lives in the meta graph), and
  [ADR-0014](0014-persistent-identifiers.md) (base/serving split, dual
  identifier model, rebase). Refined by
  [ADR-0017](0017-alternative-identifiers-and-signposting.md) (foreign IRIs
  recorded as structured alternative identifiers, not `owl:sameAs`).
- Code touched: `metadata/identifiers.py`, `metadata/ldp/router.py`
  (`_mint_member_iri` / `http_post`), `metadata/repository.py` +
  `metadata/meta.py` (privileged write path), new `metadata/backup/`
  package, `metadata/pid/rebase.py` (shared rewrite), CLI wiring.
