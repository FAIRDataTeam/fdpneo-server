# ADR-0023: Downstream composition seams on `create_app`

**Status:** Accepted
**Date:** 2026-07-29
**Relates to:** [ADR-0020](0020-product-distributions.md) (product distributions), [ADR-0005](0005-triple-store-pluggability.md) (the SPARQL 1.1 adapter port)

## Context

FDPneo is consumed two ways: as a deployed container, and as a **library**
by downstream platforms that extend it *by composition* — they import
`create_app()`, wrap it, and ship their own product around it without
forking. The 0.12.0 hand-off from the A2 FAIR Platform (the first such
downstream) surfaced the composition seams they actually need and
currently obtain by monkeypatching:

1. **Storage mediation, not just configuration.** `_build_shared_state`
   constructs ~30 services, including the triple store adapter via
   `TripleStoreAdapter.from_settings(settings.triplestore)`, with no
   injection point. Downstreams can point the adapter at a different
   SPARQL endpoint (configuration) but cannot *mediate* it — wrap it for
   driver-level quirks of a customer's store (e.g. Virtuoso), unified
   telemetry, or query budgets. Today they monkeypatch
   `TripleStoreAdapter.from_settings` for the duration of `create_app()`,
   which is exactly the kind of coupling that breaks silently on our next
   internal refactor.

2. **Routes without fighting the catch-all.** The LDP router's
   `/{path:path}` catch-all matches every method/URL not claimed earlier,
   so a downstream adding endpoints must splice routes *in front of* an
   already-built app's route table — again coupling to internals (route
   list order) rather than to an interface.

The question is what shape the seam should take.

## Decision

`create_app()` grows **keyword-only, optional composition parameters** —
nothing changes for callers that pass nothing:

```python
def create_app(
    *,
    triple_store_factory: Callable[[TripleStoreSettings], TripleStoreAdapter] | None = None,
    extension_routers: Sequence[APIRouter] | None = None,
) -> FastAPI: ...
```

- **`triple_store_factory`** replaces the internal
  `TripleStoreAdapter.from_settings` call. It receives the resolved
  `TripleStoreSettings` and returns the adapter instance every service in
  the app will use (`app.state.triplestore`, the metadata repository, the
  SPARQL projection, SHACL shape reads — all of it). A mediating
  downstream subclasses `TripleStoreAdapter` (or wraps one) and returns
  it here. This is the *only* sanctioned way to interpose on RDF I/O;
  the invariant that **all RDF I/O goes through the triple store
  adapter** is unchanged — the seam decides *which* adapter, not whether
  one is used.

- **`extension_routers`** are mounted at the very end of composition,
  after every reserved `/fdp-api` router and **immediately before the LDP
  catch-all**. Their paths therefore take precedence over LDP resource
  resolution but cannot shadow the FDP's own fixed API. Downstreams own
  the URL space they claim; anything they don't claim falls through to
  LDP exactly as before.

### What we deliberately did not do

- **No subsystem-builder decomposition.** Factoring `_build_shared_state`
  into ~10 public builder functions was considered (and requested as an
  alternative). Rejected for now: it would freeze a large, churning
  internal surface into API. The two parameters above cover the concrete
  downstream needs with a surface we can keep stable. If a second
  downstream needs a different seam, extend the parameter list — each
  addition is deliberate and documented, not a bulk export of internals.
- **No plugin/entry-point mechanism.** Overkill for one known downstream;
  composition-by-import is simpler and typed.
- **No structural `Protocol` port for the adapter (yet).** The factory is
  typed against the concrete `TripleStoreAdapter`, so mediators subclass
  or wrap it. Extracting a `TripleStorePort` protocol is future work if a
  from-scratch adapter implementation ever materialises; subclassing is
  sufficient for mediation and keeps pyright-strict guarantees.

### Stability contract

The two `create_app` parameters are **public API** from 0.13.0 and follow
semver like the rest of the package. Everything hanging off `app.state`
remains internal — downstreams should not reach for it, and this ADR
creates no promise about it.

## Consequences

- The A2 platform deletes its `TripleStoreAdapter.from_settings`
  monkeypatch and its route-splicing, replacing both with `create_app`
  arguments.
- `fdpneo_server.__version__` + these seams make the wheel consumable as
  a proper library: version-detectable, self-contained (ADR follows the
  0.12.0 packaging work), and mediable.
- The composition root stays the single place where cross-context wiring
  happens; the seams do not let a downstream reach around module
  boundaries (a mediating adapter still speaks SPARQL 1.1 Protocol
  underneath, per the adapter-port invariant).
- Risk: an extension router can claim paths that user-defined resource
  types would otherwise use (the root URL namespace). This is inherent —
  the downstream owns their deployment's namespace; we document the
  precedence order rather than police it.
