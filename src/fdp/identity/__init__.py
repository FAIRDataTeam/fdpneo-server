"""Identity module — OIDC integration and request context.

Responsibilities:

* Validate inbound JWT bearer tokens against the OIDC provider's JWKS.
* Resolve user identity, roles, and group memberships from token claims.
* Build the immutable ``RequestContext`` that downstream modules read from.
* Provide FastAPI dependencies / middleware that other modules use to require
  authenticated access.

Non-responsibilities:

* This module does *not* make authorization decisions. It establishes *who*
  the user is; the policy module decides *what* they may do.
* It does *not* persist user data. Identity is read fresh from every token;
  there is no user table.

Public interface (planned):

* ``middleware.authenticate`` — FastAPI middleware factory.
* ``deps.require_auth`` — dependency that 401s anonymous requests.
* ``deps.current_user`` — dependency returning the ``RequestContext``.

See architecture document Section 7 and CLAUDE.md for the rules around the
authentication / authorization split.
"""
