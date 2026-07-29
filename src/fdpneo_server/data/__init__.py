"""Data module — simple data provider for open-access distributions.

Responsibilities:

* Serve or redirect to data distributions exposed via ``dcat:downloadURL``.
* Expose per-distribution SPARQL endpoints scoped to RDF distributions whose
  ``dcat:accessURL`` points at this FDP.
* Enforce that only distributions whose ODRL Offer permits anonymous read are
  served. v1 is open-access only.

Non-responsibilities:

* Does *not* host arbitrary file storage. Files referenced by downloadURL
  are either streamed through (small) or served via redirect (large) — the
  operator decides per deployment.
* Does *not* serve restricted distributions. Access-controlled data delivery
  is a v1.x increment.
* Does *not* evaluate ODRL itself — calls the policy module.

Public interface:

* ``router.build_data_router`` — FastAPI router factory for distribution access:
  streaming, redirect, and the per-distribution SPARQL endpoint.
* ``distributions.resolve_distribution`` — resolve a distribution IRI to its
  access info (``DistributionInfo``), backed by ``RecordReader``.

See architecture section 5.6 and CLAUDE.md.
"""
