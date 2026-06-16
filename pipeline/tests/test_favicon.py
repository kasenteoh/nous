"""Tests for nous.sources.favicon — logo/favicon candidate extraction + validation.

Two layers are covered:
- ``best_logo_candidate``: a pure function over (html, base_url). No network.
- ``fetch_logo_url``: async, validates a candidate actually resolves to an
  image via an SSRF-guarded GET. Uses an httpx mock transport — no real network.
"""

from __future__ import annotations

import httpx
import pytest

from nous.sources.favicon import best_logo_candidate, fetch_logo_url

USER_AGENT = "nous-test test@example.com"


# ---------------------------------------------------------------------------
# best_logo_candidate — pure candidate extraction
# ---------------------------------------------------------------------------


def test_apple_touch_icon_is_preferred() -> None:
    """apple-touch-icon wins over a plain icon — it is a real raster logo,
    sized for home-screen tiles, and the best card thumbnail available."""
    html = """
    <html><head>
      <link rel="icon" href="/favicon-16.png" sizes="16x16">
      <link rel="apple-touch-icon" href="/apple-touch-icon.png">
    </head><body></body></html>
    """
    assert best_logo_candidate(html, "https://acme.com") == (
        "https://acme.com/apple-touch-icon.png"
    )


def test_highest_resolution_icon_wins_among_plain_icons() -> None:
    """With no apple-touch-icon, the largest declared ``sizes`` is chosen."""
    html = """
    <html><head>
      <link rel="icon" href="/small.png" sizes="16x16">
      <link rel="icon" href="/big.png" sizes="192x192">
      <link rel="icon" href="/mid.png" sizes="32x32">
    </head><body></body></html>
    """
    assert best_logo_candidate(html, "https://acme.com") == "https://acme.com/big.png"


def test_shortcut_icon_rel_is_recognized() -> None:
    """``rel="shortcut icon"`` (legacy multi-token rel) is a valid icon link."""
    html = """
    <html><head>
      <link rel="shortcut icon" href="/legacy.ico">
    </head><body></body></html>
    """
    assert best_logo_candidate(html, "https://acme.com") == "https://acme.com/legacy.ico"


def test_relative_href_resolved_against_base_url() -> None:
    """A relative href resolves against the (possibly deep) base URL's origin."""
    html = '<link rel="apple-touch-icon" href="img/logo.png">'
    assert best_logo_candidate(html, "https://acme.com/about/team") == (
        "https://acme.com/about/img/logo.png"
    )


def test_protocol_relative_href_resolved() -> None:
    """A protocol-relative ``//cdn/...`` href inherits the base scheme."""
    html = '<link rel="icon" href="//cdn.acme.com/favicon.png">'
    assert best_logo_candidate(html, "https://acme.com") == (
        "https://cdn.acme.com/favicon.png"
    )


def test_falls_back_to_favicon_ico_when_no_link_tags() -> None:
    """No icon <link> at all → /favicon.ico at the site root."""
    html = "<html><head><title>Acme</title></head><body>hi</body></html>"
    assert best_logo_candidate(html, "https://acme.com/deep/path") == (
        "https://acme.com/favicon.ico"
    )


def test_data_uri_icon_is_ignored_falls_back() -> None:
    """An inline data: URI icon cannot be hosted/fetched — ignore and fall back."""
    html = '<link rel="icon" href="data:image/png;base64,AAAA">'
    assert best_logo_candidate(html, "https://acme.com") == "https://acme.com/favicon.ico"


def test_empty_href_ignored() -> None:
    """An icon link with an empty href is skipped; falls back to /favicon.ico."""
    html = '<link rel="icon" href="">'
    assert best_logo_candidate(html, "https://acme.com") == "https://acme.com/favicon.ico"


def test_unparseable_base_url_returns_none() -> None:
    """A base URL without a usable host yields no candidate (can't build /favicon.ico)."""
    assert best_logo_candidate("<link rel='icon' href='/x.png'>", "not a url") is None


# ---------------------------------------------------------------------------
# fetch_logo_url — async validation that the candidate is really an image
# ---------------------------------------------------------------------------


def _client_with_routes(
    handler: object,
) -> httpx.AsyncClient:
    """Build an AsyncClient backed by a MockTransport using ``handler``."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


async def test_valid_image_candidate_returned() -> None:
    """A candidate that responds 200 with content-type image/* is returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 400,
            headers={"content-type": "image/png", "content-length": "408"},
        )

    html = '<link rel="apple-touch-icon" href="/logo.png">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://acme.com")
    assert url == "https://acme.com/logo.png"


async def test_non_image_content_type_rejected() -> None:
    """A candidate that resolves to text/html (e.g. an SPA 200 catch-all) is
    rejected — many sites return their app shell for an unknown /favicon.ico."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><body>not found</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    html = '<link rel="icon" href="/favicon.ico">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://acme.com")
    assert url is None


async def test_404_candidate_rejected() -> None:
    """A candidate that 404s is rejected (returns None, never raises)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    html = '<link rel="icon" href="/missing.png">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://acme.com")
    assert url is None


async def test_zero_byte_image_rejected() -> None:
    """An image/* response with no body is not a usable logo — rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"",
            headers={"content-type": "image/png", "content-length": "0"},
        )

    html = '<link rel="icon" href="/empty.png">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://acme.com")
    assert url is None


async def test_oversized_image_rejected() -> None:
    """A content-length far beyond a favicon's plausible size is rejected
    (cheap guard against fetching a multi-MB hero image by mistake)."""
    from nous.sources.favicon import _MAX_LOGO_BYTES

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\x89PNG",
            headers={
                "content-type": "image/png",
                "content-length": str(_MAX_LOGO_BYTES + 1),
            },
        )

    html = '<link rel="icon" href="/huge.png">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://acme.com")
    assert url is None


async def test_network_error_swallowed_returns_none() -> None:
    """A transport-level error during validation never propagates — None."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    html = '<link rel="icon" href="/logo.png">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://acme.com")
    assert url is None


async def test_ssrf_guard_rejects_internal_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the candidate host resolves to an internal IP, the SSRF guard blocks
    it before any image request and fetch_logo_url returns None."""
    import nous.util.ssrf as ssrf_module

    async def fake_resolve(host: str, port: int) -> list[str]:
        return ["169.254.169.254"]  # cloud-metadata; must be blocked

    monkeypatch.setattr(ssrf_module, "resolve_host_ips", fake_resolve)

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, headers={"content-type": "image/png"})

    # Candidate points at an internal-resolving host.
    html = '<link rel="icon" href="https://internal.example/favicon.png">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://internal.example")
    assert url is None
    assert called is False  # guard fired before the request


async def test_no_candidate_returns_none() -> None:
    """A base URL with no usable host yields no candidate and no request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not fetch when there is no candidate")

    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, "<html></html>", "not a url")
    assert url is None


async def test_head_then_get_fallback_when_head_disallowed() -> None:
    """Some servers 405 a HEAD; validation falls back to a ranged GET and still
    accepts a valid image."""
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405, content=b"")
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\n",
            headers={"content-type": "image/png"},
        )

    html = '<link rel="apple-touch-icon" href="/logo.png">'
    async with _client_with_routes(handler) as client:
        url = await fetch_logo_url(client, html, "https://acme.com")
    assert url == "https://acme.com/logo.png"
    assert "HEAD" in seen_methods
    assert "GET" in seen_methods
