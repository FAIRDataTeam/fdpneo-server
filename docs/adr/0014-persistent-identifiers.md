# ADR-0014: Persistent identifiers — base/serving split, dual model, W3ID

**Status:** Accepted
**Date:** 2026-06-15

## Context

FAIR principle **F1** requires (meta)data to carry *globally unique and
persistent* identifiers (PIDs). Through v0.2 the server derived a record's
identity from the **host the request arrived on**: `_resource_iri()` returned
`request.url`, and although `BASE_URL` claimed to "mint resource URIs", the LDP
layer ignored it. Two consequences:

- **Not persistent.** A record's IRI was tied to the deployment host; moving the
  server (or fronting it with a new domain) changed every identifier.
- **No bring-your-own-identifier.** A client `PUT`ting a record with an existing
  identifier had that identifier silently ignored — the server's host-derived
  IRI won, and the supplied one was neither honored nor preserved.

There was also no resolution story (no redirect/dereference handling) and no way
to automate registration with a PID redirector.

The dominant low-friction PID mechanisms for a self-hosted service are HTTP
**redirectors**: **W3ID** (https://w3id.org) and **PURL**. The sysadmin registers
a prefix whose redirect rules point at the deployment; the identifier
`https://w3id.org/<prefix>/...` then resolves to the FDP wherever it runs. The
Handle System / DOIs are heavier (registrar, fees) and out of scope for v0.3.0,
though the model below generalizes to them.

Local/dev deployments must stay friction-free: a `localhost` identifier is fine
when the server is never internet-reachable.

## Decision

### 1. Split the identifier base from the serving origin

Introduce two distinct concepts (previously conflated):

- **`identifier_base`** — the persistent PID namespace records are minted under
  (e.g. `https://w3id.org/myfdp`). **Immutable** across deployment moves.
- **`base_url`** — the serving origin where requests actually arrive after a
  redirect (e.g. `https://fdp.example.org`). May change when the deployment moves.

`identifier_base` defaults to `base_url` when unset, so a local deployment keeps
minting under its serving URL and **nothing changes for localhost**.

All record, schema, policy, license, and resource-definition IRIs are minted
under `identifier_base` (via `IRIExpander` and the `shared/graphs.py` helpers).

### 2. Canonicalize inbound requests in-server

A request arrives on a serving origin (post-redirect) but must resolve the record
whose canonical IRI is rooted at `identifier_base`. `shared/identifiers.py`
provides the single mapping — `canonicalize(request_url, identifier_base,
serving_origins)` — and the LDP router maps every request through it
(`_canonical_iri`) before any storage or lookup. The resource-definition registry
is initialized with `identifier_base`, so container/shape resolution matches the
canonical IRIs. In dev (`identifier_base == base_url`) the mapping is the identity.

The alternative — relying on the reverse proxy alone to rewrite the `Host` header
so `request.url` happens to equal the canonical IRI — was rejected: it is fragile
(any direct-host access mints wrong, permanent IRIs) and pushes a correctness
invariant into per-deployment proxy config.

### 3. Dual identifier model on write

The canonical, dereferenceable subject is **always** the canonical IRI (so F1 and
resolution are guaranteed). On top of that (`metadata/identifiers.py`,
`reconcile_identifiers`):

- **Within-base** — a client "brings its own identifier" by choosing the `PUT`
  path or `POST` `Slug`; that becomes the canonical IRI. No special handling.
- **Foreign primary subject** — if the submitted graph's single typed primary
  subject is an absolute IRI *not* under `identifier_base` (a DOI, ARK, another
  org's IRI), its triples are rebound to the canonical IRI and the original is
  recorded as `owl:sameAs`. The FDP cannot make a foreign identifier resolve to
  itself, so it must not be the dereferenceable subject — but it is preserved as
  a cross-reference.
- **Explicit cross-references** — any `dct:identifier` / `owl:sameAs` /
  `skos:exactMatch` the client attaches to `<>` are preserved. The resource SHACL
  shape gained these as optional, additive properties (lenient, per v0.2 posture).

Honoring *any* supplied IRI as the subject (including foreign ones) was rejected:
it breaks the resolution guarantee. This dual model also **enables** a future
faithful backup/restore (canonical IRIs + honor-supplied-within-base), which is
otherwise out of scope for v0.3.0.

### 4. W3ID automation, reusable + verifiable

`fdp pid` (the `metadata/pid/` package) provides:

- `w3id-config` — generate the redirect `.htaccess` (+ README). Pure, no secrets.
- `w3id-pr` — fork `perma-id/w3id.org` and open/update the PR via the GitHub REST
  API. **Opt-in** (needs `FDP_PID_GITHUB_TOKEN`) and **reusable**: re-running
  after a deployment move updates the redirect *target* on the existing PR
  without touching the identifier base. Every request host is checked against an
  allow-list, mirroring the schema-sync outbound posture.
- `verify` — a **resolution test**: request the canonical `identifier_base` IRIs
  and confirm they redirect to, and successfully resolve on, the serving origin.
- `rebase` — a **one-time** adoption migration that re-keys existing graphs (and
  rewrites cross-record IRIs) from an old base to `identifier_base`, for a
  deployment that was already bootstrapped under `base_url`. After adoption the
  identifier base never changes again.

## Consequences

- **Persistence achieved.** Identifiers survive a deployment/host move; only the
  redirect target changes (one PR via `w3id-pr`), never the IRIs.
- **`/config` exposes both bases.** `fdp_url` is now the canonical
  `identifier_base`; a new `serving_url` carries `base_url`. The client renders
  PIDs against `fdp_url` but issues API calls against `serving_url` (they coincide
  in dev). This is the one client-visible contract change in v0.3.0.
- **New opt-in outbound integration.** The GitHub PR automation is the first
  git/GitHub call in the codebase; it is token-gated and allow-listed, off by
  default.
- **Adoption requires one migration.** Existing public deployments that adopt a
  PID base run `fdp pid rebase` once (and re-point any cached references).
  Localhost/dev deployments are unaffected (bases coincide).
- **Generalizes.** PURL is identical (another redirector); Handle/DOI can be
  added later behind the same base/serving split without re-architecting.

## References

- FAIR F1; DCAT 3; W3ID (https://w3id.org), PURL.
- Builds on [ADR-0007](0007-one-graph-per-record.md) (graph-per-record) and
  [ADR-0009](0009-runtime-resource-definitions.md) (IRI construction).
- Code: `config.py` (`identifier_base`, `PIDSettings`), `shared/identifiers.py`,
  `metadata/identifiers.py`, `metadata/ldp/router.py`, `metadata/pid/`,
  `identity/bootstrap.py`.
