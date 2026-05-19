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

Public interface (planned):

* ``api`` — FastAPI router for distribution access.
* ``streams`` — file streaming and redirect handlers.
* ``per_distribution_sparql`` — scoped SPARQL endpoint per RDF distribution.

See architecture section 5.6 and CLAUDE.md.
"""
