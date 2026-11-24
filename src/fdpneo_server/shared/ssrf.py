"""Outbound-fetch SSRF guard (shared kernel).

The data provider's stream-mode download proxy dereferences a steward-supplied
``dcat:downloadURL`` server-side (security audit 2026-06-10, N-02). That URL is
attacker-influenced — anyone who can publish a distribution controls it — so
before the FDP fetches it we enforce two structural rules:

* the scheme must be ``http`` or ``https`` (no ``file:``, ``gopher:``, …);
* the host must not resolve to a private, loopback, link-local, or otherwise
  non-public address — this is what blocks the cloud-metadata / internal-service
  pivot (``http://169.254.169.254/…``, RFC1918, ``::1``, …).

The caller follows redirects manually so every hop passes back through the
guard — an allowlisted or public host that ``302``\\s inward is caught too.

The connection is pinned to the address that was checked
(:func:`pin_public_url`): the request goes to that IP, with the original host
in ``Host`` and as the TLS server name (SNI and certificate check). A host that
changes its DNS answer between the check and the connection (DNS rebinding)
therefore cannot steer the request to an internal address (security audit
2026-11-23, F-01; the FDPneo Index's outbound client does the same).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from fdpneo_server.shared.errors import UpstreamError

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


@dataclass(frozen=True, slots=True)
class PinnedUrl:
    """Where to send a request for a vetted URL.

    ``url`` has the checked address in place of the host name; ``headers`` and
    ``extensions`` keep the name for ``Host`` and for TLS (SNI and the
    certificate check). Pass all three to ``httpx``. For a URL whose host is
    already an IP literal there is nothing to pin.

    ``headers`` always carries ``Connection: close``. The connection pool is
    keyed on the URL's host, which is now an address: a connection left open
    for one name could otherwise be reused for another name at the same
    address, whose certificate was then never checked.
    """

    url: httpx.URL
    headers: dict[str, str] = field(default_factory=lambda: {"Connection": "close"})
    extensions: dict[str, str] = field(default_factory=dict[str, str])


async def pin_public_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] | None = None,
) -> PinnedUrl:
    """Vet ``url`` like :func:`assert_public_url`, and pin it to the checked address.

    Every address the host resolves to must be public; the connection goes to
    the first IPv4 one (containers often have no IPv6 route), else the first.
    """
    addresses, literal = await _vet(url, allowed_hosts)
    target = httpx.URL(url)
    if literal:
        return PinnedUrl(url=target)
    address = next((a for a in addresses if a.version == 4), addresses[0])
    headers = {"Host": target.netloc.decode("ascii"), "Connection": "close"}
    extensions = (
        {"sni_hostname": target.raw_host.decode("ascii")} if target.scheme == "https" else {}
    )
    return PinnedUrl(
        url=target.copy_with(host=str(address)), headers=headers, extensions=extensions
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

    A check only: a caller that then fetches ``url`` should use
    :func:`pin_public_url` instead, so the connection goes where the check looked.
    """
    await _vet(url, allowed_hosts)


async def _vet(url: str, allowed_hosts: frozenset[str] | None) -> tuple[list[_IpAddress], bool]:
    """The host's addresses (all public), and whether the host is an IP literal."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UpstreamError(f"download URL scheme is not permitted: {scheme or '<none>'!r}")
    host = parts.hostname
    if not host:
        raise UpstreamError("download URL has no host")
    if allowed_hosts and host not in allowed_hosts:
        raise UpstreamError("download URL host is not on the egress allow-list")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    addresses = await _resolve(host, parts.port)
    for address in addresses:
        if not _is_public_ip(address):
            raise UpstreamError("download URL resolves to a non-public address (SSRF blocked)")
    return addresses, literal


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
            raise UpstreamError(
                f"download URL resolved to an unparseable address: {ip_str}"
            ) from err
    if not addresses:
        raise UpstreamError(f"download URL host did not resolve: {host}")
    return addresses


__all__ = ["ALLOWED_SCHEMES", "PinnedUrl", "assert_public_url", "pin_public_url"]
