# Architecture Decision Records

This directory contains the rationale for the major architectural decisions in the FDP v2 design. Each ADR captures the context that motivated the decision, the choice made, the alternatives considered, and the consequences accepted.

ADRs are numbered sequentially and are immutable once accepted. Superseding decisions are recorded as new ADRs that reference the ones they replace.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith over microservices | Accepted |
| [0002](0002-anonymous-metrics.md) | Anonymous-by-design metrics pipeline | Accepted |
| [0003](0003-fixed-postgres-for-operational-state.md) | Fixed Postgres for operational state | Accepted |
| [0004](0004-sparql-access-via-named-graph-projection.md) | SPARQL access control via named-graph projection | Accepted |
| [0005](0005-triple-store-pluggability.md) | Pluggable triple store via SPARQL 1.1 Protocol | Accepted |
| [0006](0006-odrl-profile-permission-prohibition.md) | ODRL profile: Permissions and Prohibitions | Accepted |
| [0007](0007-one-graph-per-record.md) | One named graph per metadata record | Accepted |
| [0008](0008-full-ldp-with-patch.md) | Full LDP implementation including PATCH | Accepted |
| [0009](0009-runtime-resource-definitions.md) | Runtime-mutable resource definitions stored as RDF | Accepted |
| [0010](0010-metadata-publication-state.md) | Metadata publication state (draft/published/archived) | Accepted |
| [0011](0011-api-keys.md) | API keys for machine-to-machine access | Accepted |
| [0012](0012-first-class-odrl-policy-and-license-documents.md) | First-class ODRL policy and license documents | Accepted |
| [0013](0013-user-management-facade.md) | User-management facade over the IdP Admin API | Accepted |

## Format

Each ADR follows a lightweight Nygard-style format:

- **Status** — proposed, accepted, deprecated, or superseded
- **Context** — the forces at play
- **Decision** — what we are doing
- **Alternatives considered** — what we did not do and why
- **Consequences** — what becomes easier and harder as a result
