"""Unit tests for the outbound-fetch SSRF guard (security audit 2026-06-10, N-02).

These use literal-IP hosts so :func:`assert_public_url` short-circuits DNS and
the tests need no network.
"""

from __future__ import annotations

import ipaddress

import pytest

from fdpneo_server.shared.errors import UpstreamError
from fdpneo_server.shared.ssrf import assert_public_url, pin_public_url


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://8.8.8.8/file.csv",
        "http://1.1.1.1/",
        "https://[2606:4700:4700::1111]/x",  # public IPv6 (Cloudflare)
    ],
)
async def test_allows_public_hosts(url: str) -> None:
    await assert_public_url(url)  # does not raise


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://127.0.0.1/",  # loopback
        "http://10.0.0.5/",  # RFC1918
        "http://192.168.1.1/",  # RFC1918
        "http://172.16.0.1/",  # RFC1918
        "http://0.0.0.0/",  # unspecified
        "http://[::1]/",  # IPv6 loopback
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback smuggled via IPv6
        "http://[fe80::1]/",  # IPv6 link-local
    ],
)
async def test_blocks_non_public_addresses(url: str) -> None:
    with pytest.raises(UpstreamError, match="non-public"):
        await assert_public_url(url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://8.8.8.8/",
        "ftp://8.8.8.8/x",
        "8.8.8.8/no-scheme",
    ],
)
async def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(UpstreamError, match="scheme"):
        await assert_public_url(url)


@pytest.mark.unit
async def test_allowlist_excludes_other_public_hosts() -> None:
    # On the allow-list → fine.
    await assert_public_url("https://8.8.8.8/", allowed_hosts=frozenset({"8.8.8.8"}))
    # A different public host is rejected before any resolution.
    with pytest.raises(UpstreamError, match="allow-list"):
        await assert_public_url("https://1.1.1.1/", allowed_hosts=frozenset({"8.8.8.8"}))


# --- pinning (security audit 2026-11-23, F-01) -----------------------------


def _resolving_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[str]:
    """Answer every DNS lookup with ``addresses``; return the hosts looked up."""
    asked: list[str] = []

    async def resolve(
        host: str, _port: int | None
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        asked.append(host)
        return [ipaddress.ip_address(a) for a in addresses]

    monkeypatch.setattr("fdpneo_server.shared.ssrf._resolve", resolve)
    return asked


@pytest.mark.unit
async def test_pins_the_connection_to_the_checked_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolving_to(monkeypatch, "2606:4700:4700::1111", "93.184.216.34")
    pinned = await pin_public_url("https://files.example:8443/d1.csv?x=1")
    # The checked address (IPv4 preferred), the name kept for Host and TLS.
    assert str(pinned.url) == "https://93.184.216.34:8443/d1.csv?x=1"
    # A pinned connection is never reused for another name at that address.
    assert pinned.headers == {"Host": "files.example:8443", "Connection": "close"}
    assert pinned.extensions == {"sni_hostname": "files.example"}


@pytest.mark.unit
async def test_pins_plain_http_and_ipv6_only_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolving_to(monkeypatch, "2606:4700:4700::1111")
    pinned = await pin_public_url("http://files.example/d1.csv")
    assert str(pinned.url) == "http://[2606:4700:4700::1111]/d1.csv"
    assert pinned.headers == {"Host": "files.example", "Connection": "close"}
    assert pinned.extensions == {}  # no TLS, no server name


@pytest.mark.unit
async def test_an_ip_literal_needs_no_pinning() -> None:
    pinned = await pin_public_url("https://8.8.8.8/file.csv")
    assert (str(pinned.url), pinned.headers, pinned.extensions) == (
        "https://8.8.8.8/file.csv",
        {"Connection": "close"},
        {},
    )


@pytest.mark.unit
async def test_a_name_with_any_internal_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolving_to(monkeypatch, "93.184.216.34", "10.0.0.5")
    with pytest.raises(UpstreamError, match="non-public"):
        await pin_public_url("https://files.example/d1.csv")
