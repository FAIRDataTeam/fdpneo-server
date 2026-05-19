"""Access module — SPARQL endpoint with access control.

Responsibilities:

* Expose the SPARQL 1.1 Protocol endpoint at ``/sparql``.
* Parse incoming queries with RDFLib into algebra trees.
* Classify queries as read (SELECT/ASK/CONSTRUCT/DESCRIBE) or update
  (INSERT/DELETE/LOAD/CLEAR/CREATE/DROP/COPY/MOVE/ADD).
* Reject anonymous updates with 401.
* Reject ``SERVICE`` clauses (no federation in v1).
* Reject ambiguous-target updates (require explicit WITH/GRAPH).
* Rewrite reads by injecting ``FROM NAMED`` clauses for the user's authorized
  graph set, obtained from the policy module's ``authorized_graphs`` lookup.
* Validate explicit graph references in updates against the user's modify set.
* Forward the rewritten query to the storage adapter.
* Stream results back in the negotiated format.

Non-responsibilities:

* Does *not* evaluate ODRL — calls the policy module.
* Does *not* talk to the triple store directly — uses the storage adapter.
* Does *not* maintain the authorization cache — the policy module does.

Public interface (planned):

* ``api`` — FastAPI router mounted at ``/sparql``.
* ``parser`` — query parsing and classification.
* ``rewriter`` — FROM-NAMED injection and explicit-graph validation.

See architecture section 9, ADR-0004, and CLAUDE.md.
"""
