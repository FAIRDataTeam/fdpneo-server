# Deployment hardening runbook (security audit R-04)

Pre-production checklist for a high-assurance (hospital / HIPAA / GDPR) FDP-neo
deployment. Pairs with the audit findings in [`audit-2026-06-07.md`](audit-2026-06-07.md).
The application-level controls (R-01/02/03/05) ship enabled; this document covers
the **perimeter and operational** controls (R-04) that live outside the app.

Reference artifacts in this repo: [`deploy/caddy/Caddyfile`](../../deploy/caddy/Caddyfile)
(TLS-terminating reverse proxy) and [`deploy/.env.production.example`](../../deploy/.env.production.example)
(hardened settings template).

## 0. Topology

```
            Internet / hospital network
                        │  HTTPS (443)
                ┌───────▼────────┐
                │  Reverse proxy │  TLS termination, HSTS, security headers,
                │   (Caddy/nginx │  Server-banner strip, body cap, rate limit
                │    /Traefik)   │
                └───────┬────────┘  ── private network ──
        ┌───────────────┼───────────────┬───────────────┐
   ┌────▼────┐     ┌────▼────┐      ┌────▼────┐     ┌─────▼─────┐
   │ fdp-app │     │ Postgres│      │ GraphDB │     │ Keycloak  │
   │ :8000   │     │  (TLS)  │      │ (Fuseki)│     │  (IdP)    │
   └─────────┘     └─────────┘      └─────────┘     └───────────┘
```
The app and the backing stores are **never** exposed publicly — only the proxy
binds a public port. App ↔ store ↔ DB traffic stays on a private network.

## 1. TLS / transport (F-04)

- [ ] Terminate TLS at the proxy; redirect HTTP→HTTPS. Modern ciphers only (TLS 1.2+).
- [ ] **HSTS** enabled (`max-age` ≥ 1 year, `includeSubDomains`; add `preload` once stable).
      The app already emits HSTS; the proxy is authoritative.
- [ ] `BASE_URL` is the **https://** origin (it mints resource IRIs).
- [ ] TLS (or mTLS) on app→Postgres and app→GraphDB links (e.g. `?ssl=require`).
- [ ] Internal CA or ACME certs; automate renewal.

## 2. Secrets management (F-07)

- [ ] **Rotate every bundled dev credential** — none belong in any non-dev env:
      `KEYCLOAK_ADMIN_PASSWORD: admin`, the realm `admin/alice/bob` passwords, and
      `fdp-server-dev-secret` (the `/users` service-account client). The files
      `deploy/compose.yaml` and `deploy/keycloak/realm-fdp-dev.json` are **dev-only**.
- [ ] Source secrets from a manager (Vault / sealed-secrets / cloud secret store),
      injected as env or mounted files — never committed, never in container images.
- [ ] `.env*` stays git-ignored (verified); use `deploy/.env.production.example` as the shape.
- [ ] Least-privilege DB role; the IdP service account keeps only the four
      `realm-management` roles it needs (ADR-0013).

## 3. HTTP security headers + banner (F-05)

- [ ] App `SecurityHeadersMiddleware` is active (HSTS, `nosniff`, `DENY`, `no-referrer`,
      COOP, strict CSP). The proxy re-asserts them and **strips `Server`/`X-Powered-By`**
      (the app can't remove uvicorn's `Server` banner itself).
- [ ] If not behind a proxy, run uvicorn with `--no-server-header`.

## 4. Interactive docs (R-04)

- [ ] `ENVIRONMENT=production` (or `staging`) ⇒ Swagger `/docs` and ReDoc `/redoc` are
      **off** by default (the "try it out" surface). `/openapi.json` stays for tooling.
      Set `FDP_EXPOSE_API_DOCS=true` only if you deliberately want them.

## 5. Rate limiting + quotas (R-02)

- [ ] Authoritative limiting at the proxy/WAF (per-IP/per-route). The app limiter is
      per-instance defense-in-depth.
- [ ] Set `FDP_RATELIMIT_TRUST_FORWARDED_FOR=true` **only** behind a proxy that
      sanitizes `X-Forwarded-For` (else clients spoof the key).
- [ ] Tune `FDP_RATELIMIT_*` and `FDP_TRIPLESTORE_QUERY_TIMEOUT_SECONDS` to the workload;
      keep the proxy `request_body` cap aligned with `FDP_RATELIMIT_MAX_BODY_BYTES`.

## 6. Triple store (F-03 / R-03)

- [ ] Use a **conformant** store for `/sparql`: **GraphDB or Fuseki**. Do **not** use
      Oxigraph for multi-graph reads.
- [ ] Keep `FDP_TRIPLESTORE_VERIFY_NAMED_GRAPH_ISOLATION=true`; treat a failed
      `named_graph_isolation_*` log line at boot as a **release blocker** (multi-graph
      reads fail closed, but you want a conformant store, not a degraded one).

## 7. Data protection at rest (HIPAA/GDPR)

- [ ] Encryption-at-rest for Postgres **and** the triple store volumes (PHI lives in graphs).
- [ ] Encrypted, access-controlled backups; tested restore/DR runbook.
- [ ] Metrics stay anonymous by design (ADR-0002) — do not add identifying columns.

## 8. Identity / IdP

- [ ] Keycloak (or the chosen IdP) hardened: brute-force protection on (realm has it),
      MFA for admins, short token lifetimes (realm `accessTokenLifespan` ≈ 300s),
      account console behind the proxy.
- [ ] Confirm the JWT carries FDP roles in `realm_access.roles` (the PDP reads it).

## 9. Runtime / container hygiene

- [ ] Run the app as a **non-root** user, read-only root FS where possible, no extra caps.
- [ ] Pin images by digest; scan images + dependencies (`pip-audit`/`osv`, `npm audit`,
      SBOM) in CI and gate on high-severity advisories (R-12).
- [ ] Network policies: only proxy→app, app→{Postgres,GraphDB,IdP} egress; deny the rest.
- [ ] Resource limits (CPU/mem) to bound DoS blast radius.

## 10. Logging / audit / monitoring

- [ ] Ship structured logs to a central, access-controlled, retention-bounded store.
      Logs contain the OIDC `subject` (PII, R-09) — restrict access; consider hashing
      it on non-audit paths.
- [ ] Alert on `named_graph_isolation_unsafe_*`, `system_default_offer_missing`,
      `offer_unresolved_default_deny`, repeated `429`/`401`, and `5xx` spikes.
- [ ] `/healthz` (liveness) and `/readyz` (deps) wired to the orchestrator.

## 11. Pre-go-live gate

- [ ] Third-party penetration test against the staged HTTPS deployment.
- [ ] Re-run the audit probes (JSON-LD SSRF, LOAD, rate-limit, headers, TLS) against staging.
- [ ] BAAs / DPAs in place with hosting and IdP providers.
- [ ] Access-review + key-rotation schedule documented and owned.

---

*Open items tracked in the audit remediation plan: R-04 (this doc), plus Phase B
(R-06 client CSP/token store, R-07, R-08) and Phase C (R-09–R-12).*
