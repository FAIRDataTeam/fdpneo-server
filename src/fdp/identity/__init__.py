"""Identity bounded context — OIDC authentication.

Validates inbound JWT bearer tokens against the configured OIDC provider's
JWKS, resolves the user identity and roles, and binds an immutable
:class:`~fdp.shared.context.RequestContext` on the active task's ContextVar.

Authorization decisions are *not* this module's concern — the policy module
decides what an authenticated subject may do. Identity is read fresh from
every token; there is no user table.

See architecture §7 and CLAUDE.md for the authentication / authorization
split. Submodules:

* :mod:`fdp.identity.jwks` — discovery + JWKS fetch and cache.
* :mod:`fdp.identity.middleware` — pure-ASGI authentication middleware.
* :mod:`fdp.identity.deps` — FastAPI dependencies (``current_context``,
  ``require_auth``).
"""

from __future__ import annotations

from fdp.identity.deps import current_context, require_auth
from fdp.identity.jwks import JWKSClient, build_jwks_client
from fdp.identity.middleware import AuthenticationMiddleware

__all__ = [
    "AuthenticationMiddleware",
    "JWKSClient",
    "build_jwks_client",
    "current_context",
    "require_auth",
]
