# FDP Server ↔ FAIR Data Train Platform Vision — alignment review

**Status:** Discussion draft (advisory)
**Date:** 2026-06-12
**Scope:** What the **FDP Server** codebase must change to fit the *FAIR Data Train
Platform Architecture Vision* — i.e. to become a *specialized assembly of a shared
platform* rather than a standalone application.

> This is a review, not a change. Nothing here is implemented; it is a map of the
> gaps and a recommended sequence so the team can decide what to adopt and when.

---

## 1. Framing: what role the FDP plays in the vision

The vision (§6.1) defines the **FAIR Data Point** product narrowly:

> **Purpose:** Metadata publication and FAIR resource exposure.
> **Capabilities:** Metadata, Identity, Metadata Governance.

That matters for scope. Most of the vision's heavier capabilities — **Train**,
**Consent**, the standalone **Authorization Service**, **Storage of arbitrary
digital objects** — belong to the **FAIR Data Station (FDS)** and other products,
**not** to the FDP. So the FDP server's job is *not* to grow all of them. Its job
is to:

1. expose its three capabilities (**Metadata**, **Identity**, **Metadata
   Governance**) cleanly enough that they can be **packaged and reused** by the
   other products; and
2. adopt the **cross-cutting platform concerns** every product shares —
   **tenancy**, **kernel services**, **federation**, **audit**, **digital-object
   centricity** — so the FDP is genuinely "the same platform, assembled
   differently," not a fork.

Read this document with that lens: a gap is only an *FDP* gap if it touches those
two responsibilities. Where a capability is really another product's, we note the
**seam** the FDP must expose, not a feature it must build.

---

## 2. Executive summary

**The good news.** The FDP is already a modular monolith with explicit bounded
contexts, an in-process event bus, a ports-and-adapters storage layer, OIDC
identity, an ODRL policy decision point, runtime-mutable resource definitions, and
first-class managed schemas/policies/licenses. Structurally it is *close* to the
vision's "DDD + hexagonal + event-driven, capabilities behind interfaces" style.
Several vision concepts already exist under different names.

**The three structural gaps that actually require architectural work:**

| # | Gap | Severity | Mostly an FDP concern? |
|---|---|---|---|
| **A** | **No tenancy.** Every resource is implicitly single-tenant. The vision makes *Tenant* a first-class isolation boundary across all resources (§7). | **High** | Yes — cross-cutting, touches every module + storage |
| **B** | **No platform/product separation.** The FDP is one deployable; the vision wants the kernel + capabilities to be reusable building blocks (§5). `shared/` is a kernel-in-disguise but is FDP-internal. | **High** | Yes — packaging/organization |
| **C** | **"Metadata record" is not generalized to "Digital Object."** The model is DCAT-typed; the vision centres on arbitrary digital objects (ontologies, shapes, models, trains, APIs…) (§2.2, §3). | **Medium** | Yes — but the foundation already exists (ADR-0009) |

Everything else is either already aligned, a naming/lifecycle adjustment, or a
*seam* the FDP exposes to another product.

---

## 3. Capability mapping: vision → current code

| Vision capability (§5.3) | Current FDP location | Status |
|---|---|---|
| **Kernel** (config, identity, events, security, observability) | `shared/` (events, errors, logging, context, security_headers, limits, ssrf) + `identity/` + `config.py` | ⚠️ Exists but **not extracted**; tenant + a formal kernel boundary missing |
| **Metadata** | `metadata/` (LDP, records, schemas, meta, shape_provider, instances) | ✅ Strong |
| **Search** | `metadata/search/` (sub-module of metadata) | ⚠️ Real, but **nested inside** metadata, not a standalone capability |
| **Governance** (policy mgmt + evaluation) | `policy/` (ODRL PDP, cache, resolver) + `metadata/policies.py` + `metadata/lifecycle.py` | ⚠️ Split across `policy/` and `metadata/`; lifecycle ≠ vision lifecycle |
| **Authorization** (lifecycle of authz records) | *implicit* — ODRL Offers via `dct:rights`; no first-class Authorization record | ❌ Conceptual gap |
| **Consent** | — | ❌ Absent (correctly — FDS concern; FDP needs only a seam) |
| **Train** | — | ❌ Absent (correctly — FDS/Garage concern) |
| **Audit** | `metadata/audit.py` (record audit graph) + `policy_decisions_audit` (Postgres) + structlog | ⚠️ Exists as two record-scoped mechanisms, **not a capability** |
| **Storage** | `storage/` (triplestore adapter + Postgres) | ✅ Clean port/adapter |
| **Federation** | Phase 8 (FDP Index protocol) — **not built** | ❌ Absent |
| **Identity** | `identity/` (OIDC, JWKS, API keys, principal, user facade) | ✅ Strong |

Legend: ✅ aligned · ⚠️ exists but needs realignment · ❌ missing.

---

## 4. Gap analysis and change suggestions

### A. Multi-tenancy (the dominant change)

**Today.** There is no `Tenant`. The deployment *is* the tenant. The base URL,
the single applied profile, the root FAIR Data Point, the authz cache, the search
index, and the metrics tables are all global.

**Vision (§7).** Tenant is an isolation boundary; *Metadata, Policies,
Authorizations, Trains, Audit Events, Digital Objects* all belong to a tenant.
Three ownership tiers: Platform Owner / Tenant Owner / Digital Object Owner.

**Suggested changes (FDP server):**

1. **Add a tenant to the request context.** Extend `shared/context.py`
   `RequestContext` with a `tenant_id` resolved at ingress (subdomain, header, or
   an OIDC claim) in `identity/middleware.py`. This is the keystone — most other
   tenancy work hangs off it.
2. **Decide the triple-store isolation model and write it as an ADR.** Options,
   in increasing strength: (a) **graph-prefix per tenant** (`{base}/t/{tenant}/…`)
   reusing one store — least disruptive, builds on ADR-0007's one-graph-per-record;
   (b) **named-graph + mandatory tenant filter** layered into `PDP.authorized_graphs`
   and the SPARQL rewriter (`access/rewriter.py`) as a *second* structural gate
   alongside the existing `is_internal_graph_uri` / publication-state gates;
   (c) **store-per-tenant**. Recommendation: (a)+(b) for SaaS, (c) available for
   regulated single-tenant. This is an **ADR-0007/0004 amendment**.
3. **Tenant-scope the operational stores.** Add a `tenant_id` column to
   `authz_index`, `metadata_search`, `runtime_settings`, `profile_applied`,
   `record_audit`, and the metrics tables; make it part of every lookup key.
   (Metrics stays anonymous — tenant is an aggregation dimension, never joined to
   identity; preserves ADR-0002.)
4. **Profiles become per-tenant.** Today one profile bootstraps the deployment.
   A tenant needs its own root FDP + resource definitions. The applier, the RD
   cache, and the OpenAPI generation must key on tenant. This is the largest
   single piece of work and should be its own design.
5. **Ownership tiers.** Map Platform Owner → a super-admin role; Tenant Owner →
   tenant-scoped admin; Digital Object Owner → the existing ODRL `dct:rights`
   owner. The role model in `identity/` and `policy/` needs a tenant dimension.

**Effort:** large, cross-cutting. **Recommendation:** do *not* retrofit ad hoc —
write a "Tenancy" ADR first, then thread `tenant_id` through context → storage →
PDP → search in that order. Until then, treat the current single-tenant deployment
as "tenant = default," which keeps the door open.

### B. Platform kernel + product-line packaging

**Today.** `shared/` is the de-facto kernel (event bus, errors, logging, context,
namespaces, security headers, rate limits, SSRF guard) but it is an *internal*
package of one application. CLAUDE.md already enforces "shared imports nothing" —
the discipline for a kernel is in place; the **packaging** is not.

**Vision (§5).** A reusable **Platform Kernel** + **Capability Modules** assembled
into products. FDP, Station Directory, FDS, etc. are *assemblies*.

**Suggested changes:**

1. **Promote `shared/` to a versioned kernel package** (e.g. `fdp-platform-kernel`)
   carrying: config base, identity/request-context, the event bus, security,
   observability, error envelope, RDF/namespace utilities. Add the missing kernel
   concern — **tenant management** (gap A) — here.
2. **Make each bounded context an independently buildable capability package**
   with an explicit public interface (see C). The FDP product becomes a thin
   composition (`main.py` already plays this role — it wires routers, the PDP, the
   event bus, the RD cache). Formalize that: the product is a manifest of
   capabilities + their wiring.
3. **Capability deploy-ability / plugins (§8.3).** The vision wants capabilities
   deployable as plugins or independently configurable. Today everything is wired
   unconditionally in `main.py`. Introduce **feature/capability flags** that gate
   whole routers + their event subscriptions (the `features` block in `GET /config`
   and `GET /info` already hints at this — make it load-bearing, not descriptive).

**Effort:** medium-large, mostly mechanical once interfaces (C) are crisp. This is
the change that turns "FDP" into "FDP-as-an-assembly." **Caution:** do this *after*
the capability interfaces are stable, or you will version-churn the packages.

### C. Formalize capability boundaries (hexagonal ports)

**Today.** Boundaries are respected by convention (CLAUDE.md import rules) and the
event bus, but only `storage/` and `policy/` expose a true narrow interface
(`authorize(subject, action, resource)`). `metadata/` is a large context that also
contains **search**, **audit**, **lifecycle/governance**, labels, autocomplete,
dashboard — several of which the vision names as *separate* capabilities.

**Suggested changes:**

1. **Lift Search out of `metadata/`** into a top-level `search/` capability with
   its own port (it already subscribes to record events — the seam exists). The
   Station Directory product (§6.2) is essentially *Metadata + Search + Identity*;
   a standalone Search capability is what makes that assembly possible.
2. **Lift Audit out** (see G below) into an `audit/` capability.
3. **Name the Governance capability explicitly.** Today policy evaluation is in
   `policy/` but policy *documents* (`metadata/policies.py`), *licenses*, and the
   publication *lifecycle* (`metadata/lifecycle.py`, `states.py`) live in
   `metadata/`. Group these under a Governance capability with one interface, or at
   least document that "Governance = `policy/` (PDP) + the policy/license/lifecycle
   slice of `metadata/`."
4. **Every capability exposes a Protocol-typed port** (like the `ContainerRegistry`
   and `PDP` protocols already do) and communicates outward only via that port or
   the event bus — the vision's §8.2 rule ("no direct cross-module DB access") is
   already a CLAUDE.md rule; make it a package boundary so it is *enforced*, not
   *trusted*.

**Effort:** medium. High leverage — it is the prerequisite for B and for the other
products.

### D. Digital Object centricity

**Today.** The model is metadata records (DCAT-typed) plus a growing set of managed
RDF document kinds — schemas, policies, licenses, resource definitions — each
already a first-class, versioned, lifecycle-bearing record (ADR-0009/0010/0012).
This is **most of the way** to a generic Digital Object.

**Vision (§3).** A uniform **Digital Object** with Identifier, Type, Owner,
Metadata, Policies, Provenance, Lifecycle State, Version — instances of which
include ontologies, vocabularies, shapes, APIs, AI models, trains, workflows.

**Suggested changes:**

1. **Name the abstraction.** Extract the common record machinery (graph CRUD +
   meta graph + `owl:versionInfo` + `dct:rights` + publication state + audit) into
   a `DigitalObject` concept the typed records specialize. The pieces already exist
   in `metadata/repository.py`, `meta.py`, `states.py`, `audit.py`; today they are
   applied to records but not reified.
2. **Generalize ResourceDefinitions beyond DCAT.** ADR-0009 already lets an admin
   register new typed records at runtime. The vision's ontologies/vocabularies/
   shapes are *already* expressible (schemas are RDF records). Trains, AI models,
   workflows would slot in as additional resource-definition types — confirm the RD
   model can carry non-DCAT class hierarchies (the 15.2 modular composition work
   shows it can compose arbitrary shape chains).
3. **Don't over-build.** The FDP only needs *metadata about* trains/models, not to
   execute them. Keep the FDP a publisher of digital-object *descriptions*; leave
   execution to the FDS.

**Effort:** medium, mostly refactor + naming; the foundation is real.

### E. Governance lifecycle alignment

**Today (ADR-0010).** `DRAFT → PUBLISHED → ARCHIVED` with owner/admin transitions.

**Vision (§4.1).** `Draft → Review → Approved → Published → Deprecated`, with
**approval workflows** and **quality assurance**.

**Suggested changes:**

1. **Extend the state machine** in `metadata/states.py` with `REVIEW` and
   `APPROVED`, and add `DEPRECATED` (either replacing or complementing `ARCHIVED`).
2. **Add an approval step** — a transition that requires a *different* principal
   than the author (segregation of duties). This is new authz logic in
   `metadata/lifecycle.py` and a small role addition.
3. **Quality assurance hook** — SHACL-on-write already exists; surface a
   "validation report" on the Review→Approved transition. Largely reuses the
   validator.

**Effort:** small-medium. Backward-compatible if the new states are additive and
the default flow stays Draft→Published for tenants that don't enable review.

### F. Authorization as first-class records

**Today.** Access is governed by ODRL **Offers** referenced via `dct:rights`
(ADR-0006/0012). Permissions/Prohibitions only — **Duties/obligations are
deliberately not enforced**, and there is no validity period or standalone
"Authorization" record.

**Vision (§4.3).** An Authorization is a record with Subject, Object, Granted
Rights, Conditions, **Obligations**, **Validity Period**, Provenance.

**Suggested changes:**

1. **This is largely the Authorization Service product (§6.5), not the FDP.** The
   FDP's job is to be a clean **PEP** that calls an authorization **PDP** — which
   it already is (`policy.authorize(...)`). The key FDP change is to make the PDP a
   **swappable port**: in a standalone FDP it is the local ODRL evaluator; in a
   platform deployment it can delegate to the central Authorization Service.
2. **If the local model evolves:** add **validity period** (`odrl:constraint` on
   `dateTime` — already within the ODRL profile's reach) and revisit the deferred
   **Duty/obligation** support (currently out of scope by ADR-0006). Obligations
   need an enforcement workflow the FDP does not have, so this is genuinely a
   platform-level capability.

**Effort:** the *port* change is small and high-value; full obligation enforcement
is large and out of FDP scope.

### G. Audit as a capability

**Today.** Two record-scoped mechanisms: the per-record audit graph
(`metadata/audit.py`, the `<record>/audit` sibling) and the `policy_decisions_audit`
Postgres table, plus structlog. There is no unified, queryable, immutable audit
event stream.

**Vision (§3, §5.3).** Audit is a capability producing **immutable** records of
platform actions, tenant-scoped.

**Suggested changes:**

1. **Promote audit to a capability** (`audit/`) that subscribes to the event bus
   (the bus already carries `RecordCreated/Modified/Deleted/StateChanged`) and
   writes an append-only, tenant-scoped event log.
2. **Unify the two existing sinks** behind it; keep the RDF audit graph for
   record-local provenance (ODRL Agreements per ADR-0007) and add the
   cross-cutting platform event log alongside.
3. **Immutability** — enforce append-only at the storage layer (no update/delete
   on audit rows).

**Effort:** medium; the event sources already exist, so this is mostly a new
subscriber + table + read API.

### H. Federation capability

**Today.** Not built. Phase 8 (FDP Index protocol: ping, harvest, index entries,
webhooks) is fully specified in `TASKS.md` but unimplemented.

**Vision (§2.2 "Federation by Default", §5.3, §6.6).** Every component participates
in federation; a Federation Hub product is *Metadata + Search + Federation*.

**Suggested changes:**

1. **Implement Phase 8 as the `federation/` capability** — the design already
   exists. Outbound ping (FDP-as-node) is the minimum for an FDP to be discoverable;
   the Index/Hub side is the Station Directory / Federation Hub product.
2. **Make the policy/license/schema catalogs harvestable** (Phase 14.7 already
   leaves the seam) so federated discovery of governance documents works.

**Effort:** medium-large but **already specified** — this is execution, not design.

### I. Consent + Train seams (not FDP features)

The FDP does not manage consent or trains. To stay composable it only needs to:

- not assume `dct:rights`/ODRL is the *only* governance input (so a Consent
  capability can be layered as another structural gate, exactly like publication
  state is today); and
- keep the Digital Object model open enough that a *train* or *consent record* is
  just another typed digital object it can publish metadata about.

No FDP build work; just don't close these doors.

### J. Ownership model (§7.3)

Add the Platform/Tenant/Digital-Object ownership tiers. Digital-Object owner exists
(ODRL `dct:rights`). Platform + Tenant owners are **role + tenant-scope** additions
that fall out of gap A. No separate work once A lands.

---

## 5. What is already aligned (so we don't re-litigate it)

- **DDD + bounded contexts + event-driven** (§8.1) — already the architecture
  (ADR-0001, the in-process event bus, the CLAUDE.md import rules).
- **Hexagonal storage** (§8.1) — the triple-store adapter and Postgres repository
  are clean ports (ADR-0005).
- **API-first, semantic-web native, RDF/linked-data, OIDC** (§9) — all true today.
- **Metadata as a first-class, versioned, lifecycle-bearing, governed resource**
  (§2.2, §4.1) — ADR-0007/0009/0010/0012.
- **Governance as a native capability, not bolted on** (§2.2) — the PDP is a PEP
  on every method; SHACL-on-write is structural.
- **Container/cloud-native** (§9) — Helm + compose already shipped.

---

## 6. Recommended sequence

Ordered by *unblocking value* and dependency, not by vision section number:

1. **Write the Tenancy ADR** (gap A) and add `tenant_id` to the request context —
   nothing else multi-tenant is safe until this exists. Ship "tenant = default" so
   current behaviour is unchanged.
2. **Formalize capability ports + lift Search and Audit out of `metadata/`** (gaps
   C, G). Prerequisite for everything product-line.
3. **Thread tenancy through storage → PDP → search** (gap A continued).
4. **Make the PDP a swappable port** (gap F) — small, decouples the FDP from the
   future Authorization Service.
5. **Extract the kernel + capability packages** (gap B) — only once interfaces are
   stable.
6. **Implement Federation / Phase 8** (gap H) — already designed.
7. **Governance lifecycle: Review/Approved/Deprecated** (gap E) — additive,
   independent, can slot in anytime.
8. **Reify the Digital Object abstraction** (gap D) — refactor once the above
   settle.

Consent, Train, full obligation enforcement, and the standalone Authorization
Service are **other products** — the FDP only keeps their seams open.

---

## 7. ADR implications

New or amended ADRs this vision implies (FDP-side):

- **New: Tenancy and isolation model** (the keystone).
- **New: Platform kernel + product-line packaging** (supersedes the "single
  application" assumption in ADR-0001).
- **New: Audit as a capability** (formalizes today's ad-hoc audit sinks).
- **Amend ADR-0004/0007** for tenant-scoped graph projection.
- **Amend ADR-0010** for the Review/Approved/Deprecated lifecycle.
- **Amend ADR-0006** scope note for authorization validity periods / the PDP-as-port
  delegation seam.
- **Promote Phase 8** (Index protocol) to the Federation capability.

---

## 8. One-paragraph verdict

The FDP server is already built in the architectural *style* the vision mandates —
the distance is in **scope and packaging**, not in re-platforming. Three changes do
the heavy lifting: make **tenancy** a first-class axis, **extract the kernel and
capabilities** into reusable packages with explicit ports, and **reify the Digital
Object**. The rest is either already present under a different name, a small
additive lifecycle/role change, or genuinely another product's concern for which
the FDP need only keep a seam open. Critically, none of it requires abandoning the
one-graph-per-record model, the ODRL PDP, or the LDP surface — they all survive as
capabilities of the larger platform.
