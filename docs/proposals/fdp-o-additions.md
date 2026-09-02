# Proposal: FDP-O additions for metadata lifecycle, affordances, and resource definitions

**To:** FAIRDataTeam/FDP-O maintainers
**From:** FDPneo (fdpneo-server), which mints these terms in the
`https://w3id.org/fdp/fdp-o#` namespace as extension terms pending this
proposal (see fdpneo-server ADR-0026).
**Status:** Draft for discussion

## Motivation

Both FDP implementations (the reference FAIRDataPoint and FDPneo) share three
concept families that FDP-O does not yet name:

1. a **publication lifecycle** per metadata record (`DRAFT` → `PUBLISHED` →
   `ARCHIVED`), which the reference implementation models internally and
   FDPneo publishes as RDF in each record's meta-metadata;
2. **hypermedia affordances** — typed link relations that let a harvester or
   agent discover a record's management views (meta-metadata, SHACL spec,
   state transitions…) by link-following instead of URL conventions; and
3. **resource definitions** — the machine-readable description of which
   resource types an FDP serves and how they nest, which the reference
   implementation exposes over its API and FDPneo stores as RDF records.

Naming these in FDP-O makes them interoperable rather than
implementation-private. All terms below are additive; no existing FDP-O term
changes.

## Proposed terms

### Publication lifecycle (predicates on the meta-metadata entity)

| Term | Type | Domain | Range | Definition |
|---|---|---|---|---|
| `fdp-o:metadataState` | owl:DatatypeProperty | the record's meta-metadata entity (prov:Entity) | xsd:string, one of `DRAFT` / `PUBLISHED` / `ARCHIVED` | The record's current publication state. `PUBLISHED` records are visible to any authorized reader; other states only to curators. |
| `fdp-o:allowedStateTransition` | owl:DatatypeProperty | the record's meta-metadata entity | xsd:string (same value space) | A state the record may transition to next, per the server's state machine. Served-representation only (an affordance, not stored fact). |
| `fdp-o:validatedAgainst` | owl:ObjectProperty | the record's meta-metadata entity | the immutable version IRI of a metadata profile | The exact profile version the record was validated against at write time. Complements `dct:conformsTo` (stable profile) on the record itself. |

### Provenance activity classes (objects of `prov:wasGeneratedBy` chains)

| Term | Type | Definition |
|---|---|---|
| `fdp-o:CreateOperation` | owl:Class ⊑ prov:Activity | The activity that created a metadata record. |
| `fdp-o:ModifyOperation` | owl:Class ⊑ prov:Activity | The activity that last modified a metadata record. |

### Affordance link relations (RFC 8288 extension relation types)

Used in HTTP `Link` headers on record responses; opaque IRIs, no RDF
semantics required beyond identity.

| Term | Target | Definition |
|---|---|---|
| `fdp-o:hasMetaMetadata` | `<record>/meta` | The record's meta-metadata (provenance, versioning, publication state). |
| `fdp-o:hasSpec` | a SHACL document | The shape that validates this record (instance- or type-level). |
| `fdp-o:hasExpandedView` | `<record>/expanded` | A denormalized view embedding the record's children summaries. |
| `fdp-o:hasStateTransition` | `<record>/state` | The endpoint accepting publication-state transitions. |
| `fdp-o:hasMemberPage` | `<record>/page/{childType}` | A paged listing of one child type of a container. |
| `fdp-o:hasResourceDefinitions` | the resource-definition collection | The FDP's machine-readable type catalog (root record only). |

### Resource definitions (the FDP's type catalog as RDF)

| Term | Type | Definition |
|---|---|---|
| `fdp-o:ResourceDefinition` | owl:Class | A resource type an FDP serves: its route segment, display name, validating shape, and child links. |
| `fdp-o:urlPrefix` | owl:DatatypeProperty (xsd:string) | Route segment the type is exposed under; empty string denotes the root. |
| `fdp-o:name` | owl:DatatypeProperty (xsd:string) | The type's display name. |
| `fdp-o:childLink` | owl:ObjectProperty | Links a resource definition to a child-link description. |
| `fdp-o:relationUri` | owl:ObjectProperty | The predicate a parent record uses to point at members of the child type. |
| `fdp-o:childTarget` | owl:DatatypeProperty (xsd:string) | `urlPrefix` of the child's resource definition. |
| `fdp-o:childTitle` | owl:DatatypeProperty (xsd:string) | Human label for the child listing. |
| `fdp-o:childTagsUri` | owl:ObjectProperty | Optional predicate naming the tag vocabulary for a child listing. |

### Managed license marker

| Term | Type | Definition |
|---|---|---|
| `fdp-o:ManagedLicense` | owl:Class | Marker class for license documents managed by (stored in) an FDP, used to validate their descriptive contract uniformly. |

## Compatibility notes

- The validator in the FAIRDataPoint index (`MetadataRetrievalUtils`) already
  matches `fdp-o:MetadataService` — no index change is needed for these
  additions.
- FDPneo emits every term above today (v0.16+); earlier FDPneo releases used
  the unregistered `https://w3id.org/fdp/o#` namespace, migrated in place at
  upgrade.
- Suggested ontology hygiene: declare `fdp-o:FAIRDataPoint rdfs:subClassOf
  fdp-o:MetadataService` consumers SHOULD materialize, and note in the spec
  that index validators match `MetadataService` without inference — hence
  implementations SHOULD assert both types on the root.
