# Full-stack FDP deployment

One `docker compose` command brings up the **client**, the **server**, and the
backing services (**GraphDB**, **Postgres**, **Keycloak**). The GraphDB `fdp`
repository, the database schema, and the default metadata profile are all
bootstrapped automatically on first boot.

## Prerequisites

- Docker Engine 24+ with the Compose plugin.
- Nothing else for the default path: the `ghcr.io/fairdatateam/fdpneo-server`
  and `ghcr.io/fairdatateam/fdpneo-client` images are **public** — no GHCR
  login needed. To build from source instead, check out both repos as siblings
  (`fdpneo/server`, `fdpneo/client`).

## Quick start (pull pre-built images)

```bash
cd server
cp deploy/stack/.env.example deploy/stack/.env      # edit PUBLIC_HOST / secrets
docker compose -f deploy/stack/compose.yaml --env-file deploy/stack/.env up -d
```

Then open the UI at **http://localhost:5173**. The API is at
**http://localhost:8000** (`/fdp-api/healthz`, `/fdp-api/config`), and Keycloak at
**http://localhost:8081**.

The OpenAPI spec is always served at `/fdp-api/openapi.json`. The interactive docs
(Swagger `/fdp-api/docs`, ReDoc `/fdp-api/redoc`) are on by default in this stack
via `EXPOSE_API_DOCS=true`; they are otherwise served only when `ENVIRONMENT=development`.

Log in with a bundled realm user (e.g. `alice` / `alice`, admin `admin` / `admin`
— see `deploy/keycloak/realm-fdp-dev.json`).

### Agent access (MCP) — opt-in

An [`fdp-mcp`](../../../mcp) Model Context Protocol bridge (ADR-0018) can run
alongside the stack at **http://localhost:8002/mcp** (liveness at `/healthz`),
pointed at this FDP. It exposes a read-only tool surface over the **public**
FDP API only. The service sits behind the `mcp` compose profile because its
image is not publicly pullable (yet) — a default `up` skips it. Enable it with:

```bash
docker compose -f deploy/stack/compose.yaml --env-file deploy/stack/.env \
  --profile mcp up -d
```

This needs pull access to `ghcr.io/fairdatateam/fdp-mcp` (`docker login ghcr.io`
with a token that can read the package), or `--build` with the private `mcp`
repo checked out as a sibling (`fdpneo/mcp`).

Point any MCP client (Claude Desktop, an IDE agent, the MCP inspector) at the
URL above — see [`../../../mcp/docs/agent-quickstart.md`](../../../mcp/docs/agent-quickstart.md).
Smoke-check it after `up`:

```bash
curl -fsS http://localhost:8002/healthz            # bridge liveness
# then run an MCP `initialize` + `fdp_info` from your MCP client to confirm it
# reaches the FDP (fdp_info reports the root title and available tools).
```

## Writing metadata via the API

Records created through the LDP API (`PUT`/`POST` of Turtle) start in
publication state **DRAFT**: the create returns 201 (with an
`FDP-Metadata-State` header saying so), but the record is 404 to everyone
except its owner and admins until published. Either publish afterwards —

```bash
curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"to":"PUBLISHED"}' https://…/fdp-api/<path>/state
```

— or create it visible in one step with `Prefer: publication-state=PUBLISHED`
on the creating request. Updates (`PUT`/`PATCH`/`DELETE` of an existing
record) require `If-Match` with the record's current ETag (optimistic
concurrency); `PATCH` takes `application/sparql-update`.

## Build from source instead of pulling

```bash
docker compose -f deploy/stack/compose.yaml --env-file deploy/stack/.env up -d --build
```

The `server` build context is this repo root; the `client` context is the sibling
`../client` repo.

## How it fits together

| Service        | Exposed       | Role |
|----------------|---------------|------|
| `client`       | `:8080` → 80  | Vue SPA (nginx). API URL injected at runtime into `/config.js`. |
| `server`       | `:8000`       | FastAPI API + resource IRIs (`BASE_URL`). |
| `mcp`          | `:8002` → 8000| MCP bridge (ADR-0018): read-only agent access at `/mcp`. Uses only the public FDP surface. Opt-in via `--profile mcp`. |
| `keycloak`     | `:8081` → 8080| OIDC login (browser-facing). |
| `graphdb`      | internal      | Triple store. `graphdb-init` creates the `fdp` repo. |
| `postgres`     | internal      | Operational state (metrics, auth cache, jobs). |

Browser → client (8080) for the UI, → server (8000) for the API (CORS-allowed),
→ Keycloak (8081) for login. The server reaches GraphDB/Postgres/Keycloak over the
internal network; it resolves `PUBLIC_HOST` to the docker host (`extra_hosts:
host-gateway`) so the OIDC issuer URL is identical for browser and server.

**Bootstrapping** (automatic, idempotent):
1. `graphdb-init` POSTs `deploy/graphdb/fdp-repo-config.ttl` to GraphDB if the
   `fdp` repo is missing (no inference, named-graph/context index on).
2. The server entrypoint runs `fdp db migrate`.
3. The app lifespan auto-applies the bundled default profile (`FDP_PROFILE_AUTO_APPLY=true`)
   and runs the named-graph isolation self-test.

## Deploying on another host

Set `PUBLIC_HOST` in `.env` to a hostname/IP that resolves **the same** from the
browser and from inside the containers (a LAN IP or real DNS name) — the OIDC
issuer must match both sides. Adjust `CLIENT_PORT`/`SERVER_PORT`/`KEYCLOAK_PORT`
if those host ports are taken.

> **⚠ Login only works over plain HTTP at the exact origin
> `http://<PUBLIC_HOST>:5173`.** The bundled dev realm allows OIDC redirect
> URIs only at `http://localhost:5173` (and :3000/:8000) — on any other origin,
> and on **any HTTPS deployment**, Keycloak rejects the login redirect and the
> browser blocks the mixed/mismatched origins (CORS). Don't fight this stack
> into a public deployment: use the **single-domain production compose** in
> [`production/`](production/README.md) instead — one HTTPS origin for client,
> API, and Keycloak, which eliminates the CORS configuration entirely.

## ⚠ Before production — this stack is dev-credentialed

The bundled realm runs Keycloak in `start-dev` mode with **development
credentials** (`admin`/`admin`, `fdp-server-dev-secret`, demo users). It is fine
for evaluation and internal demos, **not** for production. Before a real deploy:

- Rotate every secret in `.env` and the Keycloak realm (admin, DB, the
  `fdp-server` client secret), and run Keycloak in production mode behind TLS.
- Put a **TLS-terminating reverse proxy** in front (HSTS, security headers). See
  `deploy/caddy/Caddyfile` and `docs/security/deployment-hardening.md`. With TLS,
  serve client/API/Keycloak under stable HTTPS origins and update `PUBLIC_HOST`,
  `BASE_URL`, `FDP_CORS_ALLOW_ORIGINS`, and the Keycloak hostname accordingly.
- Restrict GraphDB/Postgres to the private network (they are unpublished here)
  and enable encryption at rest.

See `docs/security/audit-2026-06-07.md` for the full hardening checklist.
