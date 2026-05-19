"""Pytest configuration and shared fixtures for the fdp-server test suite.

Fixtures live here when they cross multiple test files. Per-file fixtures stay
in the file that uses them.

Test organization:

* ``tests/unit/``        — fast, no I/O. Pure Python.
* ``tests/integration/`` — uses testcontainers to spin up GraphDB / Fuseki /
                          Oxigraph and Postgres. Slower; marked ``integration``.
* ``tests/contract/``    — verifies the OpenAPI schema matches the running API.
* ``tests/conformance/`` — FDP specs and LDP test suite conformance.

Markers (declared in pyproject.toml):

* ``unit``         — pure-Python unit tests (default).
* ``integration``  — needs containers.
* ``conformance``  — spec conformance.
* ``slow``         — opt-in long-running tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Provide placeholder values for required Settings fields before any test
# imports `fdp.config` or `fdp.main`. These are session-wide and never reach a
# real backend — unit tests must not touch I/O.
_TEST_ENV = {
    "POSTGRES_DSN": "postgresql+asyncpg://fdp:fdp@localhost:5432/fdp_test",
    "FDP_TRIPLESTORE_QUERY_ENDPOINT": "http://triplestore.local/query",
    "FDP_TRIPLESTORE_UPDATE_ENDPOINT": "http://triplestore.local/update",
    "FDP_OIDC_ISSUER": "http://idp.local/realms/fdp",
    "FDP_OIDC_AUDIENCE": "fdp",
}
for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Force anyio to use asyncio. Required by pytest-asyncio interoperability."""
    return "asyncio"


@pytest.fixture
def reset_settings_cache() -> Iterator[None]:
    """Clear the ``get_settings`` LRU cache around a test that changes env vars."""
    from fdp.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
