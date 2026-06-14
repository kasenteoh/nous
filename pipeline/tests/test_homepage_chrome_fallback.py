"""Unit tests for HomepageClient's 403 → curl_cffi Chrome-impersonation fallback.

These mock the internal httpx retry path and the curl_cffi shim so the tests
run offline without hitting any network. No DB needed; runs on every build.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nous.sources.homepage import FetchResult, HomepageClient, RobotsBlockedError


def _make_response(status: int, body: bytes = b"ok") -> httpx.Response:
    req = httpx.Request("GET", "https://example.com/")
    return httpx.Response(status, content=body, request=req)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    resp = _make_response(status)
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=resp.request,
        response=resp,
    )


class _FakeChromeFetchResult:
    """Stand-in for the FetchResult returned by the Chrome-impersonation path."""

    def __init__(self, status: int, content: str = "<html>chrome</html>") -> None:
        self.status_code = status
        self.content = content
        self.url = "https://example.com/"
        self.content_type = "text/html"


@pytest.fixture
def open_client() -> HomepageClient:
    """A HomepageClient with the internal httpx/robots state stubbed so we
    don't need an event loop or real network for these unit tests.
    """
    client = HomepageClient("nous-test test@example.com")
    # Pretend we entered the context manager.
    client._client = AsyncMock(spec=httpx.AsyncClient)
    client._robots = AsyncMock()
    client._robots.is_allowed = AsyncMock(return_value=True)
    return client


async def test_fetch_uses_httpx_on_success(open_client: HomepageClient) -> None:
    """200 from httpx → no fallback fires."""
    open_client._client.get = AsyncMock(return_value=_make_response(200, b"<html>httpx</html>"))

    with patch.object(
        open_client,
        "_fetch_with_chrome_impersonation",
        new=AsyncMock(),
    ) as chrome_mock, patch.object(
        open_client,
        "_throttled_get",
        new=AsyncMock(return_value=_make_response(200, b"<html>httpx</html>")),
    ):
        result = await open_client.fetch("https://example.com/")

    assert result.status_code == 200
    assert "httpx" in result.content
    chrome_mock.assert_not_called()


async def test_fetch_falls_back_to_chrome_on_403(open_client: HomepageClient) -> None:
    """403 from httpx → Chrome impersonation kicks in and its result is returned."""
    chrome_result = FetchResult(
        url="https://example.com/",
        status_code=200,
        content="<html>chrome</html>",
        content_type="text/html",
    )

    with patch.object(
        open_client,
        "_throttled_get",
        new=AsyncMock(side_effect=_http_status_error(403)),
    ), patch.object(
        open_client,
        "_fetch_with_chrome_impersonation",
        new=AsyncMock(return_value=chrome_result),
    ) as chrome_mock:
        result = await open_client.fetch("https://example.com/")

    assert result.status_code == 200
    assert "chrome" in result.content
    chrome_mock.assert_awaited_once_with("https://example.com/")


async def test_fetch_reraises_original_when_chrome_also_fails(
    open_client: HomepageClient,
) -> None:
    """If both paths 403, the caller sees the original httpx 403 (clean metrics)."""
    with patch.object(
        open_client,
        "_throttled_get",
        new=AsyncMock(side_effect=_http_status_error(403)),
    ), patch.object(
        open_client,
        "_fetch_with_chrome_impersonation",
        new=AsyncMock(side_effect=_http_status_error(403)),
    ), pytest.raises(httpx.HTTPStatusError) as excinfo:
        await open_client.fetch("https://example.com/")

    assert excinfo.value.response.status_code == 403


async def test_fetch_does_not_fall_back_on_404(open_client: HomepageClient) -> None:
    """404 (genuine not-found) should not trigger Chrome — only 403 (blocked) does."""
    with patch.object(
        open_client,
        "_throttled_get",
        new=AsyncMock(side_effect=_http_status_error(404)),
    ), patch.object(
        open_client,
        "_fetch_with_chrome_impersonation",
        new=AsyncMock(),
    ) as chrome_mock, pytest.raises(httpx.HTTPStatusError) as excinfo:
        await open_client.fetch("https://example.com/")

    assert excinfo.value.response.status_code == 404
    chrome_mock.assert_not_called()


async def test_fetch_does_not_fall_back_on_500(open_client: HomepageClient) -> None:
    """5xx (server error) doesn't trigger Chrome — fallback is specifically for WAF blocks."""
    with patch.object(
        open_client,
        "_throttled_get",
        new=AsyncMock(side_effect=_http_status_error(500)),
    ), patch.object(
        open_client,
        "_fetch_with_chrome_impersonation",
        new=AsyncMock(),
    ) as chrome_mock, pytest.raises(httpx.HTTPStatusError):
        await open_client.fetch("https://example.com/")

    chrome_mock.assert_not_called()


async def test_robots_check_runs_before_fallback(open_client: HomepageClient) -> None:
    """A robots-blocked URL never reaches httpx OR the Chrome fallback."""
    open_client._robots.is_allowed = AsyncMock(return_value=False)

    with patch.object(
        open_client,
        "_throttled_get",
        new=AsyncMock(),
    ) as get_mock, patch.object(
        open_client,
        "_fetch_with_chrome_impersonation",
        new=AsyncMock(),
    ) as chrome_mock, pytest.raises(RobotsBlockedError):
        await open_client.fetch("https://example.com/")

    get_mock.assert_not_called()
    chrome_mock.assert_not_called()


async def test_fetch_propagates_network_error_without_fallback(
    open_client: HomepageClient,
) -> None:
    """A network error from httpx (not an HTTP status error) doesn't trigger the fallback."""
    with patch.object(
        open_client,
        "_throttled_get",
        new=AsyncMock(side_effect=httpx.ConnectError("dns failed")),
    ), patch.object(
        open_client,
        "_fetch_with_chrome_impersonation",
        new=AsyncMock(),
    ) as chrome_mock, pytest.raises(httpx.ConnectError):
        await open_client.fetch("https://example.com/")

    chrome_mock.assert_not_called()


async def test_chrome_fallback_blocks_internal_url() -> None:
    """The curl_cffi fallback must refuse an internal address before fetching."""
    from nous.sources.homepage import HomepageClient
    from nous.util.ssrf import BlockedAddressError

    client = HomepageClient(user_agent="nous-test test@example.com")
    async with client:
        with pytest.raises(BlockedAddressError):
            await client._fetch_with_chrome_impersonation(
                "http://169.254.169.254/latest/meta-data/"
            )


class _FakeCurlResponse:
    """Minimal stand-in for a curl_cffi response: an endless 302 redirect."""

    def __init__(self, url: str) -> None:
        self.status_code = 302
        # Always points elsewhere so the manual redirect loop keeps going until
        # it hits _MAX_FALLBACK_REDIRECTS.
        self.headers = {"location": "/next", "content-type": "text/html"}
        self.content = b"<html>redirecting</html>"
        self.text = "<html>redirecting</html>"
        self.url = url


class _FakeCurlSession:
    """Async-context-manager session whose .get() always 302-redirects."""

    async def __aenter__(self) -> _FakeCurlSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> _FakeCurlResponse:
        return _FakeCurlResponse(url)


async def test_chrome_fallback_raises_when_redirect_cap_hit(
    open_client: HomepageClient,
) -> None:
    """A redirect chain that never resolves must raise, not return the 3xx.

    Regression guard for the SSRF redirect-cap fix: when the manual redirect
    loop exhausts _MAX_FALLBACK_REDIRECTS while the response is still a 3xx,
    the method must signal a failed fetch (httpx.RequestError) rather than hand
    a redirect response back to the caller as if it were a page.
    """
    # Neutralize the SSRF check so the synthetic redirect targets don't hit DNS;
    # this test is about the redirect-cap branch, which test_ssrf.py guards.
    with patch(
        "nous.sources.homepage.assert_public_url",
        new=AsyncMock(return_value=None),
    ), patch(
        "curl_cffi.requests.AsyncSession",
        new=lambda *a, **k: _FakeCurlSession(),
    ), pytest.raises(httpx.RequestError):
        await open_client._fetch_with_chrome_impersonation("https://example.com/")
