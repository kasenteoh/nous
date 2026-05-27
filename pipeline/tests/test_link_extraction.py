"""Unit tests for the homepage-link discoverer used by scrape-homepages.

Pure HTML/string in, list-of-URLs out. No DB, no HTTP. These run on every CI
build (no DATABASE_URL gate).
"""

from __future__ import annotations

from nous.pipeline.scrape_homepages import _extract_relevant_links


def test_picks_keyword_paths_over_noise() -> None:
    html = """
    <html><body>
      <a href="/about">About us</a>
      <a href="/product">Product</a>
      <a href="/team">Team</a>
      <a href="/contact">Contact</a>
      <a href="/blog">Blog</a>
      <a href="/somerandompage">Random</a>
    </body></html>
    """
    links = _extract_relevant_links(
        html, base_url="https://example.com/", max_links=3
    )
    assert len(links) == 3
    # /about, /product, /team all score; /contact and /blog are explicitly
    # rejected; /somerandompage has no keyword match.
    assert set(links) == {
        "https://example.com/about",
        "https://example.com/product",
        "https://example.com/team",
    }


def test_ignores_offsite_links() -> None:
    html = """
    <html><body>
      <a href="https://twitter.com/example">Twitter about</a>
      <a href="https://other-domain.com/about">External about</a>
      <a href="/about">Internal about</a>
    </body></html>
    """
    links = _extract_relevant_links(html, base_url="https://example.com/")
    assert links == ["https://example.com/about"]


def test_ignores_anchors_and_non_http_schemes() -> None:
    html = """
    <html><body>
      <a href="#about">Anchor</a>
      <a href="mailto:hello@example.com">Email about</a>
      <a href="tel:+1234567890">Phone about</a>
      <a href="javascript:alert(1)">JS about</a>
      <a href="/about">About</a>
    </body></html>
    """
    links = _extract_relevant_links(html, base_url="https://example.com/")
    assert links == ["https://example.com/about"]


def test_rejects_legal_login_and_noise_paths() -> None:
    """Paths in the reject-list are dropped even when their anchor text matches keywords."""
    html = """
    <html><body>
      <a href="/privacy">Privacy about</a>
      <a href="/terms">Terms about</a>
      <a href="/login">Login about</a>
      <a href="/careers">Careers about</a>
      <a href="/pricing">Pricing about</a>
      <a href="/blog">Blog about</a>
      <a href="/about">About</a>
    </body></html>
    """
    links = _extract_relevant_links(html, base_url="https://example.com/")
    assert links == ["https://example.com/about"]


def test_rejects_asset_file_extensions() -> None:
    html = """
    <html><body>
      <a href="/whitepaper.pdf">About PDF</a>
      <a href="/logo.png">Product logo</a>
      <a href="/about">About</a>
    </body></html>
    """
    links = _extract_relevant_links(html, base_url="https://example.com/")
    assert links == ["https://example.com/about"]


def test_dedupes_trailing_slash_variants() -> None:
    html = """
    <html><body>
      <a href="/about">About</a>
      <a href="/about/">About again</a>
    </body></html>
    """
    links = _extract_relevant_links(html, base_url="https://example.com/")
    assert len(links) == 1


def test_skips_self_link_to_homepage() -> None:
    html = """
    <html><body>
      <a href="/">Home about</a>
      <a href="/about">About</a>
    </body></html>
    """
    links = _extract_relevant_links(html, base_url="https://example.com/")
    assert links == ["https://example.com/about"]


def test_empty_html_returns_empty_list() -> None:
    assert _extract_relevant_links("", base_url="https://example.com/") == []


def test_no_relevant_links_returns_empty_list() -> None:
    html = '<html><body><a href="/randomthing">Random</a></body></html>'
    assert _extract_relevant_links(html, base_url="https://example.com/") == []


def test_respects_base_url_with_subpath() -> None:
    """Relative links resolve against the supplied base URL (which may include a path)."""
    html = '<html><body><a href="about">About</a></body></html>'
    links = _extract_relevant_links(
        html, base_url="https://example.com/intl/en/"
    )
    # urljoin('https://example.com/intl/en/', 'about') = 'https://example.com/intl/en/about'
    assert links == ["https://example.com/intl/en/about"]


def test_max_links_caps_output() -> None:
    html = """
    <html><body>
      <a href="/about">About</a>
      <a href="/product">Product</a>
      <a href="/team">Team</a>
      <a href="/company">Company</a>
      <a href="/mission">Mission</a>
    </body></html>
    """
    links = _extract_relevant_links(
        html, base_url="https://example.com/", max_links=2
    )
    assert len(links) == 2
