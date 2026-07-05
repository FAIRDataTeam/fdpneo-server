# Agent-ready (MCP tool-surface) conformance

**Status:** v0.5.0 (Phase 19)
**Last updated:** 2026-07-05
**Contract:** [`fdp-mcp` tool surface](../../../mcp/docs/mcp-tool-surface.md) (read-only v1)
**Authoritative ADR:** [ADR-0018](../adr/0018-agent-consumption-mcp-server.md) §5
**Gap report:** [`fdp-mcp` API gaps](../../../mcp/docs/fdp-api-gaps.md)

The [`fdp-mcp`](../../../mcp) bridge consumes **only the public FDP surface** and
works against any spec-compliant FDP. This note records which parts of its
tool-surface contract **this server's** public surface backs — the reference for
"how agent-ready is FDPneo." The bridge itself is a separate repo; this is the
server side of the contract. Kept in sync as gaps close.

## Tool support

| Tool | Contract tier | FDPneo public surface | Status |
|---|---|---|---|
| `fdp_info` | required | Root record `GET` (root title, `rdf:type`, `ldp:contains` kinds) | ✅ full |
| `list_records` | required | LDP containment (`ldp:contains` + typed member relations) | ✅ full |
| `get_record` | required | Content-negotiated record `GET` + FAIR Signposting `Link` header | ✅ full |
| `get_access_conditions` | required | `dct:license` / `dct:rights` (ODRL Offer) on the record | ✅ full |
| `list_data_services` | required | `dcat:DataService` records via containment | ✅ full |
| `get_schema` | optional | `GET /fdp-api/schemas`, `/fdp-api/schemas/{id}[?composed=true]`, `/fdp-api/{prefix}/spec`, per-record `ldp:constrainedBy` | ✅ full (see G-06) |
| `search` | optional | `POST /fdp-api/search` (enabled by default) | ✅ full |
| `sparql_query` | optional | `POST` / `GET /fdp-api/sparql` (SPARQL 1.1 Protocol) | ✅ full |

FDPneo backs **every** required and optional tool in the v1 surface. The gaps
below are about cross-implementation *interoperability* and *discoverability*, not
FDPneo capability holes.

## Gap triage (ADR-0018 §4)

Each entry in the bridge's [gap report](../../../mcp/docs/fdp-api-gaps.md) is
triaged here (server `TASKS.md` Phase 19). Summary:

| Gap | Capability | FDPneo position | Triage |
|---|---|---|---|
| [G-01](../../../mcp/docs/fdp-api-gaps.md#g-01--no-search-endpoint-on-the-java-reference-implementation) | `search-api` | FDPneo exposes `/fdp-api/search`. The gap is the Java RI lacking search; search is optional by design. | Won't-fix (interop; spec-level concern) |
| [G-02](../../../mcp/docs/fdp-api-gaps.md#g-02--shape-discovery-is-under-specified-across-implementations) | `shapes` | FDPneo offers `/fdp-api/schemas`, `?composed=true`, `/{prefix}/spec`, and per-record `ldp:constrainedBy`. Under-specification is cross-impl. | Won't-fix on server; spec-level |
| [G-03](../../../mcp/docs/fdp-api-gaps.md#g-03--sparql-endpoint-presenceenablement-is-not-guaranteed) | `sparql` | FDPneo exposes `/fdp-api/sparql`. Enablement guarantees are per-deployment/spec. | Won't-fix (bridge probes + degrades) |
| [G-04](../../../mcp/docs/fdp-api-gaps.md#g-04--fair-signposting-relations-absent-on-the-java-reference-implementation) | `signposting` | FDPneo emits FAIR Signposting L1 on every `GET`/`HEAD` (ADR-0017). The gap is the Java RI. | Done for FDPneo (ADR-0017) |
| [G-05](../../../mcp/docs/fdp-api-gaps.md#g-05--sparqlsearch-endpoint-locations-are-not-discoverable) | `sparql`, `search-api` | **Addressed (v0.5.0, task 19.2):** the root record now advertises `void:sparqlEndpoint` and DCAT `dcat:DataService` (`dcat:endpointURL`) for SPARQL/search, so a client discovers them from the root. | **Fixed** — server advertises; bridge to consume |
| [G-06](../../../mcp/docs/fdp-api-gaps.md#g-06--shacl-shape-closure-shnode-is-endpoint-specific) | `shapes` | FDPneo serves the composed closure via `?composed=true` and `/{prefix}/spec`; a cross-impl "give me the closure" standard is a spec concern. | Deferred (FDPneo already serves closure on request) |

## Notes

- **G-05 is server-complete, bridge-pending.** FDPneo now *advertises* its
  endpoints (task 19.2); the bridge gains a one-time discovery pass in its own
  repo to *consume* the advertisement instead of relying on configured paths.
- **G-06** needs no FDPneo change: the closure is already available at
  `?composed=true` / `/{prefix}/spec`. Making `ldp:constrainedBy` resolve to the
  closure by default, or agreeing a standard closure request, is an FDP-spec item.
