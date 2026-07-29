"""Identity bounded context — OIDC authentication.

Validates inbound JWT bearer tokens against the configured OIDC provider's
JWKS, resolves the user identity and roles, and binds an immutable
:class:`~fdpneo_server.shared.context.RequestContext` on the active task's ContextVar.

Authorization decisions are *not* this module's concern — the policy module
decides what an authenticated subject may do. Identity is read fresh from
every token; there is no user table.

See architecture §7 and CLAUDE.md for the authentication / authorization
split. Submodules:

* :mod:`fdpneo_server.identity.jwks` — discovery + JWKS fetch and cache.
* :mod:`fdpneo_server.identity.middleware` — pure-ASGI authentication middleware.
* :mod:`fdpneo_server.identity.deps` — FastAPI dependencies (``current_context``,
  ``require_auth``).
"""

from __future__ import annotations

from fdpneo_server.identity.deps import current_context, require_auth
from fdpneo_server.identity.jwks import JWKSClient, build_jwks_client
from fdpneo_server.identity.middleware import AuthenticationMiddleware

__all__ = [
    "AuthenticationMiddleware",
    "JWKSClient",
    "build_jwks_client",
    "current_context",
    "require_auth",
]
