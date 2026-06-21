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
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol

import jwt
import structlog
from jwt import InvalidTokenError

from fdp.identity.api_keys import TOKEN_PREFIX
from fdp.identity.jwks import JWKSError
from fdp.shared.context import RequestContext, reset_current, set_current
from fdp.shared.errors import Unauthenticated, error_envelope

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from fdp.config import OIDCSettings
    from fdp.identity.jwks import JWKSClient

log = structlog.get_logger(__name__)

_ALLOWED_ALGS: Final = ("RS256", "RS384", "RS512", "ES256", "ES384")

# How long an unchanged (subject, roles, groups) tuple is cached before the
# middleware re-records it. A *change* records immediately regardless.
_PRINCIPAL_REFRESH_SECONDS: Final = 300.0


class ApiKeyAuthenticator(Protocol):
    """Resolves a ``fdpk_`` bearer token to a context, or ``None`` if invalid."""

    async def authenticate(self, token: str, *, trace_id: str) -> RequestContext | None: ...


class PrincipalRecorder(Protocol):
    """Records a subject's freshest IdP-asserted roles/groups (ADR-0011 §4)."""

    async def record(
        self, subject: str, *, roles: frozenset[str], groups: frozenset[str]
    ) -> None: ...


class AuthenticationMiddleware:
    """ASGI middleware that authenticates a request and binds the context."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        oidc: OIDCSettings,
        jwks_client_provider: Callable[[], JWKSClient],
        api_key_authenticator_provider: Callable[[], ApiKeyAuthenticator] | None = None,
        principal_recorder_provider: Callable[[], PrincipalRecorder] | None = None,
    ) -> None:
        self._app = app
        self._oidc = oidc
        self._issuer = str(oidc.issuer).rstrip("/")
        self._jwks_client_provider = jwks_client_provider
        self._api_key_authenticator_provider = api_key_authenticator_provider
        self._principal_recorder_provider = principal_recorder_provider
        # In-memory throttle for the principal upsert: subject → (roles, groups,
        # monotonic timestamp). Bounds writes on the JWT hot path; a role change
        # still records immediately.
        self._principal_seen: dict[str, tuple[frozenset[str], frozenset[str], float]] = {}

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

        # Dispatch by prefix (ADR-0011 §3): an ``fdpk_`` bearer is an API key,
        # resolved against Postgres; anything else is validated as a JWT. This
        # keeps the JWT path DB-free and avoids API-key lookups on malformed
        # JWTs.
        if token.startswith(TOKEN_PREFIX):
            return await self._resolve_api_key(token, trace_id)

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
        groups = frozenset(_get_nested_claim(payload, self._oidc.groups_claim))
        ctx = RequestContext(
            subject=subject,
            roles=roles,
            groups=groups,
            trace_id=trace_id,
        )
        await self._maybe_record_principal(ctx)
        return ctx

    async def _resolve_api_key(self, token: str, trace_id: str) -> RequestContext:
        """Resolve an ``fdpk_`` token, or 401 if the feature is off / token invalid."""
        provider = self._api_key_authenticator_provider
        if provider is None:
            raise Unauthenticated("API key authentication is not enabled")
        ctx = await provider().authenticate(token, trace_id=trace_id)
        if ctx is None:
            raise Unauthenticated("invalid API key")
        return ctx

    async def _maybe_record_principal(self, ctx: RequestContext) -> None:
        """Opportunistically refresh the subject's principal record (throttled).

        This is what lets a long-lived API key track its owner's current roles
        (ADR-0011 §4). Best-effort: a failure here must never reject a valid
        login.
        """
        provider = self._principal_recorder_provider
        if provider is None or ctx.subject is None:
            return
        if not self._should_record(ctx):
            return
        try:
            await provider().record(ctx.subject, roles=ctx.roles, groups=ctx.groups)
        except Exception as exc:
            log.warning("principal_record_failed", subject=ctx.subject, error=repr(exc))

    def _should_record(self, ctx: RequestContext) -> bool:
        assert ctx.subject is not None
        now = time.monotonic()
        prev = self._principal_seen.get(ctx.subject)
        if (
            prev is not None
            and prev[0] == ctx.roles
            and prev[1] == ctx.groups
            and now - prev[2] < _PRINCIPAL_REFRESH_SECONDS
        ):
            return False
        self._principal_seen[ctx.subject] = (ctx.roles, ctx.groups, now)
        return True


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
    body = json.dumps(error_envelope(exc)).encode("utf-8")
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
