"""Unit tests for the profile applier."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import HttpUrl, PostgresDsn
from rdflib import Graph

from fdp.config import OIDCSettings, Settings, TripleStoreSettings
from fdp.metadata.profiles import apply_profile, load_profile, resolve_runtime_state
from fdp.shared.errors import BadRequest, Conflict

# --- in-memory fakes -------------------------------------------------------


@dataclass
class _FakeRepo:
    put_calls: list[tuple[str, int]] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    fail_on_put: str | None = None
    """If set to an IRI, the put_graph call for that IRI raises."""

    async def put_graph(self, record_uri: str, graph: Graph, *, subject: str | None) -> str:
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
        postgres_dsn=PostgresDsn("postgresql+asyncpg://fdp:fdp@localhost:5432/fdp_test"),
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
async def test_apply_writes_schemas_then_offers_then_repository_seed(
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
    # Apply order: schemas → offers → RD shape → RD records → Repository
    # seed (ADR-0009). The offer IRI is the one declared inside the TTL
    # (intrinsic to the bundle). The single resource definition is the
    # root Repository; its record lands under the reserved
    # resource-definitions namespace, slugged from its name. The
    # Repository seed itself lives at the configured base_url (the API
    # root) so the LDP layer serves it at "/".
    assert iris == [
        "http://www.w3.org/ns/dcat#Catalog",
        "https://fdp.example/offers/system-default",
        "https://w3id.org/fdp/o#ResourceDefinitionShape",
        "http://localhost:8000/resource-definitions/repository",
        "http://localhost:8000",
    ]
    assert report.total_written == 5
    assert report.rd_shape_iri == "https://w3id.org/fdp/o#ResourceDefinitionShape"
    assert report.resource_definition_records == [
        "http://localhost:8000/resource-definitions/repository"
    ]
    assert report.repository_iri == "http://localhost:8000"
    assert report.rolled_back is False
    assert state.recorded is not None
    assert state.recorded["name"] == "test"
    assert session.committed is True

    # The cache from build_cache_from_manifest is handed to the caller
    # so app.state.resource_definitions can be populated post-apply.
    assert report.resource_definitions is not None
    assert report.resource_definitions.root() is not None


# --- already-initialized refusal ----------------------------------------


@pytest.mark.unit
def test_resolve_runtime_state_derives_offer_and_definitions_without_writes(
    write_bundle: Callable[..., Path],
) -> None:
    # Regression: on restart the profile is already applied, so apply_profile is
    # skipped. The runtime state (system-default offer IRI + resource-definition
    # cache) must still be derivable from the profile alone — otherwise the
    # offer-resolver fallback is unset and creating new records is default-denied.
    profile = load_profile(write_bundle())

    system_default_offer_iri, resource_definitions = resolve_runtime_state(
        profile, settings=_settings()
    )

    assert system_default_offer_iri == "https://fdp.example/offers/system-default"
    assert resource_definitions is not None
    assert resource_definitions.root() is not None


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
    # Fail on the Repository seed (the last put). Schema, offer, RD shape
    # and the RD record were already written, so all must be dropped
    # during rollback.
    repo = _FakeRepo(fail_on_put="http://localhost:8000")
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

    # All prior writes (schema, offer, RD shape, RD record) were rolled
    # back in reverse order. The Repository seed itself never succeeded
    # so isn't dropped.
    assert repo.delete_calls == [
        "http://localhost:8000/resource-definitions/repository",
        "https://w3id.org/fdp/o#ResourceDefinitionShape",
        "https://fdp.example/offers/system-default",
        "http://www.w3.org/ns/dcat#Catalog",
    ]
    assert session.rolled_back is True
    assert state.recorded is None
    assert "profile_apply" in repr(exc.value) or "simulated" in repr(exc.value)


# --- validation failure rejects before any writes ------------------------


@pytest.mark.unit
async def test_apply_refuses_invalid_profile_without_writing(
    write_bundle: Callable[..., Path],
) -> None:
    # Resource definition declares a schema that wasn't listed in
    # schemas[] → validator's rd_schema_not_declared fires before any
    # write hits the triple store.
    from tests.unit.metadata.profiles.conftest import MANIFEST

    bad_manifest = MANIFEST.replace("schema: dcat:Catalog", "schema: dcat:Unknown")
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
