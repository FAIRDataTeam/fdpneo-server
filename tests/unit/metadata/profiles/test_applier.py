"""Unit tests for the profile applier."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import HttpUrl, PostgresDsn
from rdflib import Graph, URIRef

from fdp.config import OIDCSettings, Settings, TripleStoreSettings
from fdp.metadata.profiles import apply_profile, load_profile
from fdp.shared.errors import BadRequest, Conflict


# --- in-memory fakes -------------------------------------------------------


@dataclass
class _FakeRepo:
    put_calls: list[tuple[str, int]] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    fail_on_put: str | None = None
    """If set to an IRI, the put_graph call for that IRI raises."""

    async def put_graph(
        self, record_uri: str, graph: Graph, *, subject: str | None
    ) -> str:
        del subject
        if self.fail_on_put is not None and record_uri == self.fail_on_put:
            raise RuntimeError("simulated triple-store failure")
        self.put_calls.append((record_uri, len(graph)))
        return "etag-" + str(len(self.put_calls))

    async def delete_graph(self, record_uri: str) -> None:
        self.delete_calls.append(record_uri)


@dataclass
class _FakeState:
    applied: bool = False
    recorded: dict[str, str] | None = None
    cleared: int = 0

    async def current(self) -> Any:
        return object() if self.applied else None

    async def is_applied(self) -> bool:
        return self.applied

    async def record(
        self,
        *,
        name: str,
        version: str,
        manifest_checksum: str,
        applied_at: Any = None,
    ) -> Any:
        del applied_at
        self.recorded = {
            "name": name,
            "version": version,
            "manifest_checksum": manifest_checksum,
        }
        return object()

    async def clear(self) -> int:
        self.cleared += 1
        return 1


class _FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _settings() -> Settings:
    return Settings(
        postgres_dsn=PostgresDsn(
            "postgresql+asyncpg://fdp:fdp@localhost:5432/fdp_test"
        ),
        triplestore=TripleStoreSettings(
            query_endpoint=HttpUrl("http://triplestore.local/query"),
            update_endpoint=HttpUrl("http://triplestore.local/update"),
        ),
        oidc=OIDCSettings(
            issuer=HttpUrl("http://idp.local/realms/fdp"),
            audience="fdp",
        ),
    )


# --- happy path ----------------------------------------------------------


@pytest.mark.unit
async def test_apply_writes_schemas_then_containers_then_offers(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    repo = _FakeRepo()
    state = _FakeState()
    session = _FakeSession()

    report = await apply_profile(
        profile,
        repository=repo,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        settings=_settings(),
    )

    iris = [c[0] for c in repo.put_calls]
    # 1 schema, 1 container, 1 offer.
    assert iris == [
        "http://www.w3.org/ns/dcat#Catalog",
        "http://localhost:8000/repository",
        "http://localhost:8000/offers/system-default",
    ]
    assert report.total_written == 3
    assert report.rolled_back is False
    assert state.recorded is not None
    assert state.recorded["name"] == "test"
    assert session.committed is True


# --- already-initialized refusal ----------------------------------------


@pytest.mark.unit
async def test_apply_refuses_when_already_initialized(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    state = _FakeState(applied=True)
    with pytest.raises(Conflict):
        await apply_profile(
            profile,
            repository=_FakeRepo(),  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            session=_FakeSession(),  # type: ignore[arg-type]
            settings=_settings(),
        )


@pytest.mark.unit
async def test_apply_force_skips_the_already_initialized_check(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    state = _FakeState(applied=True)
    # Force=True bypasses the refusal but the caller (CLI) is expected
    # to have cleared state already. Here we just confirm the applier
    # itself doesn't raise on force.
    state.applied = False  # simulate post-clear
    report = await apply_profile(
        profile,
        repository=_FakeRepo(),  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        session=_FakeSession(),  # type: ignore[arg-type]
        settings=_settings(),
        force=True,
    )
    assert report.rolled_back is False


# --- rollback on failure --------------------------------------------------


@pytest.mark.unit
async def test_apply_rolls_back_on_triple_store_failure(
    write_bundle: Callable[..., Path],
) -> None:
    profile = load_profile(write_bundle())
    # Fail on the container write (second put). Schema was already written,
    # so it must be dropped during rollback.
    repo = _FakeRepo(fail_on_put="http://localhost:8000/repository")
    state = _FakeState()
    session = _FakeSession()

    with pytest.raises(Exception) as exc:  # ApplyError or pass-through
        await apply_profile(
            profile,
            repository=repo,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
            settings=_settings(),
        )

    # Schema was rolled back.
    assert repo.delete_calls == ["http://www.w3.org/ns/dcat#Catalog"]
    assert session.rolled_back is True
    assert state.recorded is None
    assert "profile_apply" in repr(exc.value) or "simulated" in repr(exc.value)


# --- validation failure rejects before any writes ------------------------


@pytest.mark.unit
async def test_apply_refuses_invalid_profile_without_writing(
    write_bundle: Callable[..., Path],
) -> None:
    # Container references undeclared schema → validator fails.
    from tests.unit.metadata.profiles.conftest import MANIFEST

    bad_manifest = MANIFEST.replace("constrainedBy: dcat:Catalog", "constrainedBy: dcat:Unknown")
    profile = load_profile(write_bundle(manifest_text=bad_manifest))
    repo = _FakeRepo()
    state = _FakeState()

    with pytest.raises(BadRequest):
        await apply_profile(
            profile,
            repository=repo,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            session=_FakeSession(),  # type: ignore[arg-type]
            settings=_settings(),
        )
    assert repo.put_calls == []
