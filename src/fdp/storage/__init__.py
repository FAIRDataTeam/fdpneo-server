"""Storage module — triple store adapter and Postgres repository.

Two adapters mediate all I/O. Other modules consume these through repository
interfaces; no module talks to the triple store or Postgres directly.

Triple store adapter:

* Speaks SPARQL 1.1 Protocol exclusively.
* Exposes: ``query``, ``query_stream``, ``ask``, ``update``, ``ingest_graph``,
  ``replace_graph``, ``drop_graph``, ``clear_all``, plus the module-level
  ``construct_named_graph`` helper.
* Vendor capabilities (GraphDB repo management, named-graph cluster sync) are
  out of scope for this base adapter (ADR-0005).

Postgres repository:

* SQLAlchemy 2.x in async mode.
* Owns the schema for metrics aggregates, the materialized authorization
  index, audit-decision log, OIDC session bookkeeping, and background-job
  state.
* Migrations live in ``migrations/`` (Alembic).

Non-responsibilities:

* Does *not* contain business logic. Repositories return domain types; the
  domain modules use them.
* Does *not* combine triple store and Postgres data. If a use case appears
  to need that, the cross-store join belongs in the consuming module.

Public interface:

* ``triplestore.TripleStoreAdapter`` — the SPARQL 1.1 Protocol port (plus the
  ``construct_named_graph`` helper).
* ``postgres.models`` — SQLAlchemy ORM models (``Base`` and the table mappings).
* ``postgres.engine`` — async engine and session-factory wiring.

See architecture sections 4.3, 4.4, 5.8, and ADRs 0003, 0005, 0007.
"""
