"""Unit tests for the outbound-fetch SSRF guard (security audit 2026-06-10, N-02).

These use literal-IP hosts so :func:`assert_public_url` short-circuits DNS and
the tests need no network.
"""

from __future__ import annotations

import pytest

from fdp.shared.errors import UpstreamError
from fdp.shared.ssrf import assert_public_url


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
