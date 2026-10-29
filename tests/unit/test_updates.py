"""Update notifications (ADR-0028): version helpers, the GitHub source, the checker, the router."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fdpneo_server.config import UpdatesSettings
from fdpneo_server.identity.deps import require_auth
from fdpneo_server.shared.context import RequestContext
from fdpneo_server.shared.errors import TooManyRequests, register_exception_handlers
from fdpneo_server.storage.postgres.models import Base, register_all_models
from fdpneo_server.updates import (
    RELEASE_ASSET,
    GitHubReleaseSource,
    ReleaseCheckRow,
    UpdateChecker,
    build_updates_router,
    parse_version,
    satisfies,
)

API = "https://api.github.test"
SERVER_REPO = "acme/fdpneo-server"
CLIENT_REPO = "acme/fdpneo-client"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _release(
    tag: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
    with_asset: bool = True,
    repo: str = SERVER_REPO,
) -> dict[str, Any]:
    assets = (
        [
            {
                "name": RELEASE_ASSET,
                "browser_download_url": f"https://github.test/{repo}/releases/download/{tag}/{RELEASE_ASSET}",
            }
        ]
        if with_asset
        else []
    )
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.test/{repo}/releases/tag/{tag}",
        "published_at": "2026-10-29T12:00:00Z",
        "assets": assets,
    }


def _metadata(version: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema": 1,
        "component": "server",
        "version": version,
        "min_upgrade_from": "0.17.0",
        "manual_steps": False,
        "security": False,
        **extra,
    }


class FakeGitHub:
    """A tiny GitHub stand-in: releases per repo, metadata per asset URL, ETags."""

    def __init__(self) -> None:
        self.releases: dict[str, list[dict[str, Any]]] = {SERVER_REPO: [], CLIENT_REPO: []}
        self.metadata: dict[str, Any] = {}
        self.etags: dict[str, str] = {}
        self.api_calls = 0
        self.asset_calls = 0
        self.fail_repo: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{API}/repos/"):
            self.api_calls += 1
            repo = url.removeprefix(f"{API}/repos/").split("/releases")[0]
            if repo == self.fail_repo:
                return httpx.Response(503, request=request)
            etag = self.etags.get(repo)
            if etag and request.headers.get("if-none-match") == etag:
                return httpx.Response(304, request=request)
            headers = {"etag": etag} if etag else {}
            return httpx.Response(200, json=self.releases.get(repo, []), headers=headers)
        self.asset_calls += 1
        if url in self.metadata:
            return httpx.Response(200, content=json.dumps(self.metadata[url]).encode())
        return httpx.Response(404, request=request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _asset_url(tag: str, repo: str = SERVER_REPO) -> str:
    return f"https://github.test/{repo}/releases/download/{tag}/{RELEASE_ASSET}"


def _checker(
    session_factory: async_sessionmaker[AsyncSession],
    http: httpx.AsyncClient,
    *,
    enabled: bool = True,
    env_enabled: bool = True,
    running: str = "0.18.0",
    clock: Callable[[], float] | None = None,
) -> UpdateChecker:
    settings = UpdatesSettings(
        check_enabled=env_enabled,
        server_repo=SERVER_REPO,
        client_repo=CLIENT_REPO,
        github_api=API,
        startup_delay_seconds=0,
    )

    async def _enabled() -> bool:
        return enabled

    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return UpdateChecker(
        settings=settings,
        session_factory=session_factory,
        source=GitHubReleaseSource(http_client=http, api_base=API),
        enabled_provider=_enabled,
        running_version=running,
        **kwargs,
    )


# --- version helpers ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.19.0", (0, 19, 0)),
        ("v1.2.3", (1, 2, 3)),
        (" 10.0.1 ", (10, 0, 1)),
        ("0.19.0-rc1", None),
        ("0.19", None),
        ("latest", None),
        (None, None),
    ],
)
def test_parse_version(value: str | None, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("version", "requirement", "expected"),
    [
        ("0.18.0", ">=0.18.0", True),
        ("0.17.9", ">=0.18.0", False),
        ("0.19.0", "0.18.0", True),
        ("0.18.0", "==0.18.0", True),
        ("0.18.1", "==0.18.0", False),
        ("0.18.0", None, True),
        ("0.18.0", "~0.18", None),
        ("dev", ">=0.18.0", None),
    ],
)
def test_satisfies(version: str, requirement: str | None, expected: bool | None) -> None:
    assert satisfies(version, requirement) is expected


# --- GitHub source -----------------------------------------------------------


@pytest.mark.unit
async def test_source_skips_drafts_prereleases_and_odd_tags_and_sorts_newest_first() -> None:
    gh = FakeGitHub()
    gh.releases[SERVER_REPO] = [
        _release("v0.18.0", with_asset=False),
        _release("v0.20.0", draft=True),
        _release("v0.21.0-rc1", prerelease=True),
        _release("nightly"),
        _release("v0.19.0", with_asset=False),
    ]
    async with gh.client() as http:
        result = await GitHubReleaseSource(http_client=http, api_base=API).fetch(
            SERVER_REPO, etag=None, known={}
        )
    assert result is not None
    releases, _ = result
    assert [r.version for r in releases] == ["0.19.0", "0.18.0"]
    assert all(not r.metadata_present for r in releases)


@pytest.mark.unit
async def test_source_attaches_metadata_and_rejects_mismatched_versions() -> None:
    gh = FakeGitHub()
    gh.releases[SERVER_REPO] = [_release("v0.19.0"), _release("v0.18.0")]
    gh.metadata[_asset_url("v0.19.0")] = _metadata("0.19.0", manual_steps=True, security=True)
    gh.metadata[_asset_url("v0.18.0")] = _metadata("0.17.0")  # wrong version inside
    async with gh.client() as http:
        result = await GitHubReleaseSource(http_client=http, api_base=API).fetch(
            SERVER_REPO, etag=None, known={}
        )
    assert result is not None
    newest, older = result[0]
    assert newest.metadata_present
    assert newest.manual_steps is True
    assert newest.security is True
    assert newest.min_upgrade_from == "0.17.0"
    assert not older.metadata_present


@pytest.mark.unit
async def test_source_reads_client_requirement() -> None:
    gh = FakeGitHub()
    gh.releases[CLIENT_REPO] = [_release("v0.9.0", repo=CLIENT_REPO)]
    gh.metadata[_asset_url("v0.9.0", CLIENT_REPO)] = _metadata(
        "0.9.0", component="client", requires={"server": ">=0.20.0"}
    )
    async with gh.client() as http:
        result = await GitHubReleaseSource(http_client=http, api_base=API).fetch(
            CLIENT_REPO, etag=None, known={}
        )
    assert result is not None
    assert result[0][0].requires_server == ">=0.20.0"


@pytest.mark.unit
async def test_source_returns_none_when_not_modified() -> None:
    gh = FakeGitHub()
    gh.etags[SERVER_REPO] = '"abc"'
    async with gh.client() as http:
        source = GitHubReleaseSource(http_client=http, api_base=API)
        assert await source.fetch(SERVER_REPO, etag='"abc"', known={}) is None


# --- checker -----------------------------------------------------------------


@pytest.mark.unit
async def test_check_lists_newer_server_releases_and_all_client_releases(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gh = FakeGitHub()
    gh.releases[SERVER_REPO] = [_release("v0.19.0"), _release("v0.18.0"), _release("v0.17.0")]
    gh.metadata[_asset_url("v0.19.0")] = _metadata("0.19.0", manual_steps=True)
    gh.releases[CLIENT_REPO] = [
        _release("v0.8.0", repo=CLIENT_REPO),
        _release("v0.7.0", repo=CLIENT_REPO),
    ]
    async with gh.client() as http:
        checker = _checker(session_factory, http, running="0.18.0")
        assert await checker.check_if_due() is True
        view = await checker.view()
    assert view.enabled
    assert view.server_version == "0.18.0"
    assert view.latest_server == "0.19.0"
    assert [r.version for r in view.server_releases] == ["0.19.0"]
    assert view.server_releases[0].manual_steps is True
    assert view.latest_client == "0.8.0"
    assert [r.version for r in view.client_releases] == ["0.8.0", "0.7.0"]
    assert view.checked_at is not None
    assert view.error is None


@pytest.mark.unit
async def test_check_is_skipped_within_the_interval(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gh = FakeGitHub()
    async with gh.client() as http:
        checker = _checker(session_factory, http)
        assert await checker.check_if_due() is True
        calls = gh.api_calls
        assert await checker.check_if_due() is False
        assert gh.api_calls == calls
        # An old check is due again.
        async with session_factory() as session:
            row = await session.get(ReleaseCheckRow, 1)
            assert row is not None
            row.checked_at = datetime.now(UTC) - timedelta(hours=25)
            await session.commit()
        assert await checker.check_if_due() is True


@pytest.mark.unit
async def test_disabled_checker_never_contacts_github(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gh = FakeGitHub()
    async with gh.client() as http:
        by_setting = _checker(session_factory, http, enabled=False)
        assert await by_setting.check_if_due() is False
        assert (await by_setting.check_now()).enabled is False
        by_env = _checker(session_factory, http, enabled=True, env_enabled=False)
        assert await by_env.check_if_due() is False
        view = await by_env.check_now()
    assert gh.api_calls == 0
    assert view.enabled is False
    assert view.env_enabled is False


@pytest.mark.unit
async def test_metadata_is_cached_and_etag_reused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gh = FakeGitHub()
    gh.releases[SERVER_REPO] = [_release("v0.19.0")]
    gh.metadata[_asset_url("v0.19.0")] = _metadata("0.19.0")
    gh.etags[SERVER_REPO] = '"v1"'
    async with gh.client() as http:
        checker = _checker(session_factory, http)
        await checker.check_now()
        assets_after_first = gh.asset_calls
        # Second check: GitHub answers 304 for the server, so no asset download.
        checker._last_forced = None  # pyright: ignore[reportPrivateUsage]
        view = await checker.check_now()
    assert gh.asset_calls == assets_after_first
    assert view.latest_server == "0.19.0"
    assert view.server_releases[0].metadata_present


@pytest.mark.unit
async def test_failed_fetch_keeps_previous_releases_and_records_the_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gh = FakeGitHub()
    gh.releases[SERVER_REPO] = [_release("v0.19.0", with_asset=False)]
    async with gh.client() as http:
        checker = _checker(session_factory, http)
        await checker.check_now()
        gh.fail_repo = SERVER_REPO
        checker._last_forced = None  # pyright: ignore[reportPrivateUsage]
        view = await checker.check_now()
    assert view.latest_server == "0.19.0"
    assert view.error is not None
    assert view.error.startswith("server: HTTPStatusError")


@pytest.mark.unit
async def test_forced_checks_are_rate_limited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = [1000.0]
    gh = FakeGitHub()
    async with gh.client() as http:
        checker = _checker(session_factory, http, clock=lambda: now[0])
        await checker.check_now()
        with pytest.raises(TooManyRequests):
            await checker.check_now()
        now[0] += 61
        await checker.check_now()


# --- router ------------------------------------------------------------------


def _app(checker: UpdateChecker, roles: frozenset[str]) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_updates_router(checker=checker), prefix="/fdp-api")
    ctx = RequestContext(subject="subj", roles=roles, trace_id="t")
    app.dependency_overrides[require_auth] = lambda: ctx
    return app


@pytest.mark.unit
async def test_router_is_admin_only(session_factory: async_sessionmaker[AsyncSession]) -> None:
    gh = FakeGitHub()
    async with gh.client() as http:
        checker = _checker(session_factory, http, enabled=False)
        with TestClient(_app(checker, frozenset({"steward"}))) as client:
            assert client.get("/fdp-api/admin/updates").status_code == 403
            assert client.post("/fdp-api/admin/updates/check").status_code == 403
        with TestClient(_app(checker, frozenset({"admin"}))) as client:
            res = client.get("/fdp-api/admin/updates")
            assert res.status_code == 200
            body = res.json()
            assert body["enabled"] is False
            assert body["server_releases"] == []
