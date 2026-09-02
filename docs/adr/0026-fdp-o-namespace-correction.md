# ADR-0026: Correct the FDP Ontology namespace and migrate stored vocabulary

**Status:** Accepted
**Date:** 2026-09-02

## Context

Since its first release this server minted every FDP vocabulary term under
`https://w3id.org/fdp/o#`. That IRI was a typo carried from the initial
namespace registry: the published FDP Ontology (FDP-O) lives at
**`https://w3id.org/fdp/fdp-o#`**, and `https://w3id.org/fdp/o#` was never
registered on w3id.org — it returns 404. The wrong namespace propagated into:

- the root record's `rdf:type` (`fdp-o:FAIRDataPoint`) and container
  membership (`ldp:hasMemberRelation fdp-o:servesMetadata` + the forward
  membership triples),
- every record's `/meta` sibling graph (`metadataState`, `validatedAgainst`,
  the `CreateOperation`/`ModifyOperation` prov activity types),
- the resource-definition machinery records (ADR-0009) and the stored schema
  graphs + their immutable version snapshots (`sh:targetClass fdp-o:…`),
- the signposting affordance link relations (ADR-0022),
- four graphs *named* under it (the server-owned SHACL shapes), and
- the client's and MCP sidecar's semantic matching.

The consequence that surfaced it: **the FDP Index at home.fairdatapoint.org
classified a live FDPneo deployment as INVALID.** The index's validator
retrieves the root as Turtle and accepts a repository only when some subject
is typed `r3d:Repository` or `https://w3id.org/fdp/fdp-o#MetadataService` —
exact IRI match, no RDFS inference. Our root offered neither: wrong namespace,
and only the `FAIRDataPoint` subclass even in the right one.

## Decision

**1. All FDP vocabulary moves to the published FDP Ontology namespace
`https://w3id.org/fdp/fdp-o#`** — the seven terms FDP-O already defines that
we use (`FAIRDataPoint`, `MetadataService`, `Metadata`, `servesMetadata`,
`metadataIdentifier`, `metadataIssued`, `metadataModified`) *and* FDPneo's
own lifecycle/machinery terms (`metadataState`, `allowedStateTransition`,
`validatedAgainst`, `ResourceDefinition` and the child-link predicates,
`ManagedLicense`, `CreateOperation`/`ModifyOperation`, the six `has*` link
relations). The FAIR Data Team governs FDP-O, and these are FDP-generic
concepts: rather than parking them in an FDPneo-private namespace, they are
**proposed as additions to FDP-O** — the drafted proposal lives at
[`docs/proposals/fdp-o-additions.md`](../proposals/fdp-o-additions.md). Until
the ontology release lands they are extension terms in the FDP-O namespace,
minted by the implementation that proposes them.

**2. The four server-owned SHACL shape IRIs are artifacts, not vocabulary,
and move to `urn:fdp-shape:*`** (`meta-metadata`, `license-document`,
`resource-definition`, `child-link`) — matching the existing
`urn:fdp-schema:` convention for modular shape references. A shape document
stored under the ontology IRI would dereference (via w3id) to the OWL file,
which does not contain it; a URN says clearly "server-owned stored artifact".

**3. The root record asserts BOTH `fdp-o:FAIRDataPoint` and
`fdp-o:MetadataService`** (`root_type_iris` in the profile applier, mirrored
by `migrate-modular`'s root re-type). FAIRDataPoint ⊑ MetadataService in
FDP-O, but harvesters and index validators match literally; the redundant
supertype triple is what makes an FDPneo deployment register as valid at an
FDP Index out of the box.

**4. Existing stores are migrated automatically at startup**
(`metadata/vocab_migration.py`, run from the lifespan before the authz warmup
and the store conformance check; also exposed as `fdp vocab migrate
[--dry-run]`). The migration:

- enumerates every graph containing an old-namespace term (or named under
  it) with one `STRSTARTS` scan — a clean store is a fast no-op, so the
  hook is idempotent and cheap on every boot;
- rewrites subjects, predicates and objects per the term map (vocabulary →
  `fdp-o#`, the four shape locals → their URNs), covering meta graphs,
  machinery records, schema graphs *and their immutable version snapshots*
  (faithful rewrites of history — the alternative, leaving snapshots on a
  dead namespace, would make `validatedAgainst` chains dereference to
  artifacts using vocabulary nothing else emits);
- renames the shape graphs (+ their `/meta` siblings) to the URNs and drops
  the old names;
- backfills `fdp-o:MetadataService` onto a `FAIRDataPoint` root that lacks
  it; and
- on any change, drops the authorization cache (rows reference old shape
  graph URIs) and rebuilds the search index (`metadata_search.type_iri`
  holds rewritten class IRIs).

Running in-process at startup (like Alembic for Postgres) rather than as an
operator step means a deployment that just pulls the new image keeps working
with zero manual intervention — and, because it runs before any in-process
cache (SHACL validator, RD registry) is warmed, there is no stale-cache
window of the kind a `docker exec` migration has.

**5. Clients accept both namespaces during the transition.** The web client
and the MCP sidecar match the new IRIs first and fall back to the old ones,
so they work against both pre- and post-0.16 servers; no lockstep release.

## Alternatives considered

- **Keep the internal terms at `https://w3id.org/fdp/o#` and move only the
  published FDP-O terms.** Smallest migration (meta graphs untouched), but
  it preserves a 404 namespace whose name is one character away from the
  official one — the exact confusion that caused this bug — and forever
  splits the vocabulary across two roots.
- **A new FDPneo-owned namespace for the internal terms.** Honest, but the
  terms are not FDPneo-specific concepts (the reference implementation has
  the same lifecycle and resource-definition model), and the FAIR Data Team
  can evolve FDP-O directly; a proposal to the ontology is the better path.
- **Manual CLI migration only.** Explicit, but a deployment that pulls the
  new image without running it serves new IRIs over old data — record states
  unreadable, shapes unresolvable — a silently broken state. It also repeats
  the stale-SHACL-cache trap identified with `docker exec fdp profile
  migrate-modular` (the running process keeps serving pre-migration
  artifacts until restarted).

## Consequences

- Deployments upgrading to 0.16 are rewritten in place on first boot; the
  startup log carries `vocab_migration_completed` with counts (or
  `vocab_migration_noop` thereafter). `fdp vocab migrate --dry-run` previews.
- The FDP registers as **valid** at FDP Index deployments that check
  `fdp-o:MetadataService`.
- External SPARQL queries or clients hard-coding the old IRIs must update;
  this is called out in the 0.16 changelog as a breaking change.
- `metadata_search.type_iri` rows and the authz cache are rebuilt once.
- The affordance link relations (ADR-0022) change IRI — permitted there
  explicitly ("if the FDP-O WG standardizes equivalents a later ADR swaps
  the IRIs"); this ADR is that swap.
