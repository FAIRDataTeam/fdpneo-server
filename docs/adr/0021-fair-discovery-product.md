# ADR-0021: FAIR Discovery — the aggregation product: name, tiers, and validation posture

**Status:** Proposed
**Date:** 2026-07-05

## Context

ADR-0020 established products as distributions and anticipated a `registry/` context for the product previously known as the **FDP Index**. This ADR decides what that product is, what it is called, and the two scope questions that shape its design.

**What the reference-implementation Index actually does.** Verified against `HarvesterService` in `FAIRDataTeam/FAIRDataPoint`: a ping intake endpoint FDPs call on deployment and periodically thereafter; an entry list with reachability states and IP rate limiting; and a **full recursive harvest** of each registered FDP — the crawler follows `ldp:contains` links (or, absent LDP containers, child relationships declared in a default navigation SHACL) and stores every visited node's triples in the main repository under a **named graph per source**. Two of its patterns are worth carrying forward: graph-per-source storage and SHACL-declared traversal. Its limits are equally instructive: harvest is wipe-and-recrawl (all previously harvested data for a source is deleted, then the whole tree is re-walked), there is no incremental update, no record-level provenance distinguishing source modification time from harvest time, no validation of harvested metadata against any profile, no conformance feedback to data holders, FDP/LDP is the only source protocol, and the aggregate corpus is not curated for downstream harvesting.

**The market pull: EHDS national catalogues.** Regulation (EU) 2025/327 requires each health data access body to provide a public, standardised, machine-readable dataset catalogue with per-dataset descriptions of source, scope, characteristics, nature, and access conditions (Art. 77); to display data quality and utility labels (Art. 78); and to be federated into the EU dataset catalogue (Art. 79) — i.e. the national catalogue must itself be harvestable. The metadata standard is HealthDCAT-AP (Release 5), with SHACL validation via the EC ITB validator; the implementing act on minimum metadata elements is due 26 March 2027 and applies from 26 March 2029.

**Field experience (Health-RI / HDAB-NL).** The Dutch national catalogue harvests node FDPs into a CKAN backend. Their published pain points read as a requirements list: manual source registration; catalogue entities wrongly harvested as datasets; discrepancies between FDP behaviour and SHACL specs; multiple metadata-model versions coexisting in one catalogue; multilingual label fallback; "last updated" reflecting harvest time instead of source modification time; and hand-built bridges for non-FDP sources (CEDAR, Molgenis, Beacon).

**Naming forces.** "FDP Index" ties the product to the FDP brand although sources will not all be FDPs; "index" reads to part of the community as a FAIRness *metric*; and the product indexes metadata describing **any digital object**, not only data — so "Data" in the name would wrongly narrow the scope, which rules out FAIR Data Catalogue/Index variants. Meanwhile "ping the index" is entrenched protocol vocabulary worth keeping.

## Decision

### 1. Name: FAIR Discovery

The product is named **FAIR Discovery**. The name states the user value — discovering anything described by metadata — and scales over both tiers below without strain. **"Index" is retained as the technical term inside the product**: endpoints, documentation, and the ecosystem protocol keep speaking of pinging the index and index entries. Product names the value; internals keep the entrenched jargon.

Naming-scheme consequences (amends the tables in `docs/architecture/repo-organization.md`): distribution `discovery` (replacing `index`), container image `ghcr.io/fairdatateam/fair-discovery`, release tags `discovery/v…`, client app shell `apps/discovery`. The module packages remain `fairplatform-registry` and gain `fairplatform-harvest` (see §3).

### 2. Two tiers, one product

FAIR Discovery operates in two tiers, selectable **per source** (not a product-wide mode):

- **Directory tier** — what the reference Index does, done properly: registration, ping intake (wire-compatible with existing FDP pings), reachability/health monitoring, entry lifecycle, and a browsable, searchable directory of sources. Cheap to run; preserves the ecosystem-directory use case (home.fairdatapoint.org, Station Directory).
- **Catalogue tier** — adds full metadata harvesting with incremental updates, profile validation and conformance reporting, multilingual and quality-label surfaces, and **re-publication of the aggregate as a spec-compliant FDP/DCAT-AP endpoint** — so a FAIR Discovery deployment can serve as an EHDS national catalogue that HealthData@EU harvests, and the MCP bridge (ADR-0018) works against it unchanged.

The elegant property to preserve: **the catalogue is an FDP whose content is harvested rather than authored.** Serving stack, search, client components, and agent consumption are the same as FDPneo's; the delta is registry + harvest + conformance.

### 3. Contexts

Two new bounded contexts, wired per ADR-0020 manifests into the `discovery` distribution:

- **`registry/`** — source lifecycle: ping intake, self-service registration with verification, trust/approval policy, entry states, health monitoring, rate limiting.
- **`harvest/`** — scheduled jobs behind a **connector protocol**: FDP/LDP tree walk first (traversal declared in SHACL, per the reference implementation's pattern), generic DCAT-AP endpoint/dump connector second, bridge connectors (CKAN, Molgenis, Beacon) later. Incremental harvesting (ping-on-change plus HTTP conditional requests); each source in its own named graph; record-level provenance keeps source `dct:modified` distinct from harvest timestamp. Harvested data is a disposable projection — the source remains authoritative and full re-harvest is always safe.

Validation reuses the `metadata/` SHACL schema machinery: profiles are data (Health-RI core, HealthDCAT-AP, DCAT-AP coexist and are versioned), which is also the answer to metadata-model evolution. Search reuses the `search` context via a harvested-corpus provider (ADR-0020 §3).

### 4. Validation posture: configurable per deployment

What happens to a harvested record that fails profile validation is a **deployment policy**, not a product constant:

- **Accept-and-flag (default):** ingest everything, record a conformance level per record, expose conformance dashboards and per-source reports to data holders. This matches the field reality of coexisting model versions and keeps the catalogue useful during transitions.
- **Strict:** nonconforming records are excluded from designated surfaces. Strictness can differ per surface — e.g. lenient for the national browse view, strict for the feed the EU dataset catalogue harvests.

## Alternatives considered

**Keep the name "FDP Index".** Rejected. Wrong brand coupling (non-FDP sources), collision with FAIRness-metric reading, and it means nothing to the procurement audience for national catalogues. Continuity is preserved where it matters — the wire protocol and internal vocabulary.

**FAIR Data Catalogue / FAIR Data Index.** Rejected: "Data" narrows the scope; the product indexes metadata about any digital object. "Catalogue" additionally collides with `dcat:Catalogue` as an entity type inside sources — a live confusion in the field (catalogue entities harvested as datasets).

**Single-tier product (catalogue only).** Rejected. It drops the lightweight directory use case that exists today and forces every deployment to carry harvest/validation cost. The tier split is a per-source setting, not a fork.

**Reject-only validation.** Rejected as the sole posture. Field experience shows model transitions are the steady state; a catalogue that ejects records on every schema bump punishes exactly the data holders it needs. Strictness belongs to the deployment (and per surface), hence §4.

**Separate harvester product/repository.** Rejected. Harvest is meaningless without the registry and the serving stack; ADR-0020's distribution mechanism already provides product-level separation without a repo split.

## Consequences

**Easier:**

- A clear product story for EHDS national catalogues: "a FAIR Discovery deployment configured as the national dataset catalogue", with the regulation's own word living in the deployment, not the brand.
- The directory tier keeps today's ecosystem role (and the Station Directory / Train Depot evolution) at today's operating cost.
- Conformance reporting turns validation from a gatekeeper into a service to data holders — addressing the documented pain of the current national-catalogue stack.
- Downstream federation (Art. 79) and agent consumption come for free from re-publishing the aggregate through the standard FDP serving stack and MCP bridge.

**Harder:**

- Two new contexts to build and maintain (`registry/`, `harvest/`), plus a connector protocol whose non-FDP implementations (CKAN, Molgenis, Beacon) each carry real-world quirks.
- Incremental harvesting with provenance is substantially more complex than wipe-and-recrawl; correctness (deletions, moves, partial failures) needs explicit design — see `docs/architecture/discovery.md`.
- Per-surface validation strictness adds a policy dimension to configuration and testing.
- The rename touches public artifacts (image names, tags, app shell, docs) and community habit.
