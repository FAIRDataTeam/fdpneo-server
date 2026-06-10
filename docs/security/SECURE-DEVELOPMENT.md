# Secure Development Guidance — fdp-server

Audience: any agent (or human) extending the FDP server. Read this before touching
authn/authz, the SPARQL or data paths, RDF parsing, outbound HTTP, or secrets.
It has two parts: **(A) history of the 2026-06-10 audit remediations** (all landed —
kept for context), then **(B) the standing rules** for all future work.
Reference: `../../SECURITY-AUDIT-2026-06-10.md`.

---

## Part A — History: 2026-06-10 audit remediations (all DONE)

All four Part-A findings were remediated on 2026-06-10. Recorded here so future work
doesn't re-litigate the approach; the live rules are in Part B.

### A-1 (High) — Data-provider SPARQL endpoint no longer bypasses the SPARQL safety gate ✅

**Was.** `/data/{id}/sparql` (`_stream_sparql`) forwarded raw SPARQL to
`adapter.query_stream` with no parsing, so `SERVICE` (federation/SSRF) and arbitrary
query forms were accepted anonymously.

**Shipped fix.** The `SERVICE`/`LOAD`/read-form safety gate was extracted into the shared
kernel — `fdp/shared/sparql_safety.py` (`reject_service`, `assert_query_safe`) — as the
single source of truth. `access/parser.py` imports those primitives; `data/router.py`'s
`_stream_sparql` calls `assert_query_safe(query)` before forwarding, which rejects
`SERVICE`, `LOAD`, and any non-read form. Data-graph scoping
(`default_graph_uris=(data_graph,)`) is unchanged.

**Note on the original plan.** The first draft of this section said "call
`fdp.access.parser.parse()` from the data router." That was **not** taken: `data/` may
only import `shared, policy, storage` — never `access/` (CLAUDE.md module boundary). The
shared-gate approach satisfies the same intent without crossing the boundary. *Federation
-off is a structural invariant shared by both endpoints; authorization stays per-endpoint
and ODRL-driven (access rewriter vs. the data provider's anonymous Offer check).* See
standing rule 1. Tests: `tests/unit/shared/test_sparql_safety.py`,
`tests/unit/data/test_router.py`.

### A-2 (Medium) — Stream-mode download proxy locked down (SSRF) ✅

**Shipped fix.** `fdp/shared/ssrf.py::assert_public_url` enforces an `http`/`https`
scheme allowlist and resolves the host, rejecting loopback / link-local /
RFC-1918 / ULA / reserved / IPv4-mapped-IPv6 targets. `_stream_upstream`
(`src/fdp/data/router.py`) validates the entry URL up front (clean `502`) and follows
redirects **manually** (`follow_redirects=False`), re-validating every hop.
The operator knob shipped is `FDP_DATA_ALLOWED_DOWNLOAD_HOSTS` (an optional egress
allow-list; empty = any *public* host) — note the name differs from the originally
proposed `FDP_DATA_PROXY_ALLOW_PRIVATE_HOSTS`, and private hosts are **always** blocked
rather than being unlockable by a flag. Tests: `tests/unit/shared/test_ssrf.py` and the
redirect/entry-block tests in `tests/unit/data/test_router.py`.

### A-3 (Low) — Download-proxy resource limits tightened ✅

`DataSettings.proxy_max_bytes` default lowered 1 GiB → 256 MiB; added
`proxy_max_seconds` (60 s total wall-clock deadline, enforced in the stream loop) and
`proxy_max_redirects` (3). (`src/fdp/config.py`, `src/fdp/data/router.py`.)

### A-4 (Low) — Subject PII in logs (F-09 / R-10) ✅

Applied at the logging layer, not per-call: a structlog processor in
`fdp/shared/logging.py` (`pseudonymize_subject` + `_make_subject_pseudonymizer`)
replaces `subject`/`owner_subject` on **every** log line with a stable salted SHA-256
pseudonym (`subj_<16hex>`). Salt from `FDP_LOG_SUBJECT_SALT` (else a per-process random
salt — still pseudonymized, just not stable across restarts). The identified trail stays
in the audit log / audit graph, which don't flow through structlog. Tests:
`tests/unit/shared/test_logging.py`.

---

## Part B — Standing secure-development rules

These are the rules that keep the above classes of bug from recurring. Treat them as
review gates: a PR that violates one should not merge.

### 1. Every client SPARQL string passes the shared safety gate before the adapter
Any endpoint that accepts a SPARQL string from a client **must** run it through the
shared federation/SSRF gate before it reaches `adapter.query` / `query_stream` /
`update`. That gate — `SERVICE`/`LOAD` rejection and read-form classification — lives in
`fdp/shared/sparql_safety.py` (`reject_service`, `assert_query_safe`) as the single
source of truth, so endpoints in **any** bounded context can enforce it without crossing
module boundaries (`data/` may not import `access/`). Which entry point you call depends
on the context:

- The access `/sparql` endpoint uses `fdp.access.parser.parse()`, which *additionally*
  extracts graph targets and authorizes via the rewriter/PDP — it reuses the shared
  `reject_service` primitive internally.
- A read-only surface that does its own authorization (the data provider's
  `/data/{id}/sparql`, which authorizes the distribution's anonymous Offer and scopes to
  the data graph) calls `fdp.shared.sparql_safety.assert_query_safe(query)`.

Never call the adapter with a client-supplied string that hasn't been through one of
these. Bypassing the gate silently re-opens federation SSRF (this is exactly how N-01
happened). If you add a new query surface, wire it to the shared gate; do not reimplement
a "lighter" version, and do not import `access/` from another context to get at the
parser — promote what you need into the shared kernel instead.

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
