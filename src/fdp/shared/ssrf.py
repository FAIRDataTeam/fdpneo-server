"""Outbound-fetch SSRF guard (shared kernel).

The data provider's stream-mode download proxy dereferences a steward-supplied
``dcat:downloadURL`` server-side (security audit 2026-06-10, N-02). That URL is
attacker-influenced — anyone who can publish a distribution controls it — so
before the FDP fetches it we enforce two structural rules:

* the scheme must be ``http`` or ``https`` (no ``file:``, ``gopher:``, …);
* the host must not resolve to a private, loopback, link-local, or otherwise
  non-public address — this is what blocks the cloud-metadata / internal-service
  pivot (``http://169.254.169.254/…``, RFC1918, ``::1``, …).

The caller follows redirects manually so every hop passes back through
:func:`assert_public_url` — an allowlisted or public host that ``302``\\s inward
is caught too.

**Residual (documented):** validation resolves DNS and then httpx resolves it
again when it connects, so a TOCTOU / DNS-rebinding window remains. For the
highest assurance, lock the proxy down with an egress allow-list
(``DataSettings.allowed_download_hosts``) or an egress proxy. Stream mode is
opt-in and off by default, which bounds the exposure.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from fdp.shared.errors import UpstreamError

ALLOWED_SCHEMES = frozenset({"http", "https"})

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _is_public_ip(ip: _IpAddress) -> bool:
    """True iff ``ip`` is a globally routable, non-special address.

    Conservative: anything private/loopback/link-local/multicast/reserved/
    unspecified is non-public. IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is unwrapped
    so an attacker can't smuggle a private v4 address through a v6 literal.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public_ip(mapped)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def assert_public_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] | None = None,
) -> None:
    """Reject ``url`` unless it is an http/https URL resolving to a public host.

    Raises :class:`UpstreamError` (502 — the URL is server-supplied metadata,
    not the caller's request) when the scheme is disallowed, the host is
    missing, a non-empty ``allowed_hosts`` set excludes the host, the host fails
    to resolve, or *any* resolved address is non-public.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UpstreamError(f"download URL scheme is not permitted: {scheme or '<none>'!r}")
    host = parts.hostname
    if not host:
        raise UpstreamError("download URL has no host")
    if allowed_hosts and host not in allowed_hosts:
        raise UpstreamError("download URL host is not on the egress allow-list")
    for address in await _resolve(host, parts.port):
        if not _is_public_ip(address):
            raise UpstreamError(
                "download URL resolves to a non-public address (SSRF blocked)"
            )


async def _resolve(host: str, port: int | None) -> list[_IpAddress]:
    """Resolve ``host`` to the IPs httpx might connect to.

    A literal IP host short-circuits DNS. Name resolution runs in the loop's
    executor so it never blocks the request path. An unresolvable host or an
    address we cannot parse is treated as a hard failure (raise), never as
    "public by default".
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port or 0, type=socket.SOCK_STREAM)
    except socket.gaierror as err:
        raise UpstreamError(f"download URL host could not be resolved: {host}") from err
    addresses: list[_IpAddress] = []
    seen: set[str] = set()
    for info in infos:
        ip_str = str(info[4][0]).split("%", 1)[0]  # drop any IPv6 scope id
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            addresses.append(ipaddress.ip_address(ip_str))
        except ValueError as err:
            raise UpstreamError(f"download URL resolved to an unparseable address: {ip_str}") from err
    if not addresses:
        raise UpstreamError(f"download URL host did not resolve: {host}")
    return addresses


__all__ = ["ALLOWED_SCHEMES", "assert_public_url"]
