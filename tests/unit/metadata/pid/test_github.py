"""Tests for ``fdpneo_server.metadata.pid.github`` — the w3id.org PR automation."""

from __future__ import annotations

import httpx
import pytest
import respx

from fdpneo_server.metadata.pid.github import W3IDPublisher
from fdpneo_server.metadata.pid.w3id import build_w3id_config
from fdpneo_server.shared.errors import BadRequest

API = "https://api.github.com"
CONFIG = build_w3id_config(
    identifier_base="https://w3id.org/myfdp", serving_base="https://fdp.example.org"
)
ALLOWED = ["api.github.com", "github.com"]


def _publisher(client: httpx.AsyncClient, *, allowed: list[str] | None = None) -> W3IDPublisher:
    return W3IDPublisher(
        http_client=client,
        token="ghp_test",
        allowed_hosts=ALLOWED if allowed is None else allowed,
        fork_owner="me",
    )


@pytest.mark.unit
@respx.mock
async def test_publish_opens_pr_when_none_exists() -> None:
    respx.get(f"{API}/repos/me/w3id.org").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/repos/perma-id/w3id.org").mock(
        return_value=httpx.Response(200, json={"default_branch": "master"})
    )
    respx.get(f"{API}/repos/perma-id/w3id.org/git/ref/heads/master").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "base123"}})
    )
    respx.get(f"{API}/repos/me/w3id.org/git/ref/heads/fdp-pid-myfdp").mock(
        return_value=httpx.Response(404)
    )
    create_ref = respx.post(f"{API}/repos/me/w3id.org/git/refs").mock(
        return_value=httpx.Response(201, json={})
    )
    respx.get(url__regex=rf"{API}/repos/me/w3id.org/contents/.*").mock(
        return_value=httpx.Response(404)
    )
    put_files = respx.put(url__regex=rf"{API}/repos/me/w3id.org/contents/.*").mock(
        return_value=httpx.Response(201, json={})
    )
    respx.get(f"{API}/repos/perma-id/w3id.org/pulls").mock(
        return_value=httpx.Response(200, json=[])
    )
    open_pr = respx.post(f"{API}/repos/perma-id/w3id.org/pulls").mock(
        return_value=httpx.Response(
            201, json={"html_url": "https://github.com/perma-id/w3id.org/pull/42"}
        )
    )

    async with httpx.AsyncClient() as client:
        result = await _publisher(client).publish(CONFIG)

    assert result.created_pr is True
    assert result.pull_request_url.endswith("/pull/42")
    assert result.branch == "fdp-pid-myfdp"
    assert create_ref.called
    assert put_files.call_count == 2  # .htaccess + README.md
    assert open_pr.called


@pytest.mark.unit
@respx.mock
async def test_publish_updates_existing_pr() -> None:
    # Fork + branch already exist; an open PR is present → update, don't re-open.
    respx.get(f"{API}/repos/me/w3id.org").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/repos/perma-id/w3id.org").mock(
        return_value=httpx.Response(200, json={"default_branch": "main"})
    )
    respx.get(f"{API}/repos/perma-id/w3id.org/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "newbase"}})
    )
    respx.get(f"{API}/repos/me/w3id.org/git/ref/heads/fdp-pid-myfdp").mock(
        return_value=httpx.Response(200, json={"object": {"sha": "old"}})
    )
    patch_ref = respx.patch(f"{API}/repos/me/w3id.org/git/refs/heads/fdp-pid-myfdp").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(url__regex=rf"{API}/repos/me/w3id.org/contents/.*").mock(
        return_value=httpx.Response(200, json={"sha": "filesha"})
    )
    respx.put(url__regex=rf"{API}/repos/me/w3id.org/contents/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{API}/repos/perma-id/w3id.org/pulls").mock(
        return_value=httpx.Response(
            200, json=[{"html_url": "https://github.com/perma-id/w3id.org/pull/7"}]
        )
    )

    async with httpx.AsyncClient() as client:
        result = await _publisher(client).publish(CONFIG)

    assert result.created_pr is False
    assert result.pull_request_url.endswith("/pull/7")
    assert patch_ref.called  # branch fast-forwarded to the new base


@pytest.mark.unit
async def test_host_not_on_allowlist_is_refused() -> None:
    async with httpx.AsyncClient() as client:
        publisher = _publisher(client, allowed=["example.org"])
        with pytest.raises(BadRequest, match="not on allow-list"):
            await publisher.publish(CONFIG)
