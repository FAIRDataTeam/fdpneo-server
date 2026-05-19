"""Shared fixtures for shared-kernel unit tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import HttpUrl

from fdp.config import OIDCSettings, Settings, TripleStoreSettings


@pytest.fixture
def make_settings() -> Any:
    """Return a factory that builds a fully-formed ``Settings`` for tests.

    Required fields are filled with placeholders; callers override what they
    care about by passing keyword arguments.
    """

    def _factory(**overrides: Any) -> Settings:
        defaults: dict[str, Any] = {
            "postgres_dsn": "postgresql+asyncpg://fdp:fdp@localhost:5432/fdp",
            "triplestore": TripleStoreSettings(
                query_endpoint=HttpUrl("http://triplestore.local/query"),
                update_endpoint=HttpUrl("http://triplestore.local/update"),
            ),
            "oidc": OIDCSettings(
                issuer=HttpUrl("http://idp.local/realms/fdp"),
                audience="fdp",
            ),
        }
        defaults.update(overrides)
        return Settings(**defaults)

    return _factory
