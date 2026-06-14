"""Unit tests for the JS-shell → headless-browser fallback in scrape_homepages.

These exercise the decision logic in ``_resolve_content_with_fallback``
directly. They mock the HeadlessBrowserClient so no real Chromium launches —
runs offline, no DB, every CI build.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nous.pipeline.scrape_homepages import _resolve_content_with_fallback
from nous.sources.headless_browser import HeadlessBrowserClient
from nous.sources.homepage import FetchResult

# 600 chars of body text — well above the 200-char threshold.
RICH_HTML = (
    "<html><body><p>"
    + ("This is real content. " * 30)
    + "</p></body></html>"
)

# Empty SPA shell (~80 chars of cleaned text after stripping nav etc.).
JS_SHELL_HTML = '<html><head><title>App</title></head><body><div id="__next"></div></body></html>'

# What the browser fallback "renders" — significantly more text.
RENDERED_HTML = (
    "<html><body><main>"
    + ("Hydrated JS-rendered content. " * 30)
    + "</main></body></html>"
)


def _result(html: str, url: str = "https://example.com/") -> FetchResult:
    return FetchResult(url=url, status_code=200, content=html, content_type="text/html")


async def test_no_browser_client_keeps_static_content() -> None:
    """With no browser client, the static content always wins."""
    static = _result(JS_SHELL_HTML)
    content, used = await _resolve_content_with_fallback(static, None)
    assert content == JS_SHELL_HTML
    assert used is False


async def test_rich_static_content_skips_browser() -> None:
    """A page with enough body text never triggers the browser fallback."""
    static = _result(RICH_HTML)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    content, used = await _resolve_content_with_fallback(static, browser)
    assert content == RICH_HTML
    assert used is False
    browser.fetch_rendered_html.assert_not_called()


async def test_thin_static_content_uses_browser_when_richer() -> None:
    """JS shell → browser fetch returns more text → replace."""
    static = _result(JS_SHELL_HTML)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(return_value=RENDERED_HTML)
    content, used = await _resolve_content_with_fallback(static, browser)
    assert content == RENDERED_HTML
    assert used is True
    browser.fetch_rendered_html.assert_awaited_once_with("https://example.com/")


async def test_thin_static_keeps_static_when_browser_returns_none() -> None:
    """Browser failure (None) falls back gracefully — keep static."""
    static = _result(JS_SHELL_HTML)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(return_value=None)
    content, used = await _resolve_content_with_fallback(static, browser)
    assert content == JS_SHELL_HTML
    assert used is False


async def test_thin_static_keeps_static_when_browser_also_thin() -> None:
    """If browser ALSO returns thin content, keep static (no improvement)."""
    static = _result(JS_SHELL_HTML)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    # Browser returns an equally empty shell — no improvement worth swapping for.
    browser.fetch_rendered_html = AsyncMock(return_value=JS_SHELL_HTML)
    content, used = await _resolve_content_with_fallback(static, browser)
    assert content == JS_SHELL_HTML
    assert used is False


@pytest.mark.parametrize(
    "static_text_len,should_invoke_browser",
    [
        (50, True),    # below threshold
        (199, True),   # just below threshold
        (200, False),  # exactly at threshold
        (1000, False), # well above
    ],
)
async def test_threshold_boundary(
    static_text_len: int, should_invoke_browser: bool
) -> None:
    """The 200-char threshold is the trigger boundary."""
    body = "a " * (static_text_len // 2 + 1)  # rough char count
    html = f"<html><body><p>{body}</p></body></html>"
    static = _result(html)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(return_value=RENDERED_HTML)
    await _resolve_content_with_fallback(static, browser)
    if should_invoke_browser:
        browser.fetch_rendered_html.assert_called()
    else:
        browser.fetch_rendered_html.assert_not_called()


async def test_rendered_fetch_blocks_internal_url() -> None:
    """fetch_rendered_html must refuse an internal address before navigating."""
    from nous.sources.headless_browser import HeadlessBrowserClient
    from nous.util.ssrf import BlockedAddressError

    client = HeadlessBrowserClient(user_agent="nous-test test@example.com")
    # Not entered as a context manager -> no browser launched. The guard must
    # still raise, proving the check precedes any browser interaction.
    with pytest.raises(BlockedAddressError):
        await client.fetch_rendered_html("http://169.254.169.254/")
