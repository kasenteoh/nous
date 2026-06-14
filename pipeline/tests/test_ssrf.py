"""Tests for nous.util.ssrf — the outbound SSRF guard."""

from __future__ import annotations

import socket

import httpx
import pytest

import nous.util.ssrf as ssrf_module
from nous.util.ssrf import (
    BlockedAddressError,
    SsrfGuardedAsyncTransport,
    assert_public_url,
)

# --- scheme + IP-literal blocking (no DNS performed) ------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "https://[::1]/",
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped IPv6
        "http://0.0.0.0/",
        "http://100.64.0.1/",  # RFC-6598 CGNAT (not caught by is_private)
    ],
)
async def test_internal_ip_literals_blocked(url: str) -> None:
    with pytest.raises(BlockedAddressError):
        await assert_public_url(url)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/", "gopher://x/", "data:text/plain,hi"]
)
async def test_non_http_schemes_blocked(url: str) -> None:
    with pytest.raises(BlockedAddressError):
        await assert_public_url(url)


async def test_public_ip_literal_allowed() -> None:
    # 93.184.216.34 (example.com) is public; no DNS performed for a literal.
    await assert_public_url("http://93.184.216.34/")  # must not raise


async def test_missing_host_blocked() -> None:
    with pytest.raises(BlockedAddressError):
        await assert_public_url("http:///nohost")


# --- hostname resolution path (monkeypatch the resolver) --------------------


async def test_hostname_resolving_to_internal_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["169.254.169.254"]

    monkeypatch.setattr(ssrf_module, "resolve_host_ips", fake_resolve)
    with pytest.raises(BlockedAddressError):
        await assert_public_url("http://evil.example.com/")


async def test_hostname_resolving_to_public_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_module, "resolve_host_ips", fake_resolve)
    await assert_public_url("http://good.example.com/")  # must not raise


async def test_blocks_if_any_resolved_ip_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defeats the simplest DNS-rebinding: one public + one internal answer.
    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]

    monkeypatch.setattr(ssrf_module, "resolve_host_ips", fake_resolve)
    with pytest.raises(BlockedAddressError):
        await assert_public_url("http://rebind.example.com/")


async def test_unresolvable_host_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(host: str, port: int) -> list[str]:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(ssrf_module, "resolve_host_ips", fake_resolve)
    with pytest.raises(BlockedAddressError):
        await assert_public_url("http://does-not-resolve.invalid/")


# --- transport rejects before opening a socket (no network) -----------------


async def test_transport_blocks_internal_before_connect() -> None:
    transport = SsrfGuardedAsyncTransport()
    request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(BlockedAddressError):
        await transport.handle_async_request(request)
