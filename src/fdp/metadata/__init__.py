"""Metadata module — LDP server, records, schemas, meta-metadata.

Responsibilities:

* Implement the W3C Linked Data Platform server: containers, content
  negotiation across Turtle / JSON-LD / RDF/XML / N-Triples, ETag-based
  concurrency control, GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS.
* Validate every write against the SHACL schema declared by the resource's
  container ``ldp:constrainedBy``.
* Manage meta-metadata (creator, timestamps, version, PROV) on every write —
  always server-controlled, never client-editable.
* Manage SHACL schemas as LDP resources (they are records too).
* Apply SPARQL Update PATCH bodies to single-resource graphs, with SHACL
  re-validation of the simulated post-update state before commit.

Non-responsibilities:

* Does *not* evaluate ODRL. Calls ``policy.authorize(...)`` and reads the
  decision.
* Does *not* talk to the triple store directly. Uses the storage adapter.
* Does *not* expose data distributions — that's the data provider's job.

Public interface (planned):

* ``api`` — FastAPI router mounted at the server's LDP root.
* ``ldp`` — LDP server implementation (request handling, ETag, etc.).
* ``shacl`` — validation helpers using pySHACL.
* ``records`` — domain types for records, containers, schemas.

See architecture sections 5.3 (component), 6 (data model), 10 (LDP),
ADR-0008, and CLAUDE.md.
"""
