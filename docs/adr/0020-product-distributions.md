# ADR-0020: Products as distributions — module manifests and composed deployments

**Status:** Proposed
**Date:** 2026-07-04

## Context

The FDP server is one member of a product family that is now coming into view:

- **FAIR Data Point (FDP)** — the metadata repository this codebase implements today.
- **FDP Index** — a registry of FDPs (ping intake, harvesting, health monitoring, search over the harvested corpus). In the ecosystem roadmap the Index also evolves into the **Station Directory** and **Train Depot** (hosting train metadata) of the FAIR Data Train platform, and is a candidate implementation for **EHDS national data catalogs**.
- **FAIR Data Station (FDS)** — a FAIR Data Train application type that shares the FDP's metadata-provisioning functionality and adds train reception/processing and secured data access.

The previous reference implementation shipped the FDP and the Index from one codebase differentiated by a runtime configuration toggle. The costs were real: every FDP deployment carried Index code, Index changes could regress the FDP, and there was no product-level release artifact.

Additional forces:

- Different products will not necessarily be maintained by the same team.
- Deployments in regulated contexts (e.g., national catalogs) favor minimal, auditable artifacts: code that is not part of the product should not be in the image.
- Some functionality must exist in *every* product but with different reach — search is the canonical example: the FDP searches its own records, the Index searches the harvested corpus, the FDS extends search beyond metadata to data.
- The word "profile" is already taken in this codebase (metadata schema profiles, `profiles/`). This ADR uses **distribution** for a product-level composition.
- ADR-0001 established the modular monolith with enforced bounded contexts. This ADR builds directly on it: because boundaries are already explicit, a product can be defined as a *set* of modules.

## Decision

A **product is a distribution: a static, declarative composition of bounded-context modules selected at packaging time** — not a runtime feature flag.

### 1. Module manifest

Each bounded context exports exactly one manifest object describing everything the composition root needs to wire it:

```python
@dataclass(frozen=True)
class ModuleDef:
    name: str                                    # "metadata", "policy", …
    settings_model: type[BaseSettings] | None    # module's settings section
    routers: Sequence[RouterFactory]             # built with injected deps
    orm_metadata: MetaData | None                # Postgres tables it owns
    migration_branch: str | None                 # alembic branch label
    event_subscriptions: Sequence[Subscription]  # event-bus handlers
    lifespan_resources: Sequence[ResourceFactory]
    cli_commands: Sequence[Command]
    provides: Mapping[type, Factory]             # capability registry, e.g. {SearchProvider: …}
```

The manifest is the module's *only* composition surface. `create_app()` stops importing module internals and becomes a generic loop over a distribution's manifests. The existing boundary rules (ADR-0001, `CLAUDE.md`) are unchanged; the manifest formalizes what the composition root was already doing by hand across ~20 `include_router` calls.

### 2. Distributions

A distribution is an explicit Python definition — a named list of `ModuleDef`s plus settings defaults:

```
src/fdp/distributions/
    fdp.py      # identity, metadata, policy, access, data, search, metrics
    index.py    # identity, metadata, registry, search, metrics
    fds.py      # fdp modules + train (future)
```

Composition is **static and explicit**. There is no dynamic plugin discovery, no entry-point scanning, no runtime toggle: what a product contains is readable in one file and auditable in review. Anticipated new contexts — a generic `registry/` context (registration, ping intake, scheduled harvest, health, corpus indexing) with Index/Directory/Depot as thin specializations, and later a `train/` context — plug into the same mechanism; this ADR defines the mechanism, not those modules.

### 3. Search as a first-class context with providers

Search currently lives inside `metadata` (`fdp/metadata/search/`). It is lifted into its own `search` context that owns the query/saved-query/autocomplete API and defines a `SearchProvider` protocol. Wired modules register providers through `ModuleDef.provides`: `metadata` contributes the own-records provider, `registry` the harvested-corpus provider, and a future `train`/`data` extension a data-search provider. The search API and the client component are identical across products; reach differs by composition, with no conditionals inside `search`.

### 4. Packaging: two stages, one contract

**Stage A — wiring-level (now).** One package, one image; the distribution is selected by the entry point (`fdp serve --distribution fdp`) or a build argument producing per-product images. Unselected code ships but is never imported, wired, or migrated.

**Stage B — package-level (when a second team or divergent release cadence arrives).** The repo becomes a **uv workspace**: each module an internal package (`fdp-shared`, `fdp-storage`, `fdp-metadata`, `fdp-policy`, `fdp-access`, `fdp-search`, `fdp-registry`, `fdp-train`, …) and each distribution a thin package declaring its subset as dependencies, built into its own Docker image containing only what it depends on. Core packages can then be published to a package index so an external team can pin `fdp-metadata==2.x` and release independently.

The `ModuleDef` contract is identical in both stages, so moving a module from a sub-package of `fdp` to a workspace package is mechanical and can be done one module at a time.

### 5. Migrations

Alembic **branch labels**, one per table-owning module (`identity`, `metadata`, `policy`, `metrics`, `registry`, …). A distribution upgrades only the branches of its wired modules (`alembic upgrade identity@head metadata@head …`, driven by the manifests). The single `migrations/` tree is retained in Stage A; in Stage B each package carries its own version location, registered via `version_locations`.

### 6. CI

CI runs a matrix per distribution: its test suite (module tests of wired modules plus distribution-level smoke tests) and its image build. A change that breaks the Index fails in CI, not at deployment.

## Alternatives considered

**Runtime configuration toggle (previous reference implementation).** Rejected. It conflates product identity with feature flags: all deployments carry all code, the artifact does not correspond to the product, dead code inflates the audit and attack surface, and regressions cross product lines silently.

**One repository per product.** Rejected for now. With overlapping teams it maximizes coordination cost and guarantees drift in the shared metadata-provisioning core — precisely the part the FDS must reuse verbatim. Because module boundaries are enforced and Stage B produces versioned packages, extracting a module (or a product) into its own repository later is cheap; paying the multi-repo tax before a second team exists buys nothing.

**Dynamic plugin architecture (entry points, discovery, runtime loading).** Rejected. The set of products is small, known, and changes rarely; discovery machinery would add indirection without adding capability, and would make "what is in this deployment?" a runtime question instead of a code-review question.

**Git submodules to compose products from module repositories.** Rejected — see `docs/architecture/repo-organization.md` (companion document). Submodules pin commits, not versions; they complicate every contributor workflow and CI checkout, and they solve vendoring, not co-development. Package dependencies (Stage B) are the correct composition mechanism across repository boundaries.

## Consequences

**Easier:**

- Each product is a first-class, minimal, auditable artifact with its own release cadence — a direct fit for regulated deployments.
- The FDS inherits the metadata core by composition; "shares the metadata provisioning of the FDP" becomes literally true at the package level.
- A different team can own a module or distribution (CODEOWNERS on the module directory in Stage A, a package in Stage B) without a repo split.
- Cross-product functionality (search) is written once against a provider protocol.
- The Index → Station Directory / Train Depot / national-catalog evolution is a matter of specializing the `registry` context, not forking a product.

**Harder:**

- Up-front refactoring: `create_app()` must be decomposed into manifests, and `metadata.search` extracted into its own context.
- Alembic branch labels are more ceremony than a single linear history.
- CI cost multiplies by the number of distributions.
- Module authors must think in terms of the manifest contract; ad-hoc wiring in the composition root is no longer available as an escape hatch.
