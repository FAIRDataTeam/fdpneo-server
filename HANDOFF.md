# Claude Code handoff — fdp-server

This file is the operator's note. It tells Claude Code (and any human picking
this up) what's already in place and what to do first.

## What's already here

- **Architecture** in `docs/architecture/README.md` and eight ADRs in
  `docs/adr/`. These are the source of truth for design decisions.
- **`CLAUDE.md`** at the repo root with conventions, bounded-context rules,
  what to do and what not to do.
- **`pyproject.toml`** with the agreed dependency stack, ruff and pyright
  configured.
- **Module skeleton** under `src/fdp/` — eight packages (`identity`,
  `metadata`, `policy`, `access`, `data`, `metrics`, `storage`, `shared`)
  with `__init__.py` docstrings explaining each module's responsibilities,
  interface, and constraints.
- **`main.py`** and **`config.py`** stubs that compile and serve `/healthz`.
- **`cli.py`** stub with the four `fdp profile *` commands as no-ops.
- **First smoke test** in `tests/unit/test_app_factory.py`.
- **Dev stack** in `deploy/compose.yaml` (GraphDB + Postgres + Keycloak).
- **Default profile** scaffold in `profiles/default/` with `profile.yaml`.
- **`TASKS.md`** — prioritized implementation backlog.

## First-session checklist

In order:

1. **Install uv** if you don't have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
2. **Bring up the dev stack:** `docker compose -f deploy/compose.yaml up -d`
   and wait for healthchecks to pass (`docker compose -f deploy/compose.yaml ps`).
3. **Sync dependencies:** `uv sync --extra dev`.
4. **Confirm the baseline works:**
   - `uv run pytest tests/unit -v` — smoke test passes.
   - `uv run ruff check .` — clean.
   - `uv run pyright src/fdp` — clean.
   - `uv run fastapi dev src/fdp/main.py` — server starts; `curl localhost:8000/healthz` returns OK.
5. **Read** `CLAUDE.md`, then skim `docs/architecture/README.md` headings
   so you know where to look up details.
6. **Pick up Phase 0.1 from `TASKS.md`** — the shared kernel.

## Dev-stack credentials (Keycloak)

`deploy/keycloak/realm-fdp-dev.json` ships a starter realm imported on
container start. **Dev-only — credentials are publicly known.**

| user  | password | realm roles                     |
|-------|----------|---------------------------------|
| admin | admin    | `fdp-admin`, `fdp-steward`      |
| alice | alice    | `fdp-steward`                   |
| bob   | bob      | _(none — anonymous-equivalent)_ |

OIDC settings for the FDP server against this realm:

```env
FDP_OIDC_ISSUER=http://localhost:8080/realms/fdp-dev
FDP_OIDC_AUDIENCE=fdp
FDP_OIDC_ROLES_CLAIM=realm_access.roles
```

The realm includes an audience mapper that adds `fdp` to the access
token's `aud` claim so the FDP server accepts tokens issued to
`fdp-client`. The web client allows redirect URIs from
`http://localhost:{5173,3000,8000}` for Vite/CRA/the FDP server.

Replace this realm wholesale before any non-dev deployment.

## What is deliberately not here

- **No FDP-specific SHACL shapes beyond the bundled DCAT defaults.**
  `profiles/default/schemas/` ships repository/catalog/dataset/data-service/
  distribution shapes plus the meta-metadata schema. Communities replace
  these via their own deployment profile.

## Coordination with the client repo

API contract changes need a matching update in `fdp-client`. The contract is
the OpenAPI spec — when you change it, the client regenerates types from it
via `npm run generate-api`. If you ship a contract change, flag it in your PR
so the corresponding client PR can be sequenced.

## When stuck

The architecture document and ADRs cover the controversial decisions. If a
question is not answered there, prefer surfacing the ambiguity over guessing.
Document any new decision in a new ADR (next number is 0009).
