"""Shared fixtures for the identity unit tests.

Generates an RSA keypair once per module, exposes a token factory, and a
helper to build a JWKS dict suitable for the IdP mock.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

ISSUER = "http://idp.local/realms/fdp"
AUDIENCE = "fdp"


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass
class TokenSigner:
    """Self-contained signing material with a kid + JWK representation."""

    kid: str
    private_key: RSAPrivateKey

    def jwk(self) -> dict[str, str]:
        public_numbers = self.private_key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": _b64url_uint(public_numbers.n),
            "e": _b64url_uint(public_numbers.e),
        }

    def sign(self, payload: dict[str, Any]) -> str:
        pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": self.kid})


def _empty_signers() -> list[TokenSigner]:
    return []


@dataclass
class IdPFixture:
    """Bundle of issuer, audience, signers, and a JWKS dict for a mock IdP."""

    issuer: str = ISSUER
    audience: str = AUDIENCE
    signers: list[TokenSigner] = field(default_factory=_empty_signers)

    def jwks(self) -> dict[str, Any]:
        return {"keys": [s.jwk() for s in self.signers]}

    def discovery_doc(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs",
            "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth",
            "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
        }

    @property
    def jwks_uri(self) -> str:
        return self.discovery_doc()["jwks_uri"]

    @property
    def discovery_uri(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    def token(
        self,
        *,
        signer_index: int = 0,
        sub: str = "alice",
        aud: str | None = None,
        iss: str | None = None,
        roles: list[str] | None = None,
        roles_path: tuple[str, ...] = ("realm_access", "roles"),
        exp_offset: int = 600,
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": iss if iss is not None else self.issuer,
            "aud": aud if aud is not None else self.audience,
            "sub": sub,
            "iat": now,
            "exp": now + exp_offset,
        }
        if roles is not None:
            cursor: dict[str, Any] = payload
            for part in roles_path[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[roles_path[-1]] = roles
        if extra:
            payload.update(extra)
        return self.signers[signer_index].sign(payload)


def _new_signer(kid: str) -> TokenSigner:
    return TokenSigner(
        kid=kid, private_key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )


@pytest.fixture(scope="module")
def idp() -> IdPFixture:
    """A mock IdP with one signing key, valid for the whole module."""
    return IdPFixture(signers=[_new_signer("primary")])


@pytest.fixture(scope="module")
def second_signer() -> TokenSigner:
    """An *unknown* second keypair, for bad-signature tests."""
    return _new_signer("rogue")
