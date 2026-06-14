"""Tests for nous.util.url canonicalization."""

from __future__ import annotations

import pytest

from nous.util.url import canonical_url, hostname, is_storable_website


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Lowercase scheme + host.
        ("HTTPS://EXAMPLE.COM/Path", "https://example.com/Path"),
        # Default port stripped.
        ("https://example.com:443/p", "https://example.com/p"),
        ("http://example.com:80/p", "http://example.com/p"),
        # Non-default port preserved.
        ("https://example.com:8443/p", "https://example.com:8443/p"),
        # Trailing slash stripped (except for bare root).
        ("https://example.com/path/", "https://example.com/path"),
        ("https://example.com/", "https://example.com/"),
        # Fragment dropped.
        ("https://example.com/a#section", "https://example.com/a"),
        # utm_* tracking params dropped.
        (
            "https://example.com/a?utm_source=x&utm_medium=y&id=42",
            "https://example.com/a?id=42",
        ),
        # gclid / fbclid dropped.
        ("https://example.com/a?gclid=abc&id=42", "https://example.com/a?id=42"),
        ("https://example.com/a?fbclid=abc&id=42", "https://example.com/a?id=42"),
        # Combination: case + port + trailing slash + utm + fragment.
        (
            "HTTPS://Example.COM:443/Article/?utm_source=tw&id=1#top",
            "https://example.com/Article?id=1",
        ),
        # No-query input stays clean.
        ("https://example.com/foo/bar", "https://example.com/foo/bar"),
    ],
)
def test_canonical_url(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_canonical_url_dedups_tracking_variants() -> None:
    """Two URLs differing only in tracking params canonicalize identically."""
    a = "https://techcrunch.com/2026/05/26/stord-raises/?utm_source=twitter"
    b = "https://techcrunch.com/2026/05/26/stord-raises/?utm_source=newsletter&utm_medium=email"
    c = "https://techcrunch.com/2026/05/26/stord-raises/"
    assert canonical_url(a) == canonical_url(b) == canonical_url(c)


def test_canonical_url_preserves_non_tracking_query_order() -> None:
    """Non-tracking query params keep their original order."""
    raw = "https://example.com/x?z=1&a=2&utm_source=x&m=3"
    assert canonical_url(raw) == "https://example.com/x?z=1&a=2&m=3"


def test_canonical_url_strips_leading_trailing_whitespace() -> None:
    assert canonical_url("  https://example.com/  ") == "https://example.com/"


def test_hostname_basic() -> None:
    assert hostname("https://www.techcrunch.com/2026/05/26/foo") == "techcrunch.com"
    assert hostname("https://news.google.com/rss/articles/X") == "news.google.com"
    assert hostname("HTTPS://Example.COM/p") == "example.com"


def test_hostname_relative_returns_empty() -> None:
    assert hostname("/just/a/path") == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://acme.com", True),
        ("http://acme.com/path", True),
        ("acme.com", True),  # scheme-less kept; scraper adds https later
        ("//acme.com", True),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
        ("data:text/html,x", False),
        ("ftp://acme.com", False),
        ("", False),
        ("   ", False),
        (None, False),
    ],
)
def test_is_storable_website(value: str | None, expected: bool) -> None:
    assert is_storable_website(value) is expected
