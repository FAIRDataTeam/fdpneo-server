# FAIR Discovery — design of the `registry/` and `harvest/` contexts

**Status:** Draft (companion to [ADR-0021](../adr/0021-fair-discovery-product.md))
**Date:** 2026-07-05

FAIR Discovery is the aggregation product of the platform: it registers metadata
sources, harvests their metadata, validates it against profiles, and re-publishes
the aggregate for search, browsing, agent consumption, and downstream federation.
It is built as the `discovery` distribution (ADR-0020): `identity`, `storage`,
`shared`, `search`, `metadata` (serving side), plus the two contexts this
document designs — `registry/` and `harvest/`. The product decision, the
two-tier scope, and the validation posture are recorded in ADR-0021; this
document is the how.

The governing principle, worth stating once and designing around everywhere:
**harvested metadata is a disposable projection.** The source remains
authoritative; every structure below must survive "delete everything from this
source and re-harvest" without loss of registry state, configuration, or audit
history.

---

## 1. `registry/` — sources and their lifecycle

### 1.1 The source record

A **source** is anything the deployment has agreed to track. Postgres-owned
(operational state, per ADR-0003), one row per source:

- `id`, `client_url` (canonicalized; unique), `kind` (`fdp | dcat | bridge:<name>`)
- `tier` (`directory | catalogue`) — per ADR-0021 §2 a **per-source** setting
- `state` — see 1.2
- `trust` (`pending | approved | forbidden`) and who/when decided
- `last_ping_at`, `last_harvest_at`, `last_reachable_at`
- harvest configuration overrides (interval, connector options, profile set)

The source's own descriptive metadata (what the FDP says about itself — title,
publisher, contact) is *harvested*, not authored here, and lives in the triple
store with the rest of the corpus (§2.4). The registry row is bookkeeping only.

### 1.2 State machine

Reachability and validity are observations, kept separate from trust (an
administrative decision):

```
        ping/registration
              │
              ▼
          UNKNOWN ──harvest ok──▶ VALID ◀──────────┐
              │                     │              │ harvest ok
              │ fetch fails         │ fetch fails  │
              ▼                     ▼              │
          UNREACHABLE ◀──────── UNREACHABLE ───────┤
              │                                    │
              │ parse/shape fails                  │
              ▼                                    │
           INVALID ────────────────────────────────┘
              │ no successful contact for N days (default 90)
              ▼
           EXPIRED   (retained, not scheduled; revives on ping)
```

Wire-compatible with the reference implementation's entry states so existing
tooling and expectations transfer.

### 1.3 Intake

Three ways in, all landing in the same source table:

1. **Ping** (`POST /index/ping`, unauthenticated, body `{"clientUrl": …}`) —
   wire-compatible with the reference implementation and with FDPneo's outbound
   ping (server TASKS.md Phase 8.1). Rate-limited per source IP and per
   client URL. A ping from an unknown URL creates a `pending` source; a ping
   from a known one records liveness and, for catalogue-tier sources, enqueues
   an incremental harvest (§2.3) — this is how ping-on-change becomes cheap
   freshness.
2. **Self-service registration** — an authenticated form/API that creates the
   source with declared kind and contact, and runs an immediate probe. Directly
   answers the field pain of registration being a manual, operator-only step.
3. **Operator registration** — admin API, for bridges and curated deployments.

**Trust policy is a deployment setting**: `open` (pings auto-approve, the
community-directory posture), `verified` (auto-approve after a successful probe
of a well-formed root), or `curated` (operator approval required — the national
catalogue posture). Approval, refusal, and `forbidden` (blocklist) are audited.

### 1.4 Health monitoring, events, webhooks

A scheduled liveness probe (HEAD/GET on the root with content negotiation)
updates reachability independently of full harvests. Registry emits events on
the shared event bus (`source.registered`, `source.state_changed`,
`harvest.completed`, `conformance.changed`); a webhook subsystem (signed
payloads, retry with backoff, persisted for audit) forwards them externally.
The metrics context consumes the same events for the operator dashboard.

### 1.5 Admin surface

List/inspect sources, approve/forbid, trigger probe or harvest, replay events,
inspect harvest runs and conformance reports. Behind
`policy.authorize(admin, manage, "discovery:registry")`.

---

## 2. `harvest/` — connectors, scheduling, provenance

### 2.1 The connector protocol

One protocol, many source kinds. A connector turns a remote source into a
stream of record-level results — it does *not* write to storage:

```python
class Connector(Protocol):
    kind: str                                      # "fdp", "dcat", "bridge:ckan", …

    async def probe(self, source: Source) -> ProbeResult
    # cheap liveness + self-description; used by registry health checks

    async def enumerate(self, source: Source) -> AsyncIterator[RemoteRef]
    # yield (iri, type_hint, change_hint) for every record the source exposes;
    # change_hint carries whatever cheap signal the protocol offers
    # (HTTP ETag/Last-Modified, dct:modified, sitemap lastmod)

    async def fetch(self, ref: RemoteRef) -> RecordGraph
    # dereference one record to an RDF graph
```

Planned implementations, in order:

1. **`fdp`** — LDP tree walk: follow `ldp:contains`; where a source predates
   full LDP, fall back to traversal paths declared in a **navigation SHACL**
   (the reference implementation's pattern, kept — traversal is data, not
   code). The navigation shapes also declare each node's *role*
   (catalogue / dataset / distribution / other), which is what prevents the
   documented failure of catalogue entities being ingested as datasets.
2. **`dcat`** — a DCAT-AP endpoint or dump file (CSW/OAI-PMH out of scope;
   plain dereferenceable DCAT first).
3. **`bridge:*`** — per-system adapters for genuine metadata sources (CKAN
   API, Molgenis). Bridges run inside the harvester — data holders deploy
   nothing. Query interfaces that are *not* metadata sources (e.g. GA4GH
   Beacon) are out of scope — see §5.5.

Egress is allowlist-constrained (reuse `shared/` SSRF protections): a
connector may only fetch URLs under its source's registered origin unless the
source config explicitly allows more.

### 2.2 Harvest runs

Harvests are arq jobs; every run is recorded (`harvest_runs`: source, trigger
= scheduled/ping/manual, started/finished, counters, outcome, error detail).
Runs for the same source are serialized; global and per-origin concurrency
caps keep the harvester a polite citizen. Scheduling: per-source interval
(default weekly) plus ping-triggered runs, with jitter and exponential backoff
on failing sources.

### 2.3 Incremental by default, wipe-and-recrawl as the safety net

A run executes: `enumerate` → diff against the stored record set for the
source → `fetch` only new/changed records → validate (§3) → upsert; records no
longer enumerated are removed. Correctness rules:

- **Deletions.** A record absent from `enumerate` in a *successful* run is
  deleted from the projection. A *failed* run deletes nothing.
- **Partial failure.** Per-record fetch failures are recorded and skipped; the
  rest of the run proceeds. The previous version of a record that fails to
  fetch is retained and flagged stale.
- **Moves.** A record whose IRI changes is a delete + add; no rename
  heuristics.
- **Full re-harvest** (operator-triggered or after connector-version changes)
  rebuilds the source's graphs from scratch — the disposable-projection
  guarantee. This is the reference implementation's wipe-and-recrawl, demoted
  from the only mode to the recovery mode.

### 2.4 Storage and provenance

One **named graph per harvested record** (extending ADR-0007 to the harvested
corpus), grouped per source; a per-source meta graph holds the source-level
description. Each record's meta graph records, distinctly:

- `sourceModified` — the source's own `dct:modified` (if published)
- `harvestedAt` — when we fetched it
- `sourceIri`, `sourceId`, connector kind and version, content hash
- conformance results (§3)

"Last updated" surfaces **`sourceModified`, falling back to `harvestedAt` only
when the source publishes nothing** — the direct fix for the harvest-date-
as-update-date complaint. Harvested graphs are read-only through every
authoring API; they change only via harvest runs.

---

## 3. Validation and conformance

Validation reuses the `metadata` context's SHACL machinery; profiles are data
(ADR-0009/0019 posture): a deployment configures an ordered set of **profile
packs** — e.g. DCAT-AP 3, HealthDCAT-AP R5, Health-RI core v2 — each versioned,
each applying to declared record types. Multiple packs coexist, which is the
mechanism for surviving metadata-model transitions without ejecting records.

Every harvested record gets a per-pack **conformance result** (`conforms |
warnings | violations`, plus the report graph). Results are stored in the
record's meta graph and aggregated per source into a **conformance report** —
the feedback loop to data holders that today's national-catalogue stack lacks:
"your source: 412 datasets, 371 conform to HealthDCAT-AP R5, top violation:
missing `dct:accessRights`".

**Strictness is a surface policy, not an ingest gate** (ADR-0021 §4). Ingest
always stores the record and its results (accept-and-flag). Each *surface* —
national browse view, search, the EU-catalogue feed, SPARQL — declares the
minimum conformance it exposes, so one deployment can browse leniently while
feeding HealthData@EU strictly.

---

## 4. Publishing the aggregate

The catalogue tier re-publishes the harvested corpus through the standard
serving stack — **the catalogue is an FDP whose content is harvested rather
than authored**:

- LDP browsing and content negotiation over the aggregate, with per-source
  attribution and links back to the authoritative record (`dct:source` +
  provenance from §2.4).
- The `search` context indexes the corpus via a harvested-corpus
  `SearchProvider` (ADR-0020 §3): faceted, dataset-level, multilingual
  (language-tagged labels with configurable fallback order — another field
  pain point).
- SPARQL over the aggregate with the standard named-graph projection
  (ADR-0004); internal/meta graphs stay internal.
- The MCP bridge (ADR-0018) works unchanged; a later index-level MCP adds
  source discovery and query dispatch (agent-consumption vision, increment C).
- **EHDS deployment profile**: a deployment-profile bundle (architecture §12)
  packaging HealthDCAT-AP shapes, EU vocabularies, strict-feed surface policy,
  and the DCAT-AP serialization the EU dataset catalogue harvests (Art. 79),
  also consumable by DGA single information points (Art. 77(3)). Quality and
  utility labels (Art. 78) are carried as harvested metadata and surfaced in
  search/browse; Discovery displays them, it does not compute them.

The directory tier is the same product with harvesting stopped at each
source's root: registry + health + a searchable directory — today's community
index (home.fairdatapoint.org) and the FAIR Data Train Station Directory role.

---

## 5. Resolved design decisions

Resolved 2026-07-05.

1. **Triple store — require a production store; do not support RDFLib default
   in serious deployments.** ADR-0005 keeps the store pluggable via SPARQL 1.1
   Protocol; a FAIR Discovery deployment of any real size MUST run against a
   proper triple store — GraphDB, Stardog, Virtuoso, Neo4j (via its
   RDF/SPARQL surface), or equivalent. The in-process RDFLib store is for
   development and tests only. Deployment guidance names this explicitly and
   the directory tier (no corpus) may run lighter. *Consequence:* the
   corpus-scale worry is an operational prerequisite, not an engineering risk
   to solve in-code.

2. **Cross-source identity — keep every record, keyed by provenance; never
   silently dedupe.** A single (digital) object may be described by several
   metadata records from different sources, and each is a first-class record
   whose origin FAIR Discovery preserves (§2.4: `sourceIri`, `sourceId`,
   connector kind/version, `harvestedAt`). Surfaces MAY *group* records that
   resolve to the same object (via shared PIDs, ADR-0014) for presentation,
   but grouping is a display concern layered over distinct, provenanced
   records — the store never collapses two sources' records into one.

3. **Access conditions — harvest them as metadata; do not interpret the
   policy language.** If a record is harvestable (the source's data-access
   policy permits harvesting), its access conditions are simply part of the
   metadata record and are harvested with it. FDPneo (and the future FAIR Data
   Station) express these in ODRL, but other implementations use other
   approaches; FAIR Discovery is policy-language-agnostic and stores whatever
   the source publishes. It does **not** evaluate or enforce access policy —
   that is the source's and the FDP's job. Faceting by accessibility, if ever
   added, works off harvested values, not off interpreting ODRL.

4. **Schema strictness — a deployment setting, spanning "any record" to
   "strict profile".** Whether FAIR Discovery requires records to conform to a
   configured profile or accepts any metadata record is configurable per
   deployment (and per surface, per ADR-0021 §4). The accept-and-flag pipeline
   (§3) is the mechanism: at one extreme a deployment sets no required profile
   and harvests everything; at the other it exposes only records conforming to,
   e.g., HealthDCAT-AP R5 on its EU-feed surface. The Art. 77(4) implementing
   act (due 2027-03-26) fixes the minimum elements for the strict EHDS profile
   pack; that pack tracks the act, but strictness itself is deployment policy.

5. **Beacon — out of scope as a source; it is a data service, not a metadata
   source.** Verified against the GA4GH Beacon v2 spec: a Beacon exposes
   *service*-level metadata (`/info`, `/service-info`, `/configuration`,
   `/entry_types`) and a dataset listing, but it is fundamentally a **query
   API** over genomic/clinical records returning aggregate answers — it does
   not publish harvestable, descriptive per-dataset metadata records of the
   kind FAIR Discovery aggregates. So there is no `bridge:beacon` connector.
   The correct pattern (already reflected in the agent-consumption vision and
   the ERDERA VP-Index model): a dataset's metadata record lives in an FDP and
   carries a `dcat:DataService` pointing at the Beacon endpoint; FAIR Discovery
   harvests that record from the FDP, and the Beacon is reached at *query*
   time by a consumer, not at harvest time. (The connector list in §2.1 keeps
   `fdp`, `dcat`, and `bridge:*` for genuine metadata sources such as CKAN and
   Molgenis; Beacon is removed from scope.)
