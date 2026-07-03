# Changelog

All notable changes to the FDPneo server are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic
versioning while pre-1.0 (minor versions may carry breaking API changes, called
out explicitly below).

## [Unreleased]

Changes staged for the next release (**v0.4.0**, which ships once Phase 17 is
complete). So far: write-path hardening, the alternative-identifiers semantics
change, and lifecycle/consistency fixes, plus a repaired and GraphDB-backed
integration suite. FAIR Signposting (tasks 17.3–17.5) is still to come before the
release is cut.

### Changed — LDP contract (⚠ behaviour changes)

- **Ambiguous request body → `400`.** `reconcile_identifiers` no longer has a
  "store as authored" fallback. A write body (`PUT`/`POST`) must address the
  record as `<>` / its canonical IRI, or contain exactly one typed IRI subject
  (which is rebound to the canonical IRI). Zero typed subjects, several, or a
  blank-node-only body is rejected with `400 fdp.ambiguous_subject` — the
  canonical-subject invariant is now unconditional. (ADR-0016 §1)
- **`POST` `Slug` collision → `409`.** `POST` to a container never overwrites: if
  the slug-derived member IRI already exists, the server responds
  `409 fdp.conflict` so the client picks another `Slug` or uses `PUT` + `If-Match`
  deliberately. (ADR-0016 §1)
- **Foreign brought-along identifier → `adms:identifier`, not `owl:sameAs`.** When
  a write rebinds a foreign primary subject to the canonical IRI, the original is
  now recorded as a structured alternative identifier — `dct:identifier` (literal)
  plus an `adms:identifier` node (`adms:Identifier` with `skos:notation`
  `^^xsd:anyURI`). The server no longer mints `owl:sameAs` (it cannot know the two
  resources are identical). Consumers that joined on the server-added `sameAs`
  must switch to `adms:identifier`/`dct:identifier`. Existing server-minted
  `sameAs` triples are **not** migrated (indistinguishable from client-authored
  ones); the resource SHACL shape gains an optional `adms:identifier`. (ADR-0017 §1)

Clients that relied on the previous lenient behaviours (silent "store as
authored", silent `POST` overwrite) must switch to the explicit forms above.

### Fixed

- **Managed resources can now be published via the shared `/state` endpoint.**
  Policies, licenses, schemas and resource definitions have canonical IRIs under
  the reserved namespace (`<base>/fdp-api/<segment>/<id>`), but the
  publication-state router — mounted under `/fdp-api` — resolved the
  prefix-stripped sub-path to a non-existent root IRI and returned `404`. A new
  `shared.graphs.state_record_iri` re-adds the reserved prefix for managed
  segments (and passes root-level LDP records straight through), so
  `POST /fdp-api/policies/{id}/state` (and licenses/schemas/RDs) now works.
- **Repository-root container listing.** `GET /fdp-api/page/{childPrefix}` at the
  repository root reported zero children even when catalogs existed: the handler
  queried the root subject with a trailing slash while records are stored under
  the slash-stripped IRI. The lookup is now normalized via `record_graph_uri`.

### Added

- **Full-stack deployment** (`deploy/stack/`): one `docker compose` command
  brings up client + server + GraphDB + Postgres + Keycloak with automatic
  bootstrapping. Keycloak health is probed on the management port (9000); the
  client is published on `5173` (the origin the bundled realm allows for OIDC
  redirects); `EXPOSE_API_DOCS` serves the Swagger/ReDoc UIs in the evaluation
  stack. The OpenAPI spec is always served at `/fdp-api/openapi.json`.
- **ADR-0016** (faithful backup/restore & instance migration) and **ADR-0017**
  (structured alternative identifiers + FAIR Signposting) recorded as *proposed*.
  Only ADR-0016 §1 (write-path hardening, above) is implemented in this release;
  `fdp dump`/`restore`/`import`, `adms:identifier`, and FAIR Signposting are
  planned for a subsequent release.

### Documentation

- README documents the one-command full-stack path, the always-on OpenAPI spec
  URL, and the `EXPOSE_API_DOCS` flag.

### Tests / infrastructure

- Shared **GraphDB testcontainer fixture** (`tests/integration/conftest.py`) for
  the end-to-end tests that exercise multi-graph SPARQL projection, which
  requires a store honouring named-graph isolation (Oxigraph does not).
- Repaired integration e2e tests that used pre-`/fdp-api` endpoint paths
  (`/search`, `/sparql`, `/{record}/state`) and stale profile-apply / schema
  expectations. Schemas are referenced by their storage IRI
  (`<base>/fdp-api/schemas/<slug>`); profile manifests still declare them by
  CURIE, resolved by the applier.

## [0.3.0]

Persistent identifiers (FAIR F1): identifier-base / serving-origin split, dual
identifier model, and `fdp pid` tooling. See ADR-0014.

## [0.2.0]

DCAT v3 modular schemas and full LDP conformance. See Phase 15.
