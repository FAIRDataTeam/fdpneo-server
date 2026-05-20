"""Pure-ASGI authentication middleware.

**Responsibilities**

* Extract the bearer token from the ``Authorization`` header.
* Validate the JWT signature against the IdP's JWKS, plus standard claims
  (``iss``, ``aud``, ``exp``, ``iat``, ``sub``).
* Build an immutable :class:`RequestContext` and bind it on the ContextVar
  for the request's lifetime so logging and downstream PEPs can read it.

**Non-responsibilities**

* Authorization. Roles are extracted from the token; PEPs decide what each
  role can do.
* Token issuance. Authentication is delegated entirely to the IdP.

**Header semantics**

* No ``Authorization`` header → anonymous context. The request proceeds;
  downstream PEPs decide whether anonymous can do the thing.
* ``Authorization: Bearer <valid>`` → authenticated context.
* Anything else (``Basic``, malformed, empty bearer) → 401 with the
  ``fdp.unauthenticated`` envelope. We fail loudly rather than silently
  treat a misconfigured client as anonymous.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import jwt
import structlog
from jwt import InvalidTokenError

from fdp.identity.jwks import JWKSError
from fdp.shared.context import RequestContext, reset_current, set_current
from fdp.shared.errors import Unauthenticated

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from fdp.config import OIDCSettings
    from fdp.identity.jwks import JWKSClient

log = structlog.get_logger(__name__)

_ALLOWED_ALGS: Final = ("RS256", "RS384", "RS512", "ES256", "ES384")


class AuthenticationMiddleware:
    """ASGI middleware that authenticates a request and binds the context."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        oidc: OIDCSettings,
        jwks_client_provider: Callable[[], JWKSClient],
    ) -> None:
        self._app = app
        self._oidc = oidc
        self._issuer = str(oidc.issuer).rstrip("/")
        self._jwks_client_provider = jwks_client_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        trace_id = uuid.uuid4().hex
        header = _extract_authorization(scope)

        try:
            ctx = await self._resolve_context(header, trace_id)
        except Unauthenticated as exc:
            await _send_envelope(send, exc, trace_id)
            return

        token = set_current(ctx)
        try:
            await self._app(scope, receive, send)
        finally:
            reset_current(token)

    async def _resolve_context(self, header: str | None, trace_id: str) -> RequestContext:
        if header is None:
            return RequestContext.anonymous(trace_id=trace_id)

        scheme, _, raw_token = header.partition(" ")
        if scheme.lower() != "bearer":
            raise Unauthenticated("unsupported authorization scheme")
        token = raw_token.strip()
        if not token:
            raise Unauthenticated("empty bearer token")

        try:
            unverified = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise Unauthenticated("malformed bearer token") from exc

        alg = unverified.get("alg")
        kid = unverified.get("kid")
        if alg not in _ALLOWED_ALGS:
            raise Unauthenticated(f"disallowed signing algorithm: {alg!r}")
        if not isinstance(kid, str):
            raise Unauthenticated("token header missing 'kid'")

        try:
            pyjwk = await self._jwks_client_provider().get_signing_key(kid)
        except JWKSError as exc:
            raise Unauthenticated("could not resolve token signing key") from exc

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                key=pyjwk,
                algorithms=list(_ALLOWED_ALGS),
                audience=self._oidc.audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=30,
            )
        except InvalidTokenError as exc:
            raise Unauthenticated("invalid bearer token") from exc

        subject = f"{self._issuer}#{payload['sub']}"
        roles = frozenset(_get_nested_claim(payload, self._oidc.roles_claim))
        return RequestContext(
            subject=subject,
            roles=roles,
            trace_id=trace_id,
        )


def _extract_authorization(scope: Scope) -> str | None:
    headers = scope.get("headers", [])
    for raw_name, raw_value in headers:
        if raw_name == b"authorization":
            try:
                return raw_value.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


def _get_nested_claim(payload: Mapping[str, object], dotted: str) -> list[str]:
    """Walk a dotted claim path. Missing or wrong-typed → empty list."""
    cursor: object = payload
    for part in dotted.split("."):
        if not isinstance(cursor, Mapping):
            return []
        # Mapping value type is `object`; this returns object | None.
        cursor = cursor.get(part)  # type: ignore[assignment]
        if cursor is None:
            return []
    if not isinstance(cursor, list):
        return []
    items: list[object] = cursor  # type: ignore[assignment]
    if not all(isinstance(x, str) for x in items):
        return []
    return [x for x in items if isinstance(x, str)]


async def _send_envelope(send: Send, exc: Unauthenticated, trace_id: str) -> None:
    """Render the FDP error envelope from inside the middleware."""
    log.warning(
        "fdp_error",
        code=exc.code,
        http_status=exc.http_status,
        message=exc.message,
        trace_id=trace_id,
    )
    body = json.dumps(
        {
            "code": exc.code,
            "message": exc.message,
            "docs_url": exc.docs_url,
            "details": None,
        }
    ).encode("utf-8")
    start: Message = {
        "type": "http.response.start",
        "status": exc.http_status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})


__all__ = ["AuthenticationMiddleware"]
