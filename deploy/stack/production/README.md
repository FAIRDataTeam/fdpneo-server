# Single-domain production deployment

One `docker compose up` serves the **client**, the **API + record IRIs**, and
**Keycloak** under **one HTTPS origin** behind Caddy, with certificates
obtained and renewed automatically (ACME). One origin means **no CORS to
configure** and an OIDC issuer that is consistent for the browser and the
server by construction — the two classic failure modes of ad-hoc deployments.

```text
https://fdp.example.org/           browsers → client (SPA)
                                   RDF conneg → server (record IRIs)
https://fdp.example.org/fdp-api/…  the API (OpenAPI, SPARQL, /users, …)
https://fdp.example.org/auth/…     Keycloak (production mode)
https://fdp.example.org/mcp        MCP bridge (optional, --profile mcp)
```

Unlike the [evaluation stack](../README.md) (localhost, HTTP, dev-credentialed
realm with demo users), this one is production-shaped: Keycloak runs in
`start` mode against its own Postgres database, the realm template contains
**no demo users** and env-parameterized secrets, brute-force protection is on,
and GraphDB/Postgres are never published.

## Prerequisites

- A DNS name (`PUBLIC_HOST`) resolving to this host from the internet.
- Ports **80 and 443** reachable (ACME issuance + serving).
- Docker Engine 24+ with the Compose plugin. Images are pulled from the public
  GHCR packages.

## Deploy

```bash
cd deploy/stack/production
cp .env.example .env        # set PUBLIC_HOST, ACME_EMAIL, and every CHANGE-ME
docker compose up -d
```

First boot takes a couple of minutes (Keycloak schema, GraphDB repo creation,
DB migrations, default profile). Then:

- UI: `https://PUBLIC_HOST/`
- API health: `https://PUBLIC_HOST/fdp-api/healthz`
- Keycloak admin console: `https://PUBLIC_HOST/auth` (the `KEYCLOAK_ADMIN`
  account from `.env`)

## First login and users

Sign in to the FDP as **`fdp-admin`** with `FDP_ADMIN_INITIAL_PASSWORD` — the
password is marked temporary and Keycloak forces a change at first login.
Create further users in the Keycloak admin console (realm `fdp`), assigning
the `admin` or `steward` realm role as needed, or through the FDP's own
user-management UI (backed by the `/fdp-api/users` facade, ADR-0013).

## How the routing works

The server owns the origin's IRI space (records dereference at the root — any
path except `/fdp-api`), while the SPA needs the same origin for its routes.
Caddy splits the two the way the FDP reference implementation does, on content
negotiation: requests preferring `text/html` (browsers) go to the client;
everything else (`text/turtle`, `application/ld+json`, `*/*` — machine
clients) dereferences straight to the server. `/fdp-api/*` and `/auth/*` are
plain path routes.

`BASE_URL` is `https://PUBLIC_HOST`, so minted IRIs, the OIDC issuer, and the
page origin all agree. The server reaches the public issuer URL through the
docker host (`extra_hosts: host-gateway`), so token validation works even
where the network can't hairpin its own public IP.

## MCP bridge (optional)

```bash
docker compose --profile mcp up -d
```

Serves the read-only agent bridge (ADR-0018) at `https://PUBLIC_HOST/mcp`.
Needs pull access to the (currently private) `ghcr.io/fairdatateam/fdp-mcp`
image, or a sibling `mcp` checkout to `--build`.

## Using nginx instead of Caddy

If the host already runs nginx (or you prefer it), keep the same one-origin
routing contract and replace only the edge:

1. **Publish the app containers on loopback ports** and drop the Caddy
   service:

   ```bash
   docker compose -f compose.yaml -f compose.nginx.yaml up -d --scale caddy=0
   ```

   [`compose.nginx.yaml`](compose.nginx.yaml) binds client/server/Keycloak
   (and mcp, with the profile) to `127.0.0.1:8090–8093` — reachable only
   through nginx.

2. **Obtain a certificate** (nginx doesn't do ACME itself):

   ```bash
   sudo certbot certonly --webroot -w /var/www/certbot -d fdp.example.org
   ```

3. **Install the site config** [`nginx/fdpneo.conf`](nginx/fdpneo.conf) —
   replace `fdp.example.org` with your `PUBLIC_HOST` and check the cert paths,
   then:

   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```

The config reproduces the Caddyfile exactly: `/fdp-api` → server, `/auth` →
Keycloak, `/mcp` → bridge, SPA static files → client, and for everything else
an `Accept`-header `map` sends `text/html` (browsers) to the client and all
other content negotiation (RDF, `*/*`) to the server. This is the same split
the FDP **reference implementation** performs inside its client image — at
port 80 of an RI deployment, a browser gets the UI while `curl` reaches the
server, and Swagger UI paths are forced to the server; here the split lives at
the edge so the stock images stay unchanged. Forwarded headers matter: Keycloak
(`KC_PROXY_HEADERS=xforwarded`) and the server's rate limiter
(`FDP_RATELIMIT_TRUST_FORWARDED_FOR`) both key on `X-Forwarded-*`.

## Operations

- **Updates:** `docker compose pull && docker compose up -d` (pin `IMAGE_TAG`
  to a release tag for reproducible deploys).
- **Backups:** the state lives in the `postgres-data` (operational state +
  Keycloak) and `graphdb-data` (the knowledge graph) volumes; `caddy-data`
  holds certificates (re-obtainable, but backing it up avoids ACME rate limits
  on rebuilds).
- **No public DNS yet?** Add `tls internal` inside the site block of the
  `Caddyfile` to smoke-test with a self-signed certificate (browsers will
  warn; the server's issuer fetch will fail certificate verification, so
  login stays broken until real TLS is in place — this mode is for checking
  routing only).
- **Hardening checklist:** [`docs/security/deployment-hardening.md`](../../../docs/security/deployment-hardening.md)
  — most items (TLS, headers, secret rotation, internal-only backing stores)
  are already satisfied by this stack's shape; the rest (offsite backups,
  monitoring, IdP MFA) are operational.
