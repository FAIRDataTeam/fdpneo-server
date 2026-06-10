# Secure Development Guidance — fdp-server

Audience: any agent (or human) extending the FDP server. Read this before touching
authn/authz, the SPARQL or data paths, RDF parsing, outbound HTTP, or secrets.
It has two parts: **(A) fix the findings from the 2026-06-10 audit**, then **(B) the
standing rules** for all future work. Reference: `../../SECURITY-AUDIT-2026-06-10.md`.

---

## Part A — Remediations to implement now

### A-1 (High) — Route the data-provider SPARQL endpoint through the access parser

**Problem.** `/data/{id}/sparql` (`src/fdp/data/router.py`, `sparql_get`/`sparql_post`
→ `_stream_sparql`) forwards raw SPARQL to `adapter.query_stream` with no parsing, so
`SERVICE` (federation/SSRF) and arbitrary query forms are accepted anonymously. The
main `/sparql` router is protected by `fdp.access.parser.parse()`; the data path is not.

**Fix.**
1. In `_stream_sparql` (or before it), call `parsed = fdp.access.parser.parse(query)`.
2. Reject anything that is not a `ParsedRead` (a `ParsedUpdate` on a read-only data
   endpoint is `400`/`405`). `parse()` already rejects `SERVICE` and `LOAD` for you.
3. Keep the existing data-graph scoping (`default_graph_uris=(data_graph,)`). Do **not**
   honour user `FROM`/`FROM NAMED` that would widen the dataset beyond the distribution's
   data graph — if `parsed.has_dataset_clause` names anything other than the data graph,
   return `400`.
4. Gate the response media type the same way `/sparql` does via
   `select_result_media_type(parsed.form, accept)` so an unsupported `Accept` returns
   `406`, not a raw store error.

**Acceptance.** A unit test that a `SERVICE`-bearing query to `/data/{id}/sparql`
returns `400` *before* any adapter call (monkeypatch `adapter.query_stream` to assert it
is never reached); a live POST of a `SERVICE` query to a published open distribution
triggers **no** outbound callback. Add a regression test so the guard can't be removed.

### A-2 (Medium) — Lock down the stream-mode download proxy (SSRF)

**Problem.** `_stream_upstream` (`src/fdp/data/router.py:226`) fetches the
steward-supplied `dcat:downloadURL` server-side with `follow_redirects=True`, no scheme
check, no host allowlist — SSRF when `download_mode="stream"`.

**Fix.**
- Before fetching, validate the URL: scheme in `{"http","https"}` only; resolve the host
  and **reject** loopback, link-local (`169.254.0.0/16`, `fe80::/10`), and RFC-1918 /
  ULA private ranges unless an explicit operator allowlist permits them. Add a setting
  `FDP_DATA_PROXY_ALLOW_PRIVATE_HOSTS` (default `false`).
- Cap redirects (`max_redirects` small, e.g. 3) and **re-validate the target host after
  each redirect** — an allowlisted host can 302 inward. Easiest robust approach: disable
  automatic redirect-following and handle redirects manually with re-validation, or use a
  transport that pins the resolved IP.
- Keep `proxy_max_bytes` enforced (already present) but lower the default (A-4).

**Acceptance.** Unit tests: `file://`, `http://169.254.169.254/...`, and a redirect from
a public host to `http://127.0.0.1/...` are all rejected with `400`/`502` before bytes are
streamed; a normal public URL still streams.

### A-3 (Low) — Tighten download-proxy resource limits

Lower `DataSettings.proxy_max_bytes` default to something defensible (e.g. 256 MiB) and
document raising it per deployment; give the stream read its own bounded timeout rather
than relying on the shared 5 s app client. (`src/fdp/config.py`, `src/fdp/data/router.py`.)

### A-4 (Low, ongoing) — Subject PII in logs (F-09 / R-10)

`log.info(..., subject=ctx.subject)` still appears on hot paths (`access/router.py:116`,
`identity/api_keys.py`, others). Define a policy: keep the identified trail in the audit
log / audit graph; in application logs hash the subject (e.g. a keyed `sha256` truncated)
or drop it on non-audit paths. Apply consistently via the logging layer, not per-call.

---

## Part B — Standing secure-development rules

These are the rules that keep the above classes of bug from recurring. Treat them as
review gates: a PR that violates one should not merge.

### 1. The access parser is the only door to the triple store for user queries
Any endpoint that accepts a SPARQL string from a client **must** pass it through
`fdp.access.parser.parse()` and authorize via the rewriter/PDP before it reaches the
adapter. Never call `adapter.query` / `query_stream` / `update` with a
client-supplied string that hasn't been parsed. `SERVICE` and `LOAD` rejection,
form classification, and graph-target extraction all live in the parser — bypassing it
silently re-opens federation SSRF (this is exactly how N-01 happened). If you add a new
query surface, wire it to the same pipeline; do not reimplement a "lighter" version.

### 2. Fail closed, and keep the PEP between the user and the data
Authorization decisions default to **deny**. The read rewriter projects only authorized
graphs; the update path authorizes every explicit target. When a new capability can't be
expressed in that model, deny it rather than special-casing around the PDP. The
named-graph isolation gate (`multigraph_safe_provider`) must stay fail-closed — never
"optimize" it to assume the store is conformant.

### 3. Treat every outbound request as SSRF-prone
The server makes outbound HTTP in three places today: JWKS/OIDC discovery (trusted
issuer, fine), the download proxy, and the (now-guarded) JSON-LD parser. Any **new**
outbound fetch whose URL is influenced by user/metadata input must: allow only
`http`/`https`, block private/loopback/link-local unless explicitly configured, bound
redirects with per-hop re-validation, and bound size + time. RDF parsing counts —
never let a parser dereference remote documents (`@context`, `LOAD`, XML external
entities). Keep RDFLib pinned `>=7` (RDF/XML XXE safety) with the existing pin comment.

### 4. JWT/identity invariants — don't loosen them
Keep the asymmetric-only algorithm allowlist (`_ALLOWED_ALGS`), required claims
(`exp`, `iat`, `iss`, `aud`, `sub`), and the issuer/audience checks in
`identity/middleware.py`. Never add `HS*` to the allowlist (alg-confusion). Never accept
an `alg`/`kid` from the token to *select trust* beyond looking up the JWKS key. API keys
stay high-entropy CSPRNG + hashed-at-rest, shown once, owner/admin-revocable.

### 5. Validate and constrain all path/route inputs
Anything interpolated into an upstream URL (Keycloak admin, store endpoints) must be
format-validated first (see `_require_uuid` for `/users/{id}`). Prefer typed/escaped
clients over string concatenation.

### 6. Errors return the envelope; secrets never leave the process
All errors go through the FDP error envelope / `CatchAllExceptionMiddleware` — no
stack traces, internal IRIs, or store errors in responses. Secrets come from settings
(`SecretStr`), never literals; `.env` stays gitignored; only `.env.example` is tracked.
Don't log tokens, keys, passwords, or PHI.

### 7. Security middleware order is load-bearing
The stack (outermost→innermost: SecurityHeaders → CORS → CatchAll → RateLimit/BodySize →
RequestObservation → Authentication) is deliberate (`main.py`). If you add middleware,
reason about where it sits relative to auth and CORS, and don't move CORS off the
outside or auth off the inside. CORS `allow_credentials=true` must never pair with a
wildcard origin — keep origins explicit.

### 8. Tests are part of the control
Every security fix ships with a unit test that fails if the guard is removed (a
"monkeypatch the dangerous call and assert it's never reached" test is the strongest
form). The `pip-audit` + SBOM CI gate (`security-scan.yml`) must stay green; triage new
advisories with a documented rationale, don't blanket-ignore.

### 9. Before merging anything that touches auth, the access path, RDF parsing, or outbound HTTP
Run the repo's own checklist: `ruff`, `pyright`, the unit + integration tests, and a
manual re-read against rules 1–3 above. For changes to the SPARQL/data path, add a
`SERVICE`/`LOAD`/private-host probe to the test suite.

---

*Keep this file current: when a new finding is remediated, move it from Part A to a short
"history" note, and add any new standing rule it taught us to Part B.*
