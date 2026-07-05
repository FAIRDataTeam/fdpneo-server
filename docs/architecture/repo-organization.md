# Repository organization — local and GitHub

Companion to [ADR-0020](../adr/0020-product-distributions.md). ADR-0020 decides *what a product is* (a distribution composed of modules); this document decides *where code lives* so that model works across teams, locally and on GitHub.

## Current state

The local `fdp-neo/` folder is a plain directory holding three independent git repositories:

| Local dir | GitHub remote | Notes |
|---|---|---|
| `server/` | `FAIRDataTeam/fdpneo-server` | Python/FastAPI modular monolith |
| `client/` | `FAIRDataTeam/fdpneo-client` | Vue/TypeScript client |
| `mcp/` | **none** | MCP sidecar (ADR-0018) — exists only on a local disk |

## Recommendation in one paragraph

Keep **three repositories** — server, client, mcp — and make each one an *internal workspace* (uv for Python, pnpm for the client) whose packages are the bounded-context modules. Products (FDP, Index, FDS) do **not** get their own repositories: they are distributions built by CI from the same repo, per ADR-0020. Do **not** use git submodules. If cross-repo aggregation is wanted for development convenience, add a lightweight meta-repo with a compose file and a clone script — nothing more.

## Why not git submodules

Submodules look like the natural way to compose "an FDS repo = server core + train module", but they solve the wrong problem:

- A submodule pins a **commit**, not a version. There is no dependency resolution, no compatibility range, no changelog discipline — every update is a manual pointer bump that must be committed in the parent.
- Every contributor workflow gains failure modes: `clone` without `--recurse-submodules` yields empty directories, submodule checkouts sit on detached HEADs, and a pushed parent can reference an unpushed child commit, breaking everyone else's build.
- CI must orchestrate multi-repo checkouts with matching credentials.
- They were designed for **vendoring** third-party code at a fixed revision, not for active co-development of tightly related modules.

The composition mechanism you actually want across repository boundaries is a **package dependency** (`fdp-metadata==2.x` from a package index), which carries versioning, resolution, and an explicit compatibility contract. Inside one repository, the workspace gives the same modularity with zero synchronization cost. Submodules occupy the worst point between those two options.

## Per-repository plan

### `fdpneo-server`

Stays the single server repo for **all** server-side products. Evolution per ADR-0020: first manifests + distribution definitions (Stage A), then a uv workspace of module packages (Stage B) when a second team or divergent release cadence arrives:

```
fdpneo-server/
  pyproject.toml            # uv workspace root
  packages/
    fdp-shared/  fdp-storage/  fdp-identity/  fdp-metadata/
    fdp-policy/  fdp-access/   fdp-search/    fdp-registry/   # fdp-train later
  distributions/
    fdp/  index/  fds/       # thin packages: dependency set + Dockerfile
```

Different teams inside one repo are handled with GitHub mechanics, not repo splits: a **team per product** in the `FAIRDataTeam` org and a `CODEOWNERS` file mapping module directories/packages to teams (e.g. `packages/fdp-registry/ @FAIRDataTeam/index-team`), with branch protection requiring code-owner review.

### `fdpneo-client`

Mirror the same idea with an npm workspace (the repo already uses npm; pnpm works equally well): shared packages (`ui-kit` design system, `metadata-components` browsing/editing, `search-components`) plus one thin app shell per product (`apps/fdp`, `apps/index`, `apps/fds`). Same repo-level rules: CODEOWNERS per package, CI matrix building each shell.

### `fdpneo-mcp`

Create the GitHub repository and push — today this code has no remote and lives on one disk. Keep it a separate repo: ADR-0018 already defines it as a standalone sidecar with its own release cadence.

### Optional: `fdpneo-workspace` (meta-repo)

A small repo — not a submodule parent — for whole-ecosystem concerns:

- `compose.yaml` running the full stack (server distribution + client + mcp + Postgres + triple store + Keycloak) for local development;
- a `clone.sh`/`justfile` that clones the sibling repos next to it (replicating today's `fdp-neo/` folder layout);
- ecosystem-level documentation that belongs to no single repo (product family overview, FAIR Data Train integration notes).

This gives the aggregation convenience people reach for submodules to get, without coupling any repository's history to another.

## Releases and images

Tag per product from the server repo (`fdp/v2.0.0`, `index/v1.0.0`), and have CI publish one image per distribution to GHCR (`ghcr.io/fairdatateam/fdp`, `.../fdp-index`, `.../fds`). The repo is shared; the release artifacts are strictly per product.

## If a real split becomes necessary later

If a product team eventually needs full autonomy (own repo, own cadence, restricted visibility), the path is: publish the core packages from `fdpneo-server` to a package index (GitHub Packages or PyPI), then extract the product's module directory into a new repo **with history** using `git filter-repo --path packages/fdp-train/`. Nothing in the layout above has to be undone — which is the point of deferring the split.

## Local layout

No restructuring needed: the plain parent folder is fine (optionally replaced by a `fdpneo-workspace` clone). One practical note: the working copies currently live on an external volume; once `fdpneo-mcp` is pushed, all three repos are safe against disk loss.
