"""Storage module — triple store adapter and Postgres repository.

Two adapters mediate all I/O. Other modules consume these through repository
interfaces; no module talks to the triple store or Postgres directly.

Triple store adapter:

* Speaks SPARQL 1.1 Protocol exclusively.
* Exposes: ``query``, ``update``, ``ingest_graph``, ``replace_graph``,
  ``drop_graph``, ``ask``.
* Vendor capabilities (GraphDB repo management, named-graph cluster sync)
  sit behind capability flags read from configuration.

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

Public interface (planned):

* ``triplestore.TripleStoreAdapter`` — the SPARQL 1.1 Protocol port.
* ``postgres.repositories`` — typed repositories per aggregate.
* ``models`` — SQLAlchemy ORM models for Postgres tables.

See architecture sections 4.3, 4.4, 5.8, and ADRs 0003, 0005, 0007.
"""
