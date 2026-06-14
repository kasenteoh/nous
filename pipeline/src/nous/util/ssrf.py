"""SSRF guard for all outbound pipeline fetches.

The pipeline fetches attacker-influenceable URLs — company websites scraped
from VC portfolios and article links from RSS feeds. Without a guard, a crafted
URL (or a 3xx redirect to one) can make the runner request internal-only
addresses: loopback, link-local cloud metadata (169.254.169.254), or RFC-1918
ranges. This module blocks that BEFORE a socket is opened, on every hop.

Three transports carry untrusted URLs; each wires in this guard:
- httpx      -> SsrfGuardedAsyncTransport (httpx routes every redirect hop
               through the transport, so each hop is validated).
- curl_cffi  -> assert_public_url() pre-call + manual redirect re-validation
               (see sources/homepage.py).
- Playwright -> assert_public_url() pre-goto + a context.route abort handler
               (see sources/headless_browser.py).

Residual risk (documented, accepted): a TOCTOU DNS-rebinding attacker who flips
a record between our resolution and the OS connect resolution. Eliminating it
requires pinning the validated IP into the connection; out of scope for this
threat model. We do block the multi-answer rebinding case (any internal answer
fails the whole URL).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# RFC-6598 carrier-grade NAT range. ipaddress.is_private excludes it, but some
# container/cloud networks use it for internal addressing, so block it too.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


class BlockedAddressError(Exception):
    """Raised when a URL uses a disallowed scheme or resolves to a non-public IP."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is not a routable public address."""
    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) before classifying.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_V4:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def resolve_host_ips(host: str, port: int) -> list[str]:
    """Resolve ``host`` to its IP strings via the event loop's async resolver.

    Module-level so tests can monkeypatch it without touching real DNS.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def assert_public_url(url: str) -> None:
    """Raise :class:`BlockedAddressError` unless ``url`` is http(s) to a public IP.

    Resolves the hostname and rejects if ANY resolved address is internal, so a
    DNS name pointing at 127.0.0.1 / 169.254.169.254 / 10.x is blocked too.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise BlockedAddressError(f"blocked scheme {parts.scheme!r} in {url!r}")
    host = parts.hostname
    if not host:
        raise BlockedAddressError(f"no host in {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    try:
        candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [
            ipaddress.ip_address(host)
        ]
    except ValueError:
        try:
            resolved = await resolve_host_ips(host, port)
        except OSError as exc:
            # Unresolvable host == not a reachable public address. Fail closed:
            # raise BlockedAddressError (which callers handle) instead of leaking
            # socket.gaierror, so the httpx/Playwright/curl paths surface one
            # known exception type.
            raise BlockedAddressError(
                f"could not resolve host {host!r} in {url!r}"
            ) from exc
        candidates = [ipaddress.ip_address(ip) for ip in resolved]

    for ip in candidates:
        if _is_blocked_ip(ip):
            raise BlockedAddressError(
                f"blocked internal address {ip} for host {host!r}"
            )


class SsrfGuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """httpx transport that validates every request, including each redirect hop."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await assert_public_url(str(request.url))
        return await super().handle_async_request(request)


def guarded_async_client(
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` whose every hop is SSRF-validated."""
    return httpx.AsyncClient(
        transport=SsrfGuardedAsyncTransport(),
        headers=headers,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )
