"""Unit tests for the outbound Index ping (task 8.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from fdpneo_server.config import IndexSettings
from fdpneo_server.metadata.events import RecordCreated
from fdpneo_server.metadata.index_ping import IndexPinger, ping_indexes
from fdpneo_server.shared.events import EventBus

pytestmark = pytest.mark.unit

CLIENT = "https://fdp.example"


def _settings(**over: object) -> IndexSettings:
    return IndexSettings(_env_file=None, **over)  # type: ignore[arg-type]


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _ok(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(204)


# --- ping_indexes -----------------------------------------------------------


async def test_ping_posts_clienturl_to_index_root_and_accepts_204() -> None:
    captured: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), json.loads(request.content.decode())))
        return httpx.Response(204)

    async with _client(handler) as client:
        results = await ping_indexes(client, client_url=CLIENT, targets=["https://idx.example"])

    # Reference wire protocol: POST {index}/ with {"clientUrl": ...}.
    assert captured == [("https://idx.example/", {"clientUrl": CLIENT})]
    assert results[0].ok and results[0].status == 204


async def test_ping_reports_failures_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "limited" in str(request.url):
            return httpx.Response(429)  # rate-limited
        raise httpx.ConnectError("unreachable")

    async with _client(handler) as client:
        results = await ping_indexes(
            client, client_url=CLIENT, targets=["https://limited.example", "https://down.example"]
        )

    assert results[0].status == 429 and not results[0].ok
    assert results[1].status is None and not results[1].ok  # transport error captured
    assert len(results) == 2  # one unreachable index does not stop the other


# --- settings ---------------------------------------------------------------


def test_targets_parse_and_enabled() -> None:
    s = _settings(ping_targets="https://a.example, https://b.example/ ")
    assert s.targets == ["https://a.example", "https://b.example"]  # trimmed + slash-stripped
    assert s.enabled
    assert not _settings(ping_targets="").enabled


# --- IndexPinger ------------------------------------------------------------


async def test_pinger_disabled_without_targets() -> None:
    bus = EventBus()
    async with _client(_ok) as client:
        pinger = IndexPinger(
            settings=_settings(ping_targets=""), client_url=CLIENT, http_client=client
        )
        pinger.start(bus)
        assert bus.subscriber_count(RecordCreated) == 0
        await pinger.stop()  # idempotent no-op


async def test_pinger_start_subscribes_and_stop_unsubscribes() -> None:
    bus = EventBus()
    settings = _settings(ping_targets="https://idx.example", ping_in_process=False)
    async with _client(_ok) as client:
        pinger = IndexPinger(settings=settings, client_url=CLIENT, http_client=client)
        pinger.start(bus)
        assert bus.subscriber_count(RecordCreated) == 1
        await pinger.stop()
        assert bus.subscriber_count(RecordCreated) == 0


async def test_on_change_is_throttled_by_min_interval() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(204)

    settings = _settings(
        ping_targets="https://idx.example",
        ping_in_process=False,
        ping_min_interval_seconds=3600,
    )
    event = RecordCreated(
        record_iri=f"{CLIENT}/catalog/c1", subject=None, etag="e", timestamp=datetime.now(UTC)
    )
    async with _client(handler) as client:
        pinger = IndexPinger(settings=settings, client_url=CLIENT, http_client=client)
        await pinger.ping_now("first")  # count == 1, sets the throttle baseline
        await pinger._on_change(event)  # within the 1h window → throttled, no ping
    assert count == 1


async def test_client_url_override() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode()))
        return httpx.Response(204)

    settings = _settings(
        ping_targets="https://idx.example",
        ping_in_process=False,
        ping_client_url="https://public.example/fdp",
    )
    async with _client(handler) as client:
        pinger = IndexPinger(settings=settings, client_url=CLIENT, http_client=client)
        await pinger.ping_now()
    assert captured == [{"clientUrl": "https://public.example/fdp"}]


# --- runtime targets provider (ADR-0025) --------------------------------------


async def test_pinger_with_provider_starts_even_when_env_targets_empty() -> None:
    """The zero-targets-at-boot deployment must still subscribe and loop: the
    provider is re-read per ping, so the first admin-added target starts
    announcing with NO restart."""
    bus = EventBus()

    async def provider() -> list[str]:
        return []

    async with _client(_ok) as client:
        pinger = IndexPinger(
            settings=_settings(ping_targets="", ping_in_process=False),
            client_url=CLIENT,
            http_client=client,
            targets_provider=provider,
        )
        pinger.start(bus)
        assert bus.subscriber_count(RecordCreated) == 1
        await pinger.stop()


async def test_ping_now_uses_provider_targets() -> None:
    pinged: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pinged.append(str(request.url))
        return httpx.Response(204)

    async def provider() -> list[str]:
        return ["https://runtime.example"]

    async with _client(handler) as client:
        pinger = IndexPinger(
            settings=_settings(ping_targets="https://env-ignored.example"),
            client_url=CLIENT,
            http_client=client,
            targets_provider=provider,
        )
        results = await pinger.ping_now()
    assert pinged == ["https://runtime.example/"]
    assert [r.target for r in results] == ["https://runtime.example"]


async def test_empty_provider_ping_is_noop_and_does_not_arm_throttle() -> None:
    """A scheduled empty run must not arm the on-change throttle — otherwise the
    first record change right after adding the deployment's first target would
    be silently swallowed."""
    targets: list[str] = []
    pinged: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pinged.append(str(request.url))
        return httpx.Response(204)

    async def provider() -> list[str]:
        return list(targets)

    event = RecordCreated(
        record_iri=f"{CLIENT}/catalog/c1", subject=None, etag="e", timestamp=datetime.now(UTC)
    )
    async with _client(handler) as client:
        pinger = IndexPinger(
            settings=_settings(ping_targets="", ping_min_interval_seconds=3600),
            client_url=CLIENT,
            http_client=client,
            targets_provider=provider,
        )
        assert await pinger.ping_now("scheduled") == []  # empty: no-op
        targets.append("https://idx.example")
        await pinger._on_change(event)  # not throttled by the empty run
    assert pinged == ["https://idx.example/"]


async def test_on_results_hook_invoked_and_errors_swallowed() -> None:
    seen: list[list[str]] = []
    calls = 0

    async def hook(results: list[object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("bookkeeping down")
        seen.append([r.target for r in results])  # type: ignore[attr-defined]

    async def provider() -> list[str]:
        return ["https://idx.example"]

    async with _client(_ok) as client:
        pinger = IndexPinger(
            settings=_settings(ping_targets="", ping_min_interval_seconds=0),
            client_url=CLIENT,
            http_client=client,
            targets_provider=provider,
            on_results=hook,  # type: ignore[arg-type]
        )
        first = await pinger.ping_now()  # hook raises — swallowed
        second = await pinger.ping_now()  # hook records
    assert [r.ok for r in first] == [True]
    assert [r.ok for r in second] == [True]
    assert seen == [["https://idx.example"]]
