# ADR-0015: Cross-schema form grouping via a presentation overlay

**Status:** Proposed
**Date:** 2026-06-19

> This ADR captures a design discussion for later review. It is **not yet decided** — it
> records the problem, the recommended approach, and the alternatives so a choice can be
> made deliberately. No code has been written against it.

## Context

The FDP composes a resource type's metadata schema from reusable base shapes. Composition is
purely *structural*: a type's `sh:NodeShape` pulls in base shapes via `sh:node` (and
`sh:and`/`sh:or`/`sh:xone`), and the validator assembles the **transitive closure** of those
shapes into a single merged graph (`src/fdpneo_server/metadata/shacl.py:182-213`). The bundled DCAT
profile uses this to model `dcat:Catalog ⊑ dcat:Dataset ⊑ dcat:Resource`:

- `profiles/default/schemas/resource.ttl` — `fs:resource`, the reusable base (no `sh:targetClass`, only `sh:property`).
- `profiles/default/schemas/dataset.ttl:15` — `fs:dataset` composes `fs:resource` via `sh:node fs:resource`.
- `profiles/default/schemas/catalog.ttl:15` — `fs:catalog` composes `fs:dataset`.

The dev docs state the governing rule: a shape referenced via `sh:node` "contributes only its
*constraints*" (`docs/dev-docs/05-key-processes.md:194-223`). The merge is conjunctive — every
property shape on a path, from every level, applies; there is no override, only tightening.

The type-level read extension `GET /{prefix}/spec` returns this **flat merged closure** to the
client verbatim (`src/fdpneo_server/metadata/extensions.py:105-121`), and the `fdp-client` form renderer
turns it into an entry form.

### The problem

We want forms whose fields are organized into meaningful sections (e.g. "Identification",
"Provenance & attribution", "Access"). SHACL's standard mechanism for this is `sh:group`: each
`sh:PropertyShape` points (by IRI) at a `sh:PropertyGroup` that carries a label and `sh:order`.

This works for a single hand-authored shape. It breaks down for our **composed** schemas: we
cannot put properties that originate in *different base schemas* into the *same* form section.
A section like "Provenance" that should gather `dct:creator` (defined in the `resource` base)
and a dataset-specific property (defined in the `dataset` shape) has no clean home.

`sh:group` is **not used anywhere in the codebase today** (no `sh:group`, `PropertyGroup`, or
`sh:order` in the schemas or code). So this is a greenfield decision, not a migration.

### Why this is not a raw SHACL limitation

`sh:group` references a `PropertyGroup` *by IRI*, and nothing in SHACL stops property shapes in
two different base schemas from pointing at the same group IRI. The merged closure would even
preserve those triples. The real obstacles are about **ownership and reuse**, not expressivity:

1. **Grouping is a presentation concern, but `sh:group` forces it into the constraint
   definition** — i.e. into the reusable base shape. The moment `fs:resource`'s property shapes
   hardcode `sh:group ex:Provenance`, the base is no longer presentation-neutral: every type
   that composes it (`catalog`, `dataset`, `distribution`, …) inherits a layout decision it did
   not make and may not want.
2. **The composite cannot re-group inherited properties.** `fs:dataset` only *references*
   `fs:resource`'s property shapes; it does not own them. It cannot move an inherited property
   into a different section without editing the base, which re-couples the bases.
3. **Group label and `sh:order` are global.** If two bases each define `ex:Provenance` with a
   different label or order, the merged closure contains conflicting triples.

So the design question is really: **where should grouping metadata live**, given that bases must
stay reusable and grouping depends on the *combination*, not on any single base.

## Proposed decision (recommended option)

Separate **layout** from **constraints**, and make the layout owned by the composite type.

**1. A shared `PropertyGroup` vocabulary**, defined once (profile-level, a single graph), with
stable IRIs, labels, and a default order. These are presentation buckets — "Identification",
"Provenance", "Access", "Temporal" — deliberately decoupled from any one base schema.

**2. A per-type layout overlay** that assigns properties to groups *by property path* (plus
group order), owned by the composite resource type rather than by the bases:

```turtle
# shared presentation-layer vocabulary (in no base schema)
ui:identification a sh:PropertyGroup ; rdfs:label "Identification" .
ui:provenance     a sh:PropertyGroup ; rdfs:label "Provenance & attribution" .

# layout overlay owned by the dataset type
fs:datasetLayout a fdp:FormLayout ;
    fdp:forShape   fs:dataset ;
    fdp:groupOrder ( ui:identification ui:provenance ) ;
    fdp:assign [ fdp:path dct:title    ; fdp:group ui:identification ; sh:order 1 ] ,
               [ fdp:path dct:creator  ; fdp:group ui:provenance     ; sh:order 1 ] ,  # from fs:resource base
               [ fdp:path dcat:keyword ; fdp:group ui:identification ; sh:order 2 ] .  # from fs:dataset
```

Because the overlay keys on the *path*, an inherited property (`dct:creator`, from the
`resource` base) and a type-specific property (`dcat:keyword`, from `dataset`) can be placed in
whatever sections we want — which is exactly the cross-base grouping that `sh:group` alone
cannot express cleanly.

**3. Resolve the overlay server-side in the closure builder.** After
`shape_closure` flattens the shapes (`src/fdpneo_server/metadata/shacl.py:182-213`), it looks up the
layout for the root shape and stamps `sh:group`/`sh:order` onto the merged property shapes
(matching by `sh:path`), and emits the `PropertyGroup` definitions plus the group order.

**The wire format at `/spec` stays standard SHACL + DASH (`sh:group`/`sh:order`).** A client
that renders by `sh:group` needs little or no change; the grouping simply arrives already
resolved, with cross-base sections intact.

## Alternatives considered

**Hardcode shared group IRIs directly in the base shapes.** Mechanically works (two bases point
at one `ui:Provenance`), but re-couples the bases to a layout decision and reintroduces the
global label/order conflicts above. The overlay is the same idea with ownership moved to the
correct layer (the composite/profile).

**Re-declare properties on the composite shape to override `sh:group`.** The composite could
add its own `sh:property` entries carrying group assignments. Rejected: SHACL's conjunctive
merge means this *adds* duplicate property shapes per path rather than overriding, fighting the
"no override, only tightening" rule and forcing the client to de-duplicate by path with
last-writer-wins semantics. Hacky and fragile.

**Tag each property with its origin schema (`prov:wasDerivedFrom`) and group by source.**
Rejected: this groups *by base schema*, which is the opposite of the requirement. The goal is
sections that cut *across* bases, not one section per base.

**Emit a bespoke JSON form descriptor from `/spec` instead of SHACL.** The server could compute
an ordered list of groups, each with ordered fields, and return typed JSON. This centralizes
all layout logic server-side and frees the client from SHACL internals. Deferred rather than
rejected: it is a larger contract change with `fdp-client`, and it abandons the standards-based
wire format. The overlay approach can be delivered first and a JSON projection added later via
content negotiation if desired — the two are compatible.

## Open questions for the reviewer

1. **Path ambiguity.** Keying assignments by `sh:path` is ambiguous when a composite has two
   property shapes on the same path (e.g. `sh:qualifiedValueShape`). Mitigation: give base
   property shapes stable IRIs and let the overlay optionally target the *shape* IRI instead of
   the path for those cases. Decide whether to support both keys or only one.
2. **Where the overlay lives.** As a sibling RDF record owned by the resource type, inside the
   type shape's graph, or in the profile manifest. Storing it as an LDP record (like schemas
   and resource definitions per ADR-0009) keeps versioning/export for free.
3. **Vocabulary terms.** `fdp:FormLayout`, `fdp:forShape`, `fdp:assign`, `fdp:path`,
   `fdp:group`, `fdp:groupOrder` would be new terms — register in `shared/namespaces.py`,
   don't redefine in module code.
4. **Ungrouped properties.** Define fallback behavior for properties with no assignment (a
   default trailing "Other" group, or render ungrouped at the end).

## Consequences

**Easier:**
- Form sections can span base schemas; the same inherited property can sit in different sections
  for different composite types.
- Base shapes stay pure constraint definitions and fully reusable — no layout coupling.
- The `/spec` wire format remains standard SHACL + DASH; minimal/no client change if the client
  already honors `sh:group`/`sh:order`.

**Harder / costs:**
- Introduces a **second kind of composition** — a presentation merge — alongside the structural
  `sh:node` merge the architecture currently describes. This needs to be documented in
  `docs/dev-docs/05-key-processes.md` so the closure is no longer described as "constraints only".
- The closure builder gains overlay-resolution logic and a new cache-invalidation edge (a layout
  edit must invalidate the affected closure, like a shape edit does today).
- `/spec` is part of the contract with `fdp-client`; even though the format stays standards-based,
  the change warrants a coordinated note to that repository.

## Decision

_Pending review._
