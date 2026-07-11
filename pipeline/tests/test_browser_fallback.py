"""Unit tests for the JS-shell → headless-browser fallback in scrape_homepages.

These exercise the decision logic in ``_resolve_content_with_fallback``
directly. They mock the HeadlessBrowserClient so no real Chromium launches —
runs offline, no DB, every CI build.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nous.pipeline.scrape_homepages import (
    _BROWSER_FALLBACK_TEXT_THRESHOLD,
    _RESCUE_BROWSER_TEXT_THRESHOLD,
    _resolve_content_with_fallback,
)
from nous.sources.headless_browser import HeadlessBrowserClient
from nous.sources.homepage import FetchResult
from nous.util.text import extract_visible_text

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


# ---------------------------------------------------------------------------
# Husk rescue: the raised (describe-threshold) trigger for description-less
# companies — the Perplexity regression class.
# ---------------------------------------------------------------------------

# A Perplexity-shaped SPA shell: HTTP 200 whose *extracted* text (lifted SEO
# meta + a few visible chip labels; extract_visible_text strips nav/script)
# lands in the dead zone — above the near-zero shell check (200) but below
# enrich's describe threshold (700). Pre-H-1 this slipped past the fallback
# entirely: not empty enough to render, not rich enough to describe.
DEAD_ZONE_SHELL_HTML = (
    "<html><head><title>Perplexity - Where knowledge begins</title>"
    '<meta name="description" content="Ask anything and get a cited, '
    "conversational answer drawn from the live web. Follow up naturally, "
    'explore sources, and turn curiosity into understanding.">'
    '<meta property="og:description" content="An AI-powered answer engine '
    'that searches the web and responds with cited, up-to-date answers.">'
    "</head><body>"
    '<div id="__next"><div class="chips">'
    "<span>Discover</span><span>Spaces</span><span>Finance</span>"
    "<span>Travel</span><span>Academic</span><span>Pro</span>"
    "<span>Enterprise</span><span>Try asking about anything</span>"
    "</div></div></body></html>"
)


def test_dead_zone_fixture_sits_between_the_thresholds() -> None:
    """Keep the fixture honest: its visible text must fall in the dead zone
    (>= the default trigger, < the rescue trigger) or the tests below prove
    nothing."""
    shell_len = len(extract_visible_text(DEAD_ZONE_SHELL_HTML))
    assert _BROWSER_FALLBACK_TEXT_THRESHOLD <= shell_len < _RESCUE_BROWSER_TEXT_THRESHOLD


async def test_default_threshold_misses_dead_zone_shell() -> None:
    """With the default (near-zero) trigger, a dead-zone shell keeps its thin
    static content — this is the pre-H-1 behavior the rescue exists to fix."""
    static = _result(DEAD_ZONE_SHELL_HTML)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(return_value=RENDERED_HTML)
    content, used = await _resolve_content_with_fallback(static, browser)
    assert content == DEAD_ZONE_SHELL_HTML
    assert used is False
    browser.fetch_rendered_html.assert_not_called()


async def test_rescue_threshold_forces_render_for_dead_zone_shell() -> None:
    """Regression (Perplexity shape): static 200-OK shell with a few hundred
    chars of visible text + the rescue threshold → the headless render fires
    and its richer content wins."""
    static = _result(DEAD_ZONE_SHELL_HTML)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(return_value=RENDERED_HTML)
    content, used = await _resolve_content_with_fallback(
        static, browser, min_text_chars=_RESCUE_BROWSER_TEXT_THRESHOLD
    )
    assert content == RENDERED_HTML
    assert used is True
    browser.fetch_rendered_html.assert_awaited_once_with("https://example.com/")


async def test_rescue_threshold_still_skips_genuinely_rich_static() -> None:
    """A static page already above the describe threshold never pays for a
    render, even on the rescue path."""
    rich = (
        "<html><body><p>" + ("Real describable content here. " * 30) + "</p></body></html>"
    )
    assert len(extract_visible_text(rich)) >= _RESCUE_BROWSER_TEXT_THRESHOLD
    static = _result(rich)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(return_value=RENDERED_HTML)
    content, used = await _resolve_content_with_fallback(
        static, browser, min_text_chars=_RESCUE_BROWSER_TEXT_THRESHOLD
    )
    assert content == rich
    assert used is False
    browser.fetch_rendered_html.assert_not_called()


async def test_rescue_render_no_improvement_keeps_static() -> None:
    """If the render is no richer than the dead-zone static text (e.g. a WAF
    challenge page), keep the static content — never store a worse page."""
    static = _result(DEAD_ZONE_SHELL_HTML)
    browser = AsyncMock(spec=HeadlessBrowserClient)
    browser.fetch_rendered_html = AsyncMock(
        return_value="<html><body>Checking your browser</body></html>"
    )
    content, used = await _resolve_content_with_fallback(
        static, browser, min_text_chars=_RESCUE_BROWSER_TEXT_THRESHOLD
    )
    assert content == DEAD_ZONE_SHELL_HTML
    assert used is False


async def test_rendered_fetch_blocks_internal_url() -> None:
    """fetch_rendered_html must refuse an internal address before navigating."""
    from nous.sources.headless_browser import HeadlessBrowserClient
    from nous.util.ssrf import BlockedAddressError

    client = HeadlessBrowserClient(user_agent="nous-test test@example.com")
    # Not entered as a context manager -> no browser launched. The guard must
    # still raise, proving the check precedes any browser interaction.
    with pytest.raises(BlockedAddressError):
        await client.fetch_rendered_html("http://169.254.169.254/")
