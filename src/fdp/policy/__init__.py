"""Policy module — ODRL evaluator (PDP), Offer/Agreement lifecycle.

Responsibilities:

* Evaluate ODRL Offers against a request context and return PERMIT or DENY.
* Walk the policy inheritance chain (record → container → repository →
  system default) to resolve the effective policy.
* Materialize ``odrl:Agreement`` instances on PERMIT and persist them to the
  resource's audit graph.
* Maintain the materialized authorization index in Postgres for fast
  set-membership queries used by the SPARQL endpoint and per-record checks.
* Invalidate the index on policy change, record ``dct:rights`` change, or user
  role change.

Non-responsibilities:

* Does *not* enforce policy itself — it provides decisions; other modules act
  on them. (The architecture term: this is the Policy Decision Point. The
  PEPs are the metadata and access modules.)
* Does *not* support ODRL features outside the FDP profile (Permissions and
  Prohibitions only, no Duties, restricted constraint vocabulary). Policies
  using unsupported features are rejected at write time.

Public interface (planned):

* ``authorize(subject, action, resource) -> Decision`` — the synchronous PDP
  call other modules make.
* ``authorized_graphs(subject, action) -> set[URIRef]`` — bulk lookup used by
  the SPARQL endpoint.
* ``materialize_agreement(decision, request_ctx)`` — write Agreement to audit.

See architecture section 8, ADR-0006, and CLAUDE.md.
"""
