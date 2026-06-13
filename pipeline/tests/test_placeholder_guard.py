"""Unit tests for placeholder-name guard and a16z adapter filtering.

No DATABASE_URL required — these are pure unit tests covering:
- is_placeholder_name() correctness
- a16z adapter skips bracketed entries and keeps valid ones
- _domain_to_display_name() derivation helper
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.a16z import A16zAdapter
from nous.sources.vc_portfolios.base import is_placeholder_name

FIXTURES = Path(__file__).parent / "fixtures" / "vc_portfolios"
USER_AGENT = "nous-test test@example.com"


# ---------------------------------------------------------------------------
# is_placeholder_name unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        # Bracketed → placeholder (the primary guard)
        ("[untitled]", True),
        ("[TBD]", True),
        ("[stealth]", True),
        ("[Company Name]", True),
        ("[  ]", True),
        # Empty / whitespace → placeholder
        ("", True),
        ("   ", True),
        # Known literal placeholders (case-insensitive)
        # Note: "untitled" bare is a real company name so it is NOT filtered;
        # only "[untitled]" (bracketed) is. See _PLACEHOLDER_NAMES docstring.
        ("TBD", True),
        ("stealth", True),
        ("Stealth Startup", True),
        ("stealth mode", True),
        ("N/A", True),
        ("Placeholder", True),
        ("Company Name", True),
        # "unknown" and "none" were removed — too many real company names.
        # Valid names that should NOT be filtered
        ("Airbnb", False),
        ("Untitled", False),        # real company name (e.g. untitled.stream)
        ("Untitled Labs", False),   # contains "untitled" but not the whole name
        ("Acme [NY]", False),       # brackets don't surround the entire value
        ("TBD Technologies", False),
        ("11x", False),
        ("OpenAI", False),
        ("stealth.stream", False),  # not a bare placeholder
    ],
)
def test_is_placeholder_name(name: str, expected: bool) -> None:
    assert is_placeholder_name(name) is expected, (
        f"is_placeholder_name({name!r}) should be {expected}"
    )


# ---------------------------------------------------------------------------
# a16z adapter — skips [untitled] but keeps valid entries
# ---------------------------------------------------------------------------


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict[str, bytes]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for pattern, body in self._routes.items():
            if pattern in url_str:
                return httpx.Response(
                    200,
                    content=body,
                    headers={"content-type": "text/html; charset=utf-8"},
                )
        return httpx.Response(404, content=b"Not Found")


def _make_a16z_html(companies: list[dict]) -> bytes:  # type: ignore[type-arg]
    """Build a minimal a16z portfolio page with the given companies list."""
    payload = json.dumps(companies)
    html = f"""<!DOCTYPE html>
<html><head></head><body>
<script>
window.a16z_portfolio_companies = {payload};
</script>
</body></html>
"""
    return html.encode("utf-8")


async def test_a16z_adapter_skips_bracketed_name() -> None:
    """The adapter must not emit any entry whose title is "[untitled]"."""
    companies = [
        {"id": "1", "title": "[untitled]", "web": "https://untitled.stream/"},
        {"id": "2", "title": "Airbnb", "web": "https://airbnb.com/"},
    ]
    html = _make_a16z_html(companies)
    transport = _MockTransport({"a16z.com/portfolio": html, "a16z.com/robots.txt": b""})

    adapter = A16zAdapter()
    async with HomepageClient(user_agent=USER_AGENT) as client:
        client._client = httpx.AsyncClient(
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        client._robots._client = httpx.AsyncClient(  # type: ignore[union-attr]
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        entries = await adapter.fetch(client)

    names = [e.name for e in entries]
    assert "Airbnb" in names, "Valid entry 'Airbnb' should be included"
    assert "[untitled]" not in names, "Placeholder '[untitled]' should be filtered out"
    # Only the valid entry is emitted.
    assert len(entries) == 1


async def test_a16z_adapter_keeps_valid_bracketed_partial_name() -> None:
    """A name that merely CONTAINS brackets but isn't fully wrapped is kept."""
    companies = [
        # "Acme [NY]" — brackets don't wrap the entire value, not a placeholder.
        {"id": "3", "title": "Acme [NY]", "web": "https://acmeny.com/"},
    ]
    html = _make_a16z_html(companies)
    transport = _MockTransport(
        {"a16z.com/portfolio": html, "a16z.com/robots.txt": b""}
    )

    adapter = A16zAdapter()
    async with HomepageClient(user_agent=USER_AGENT) as client:
        client._client = httpx.AsyncClient(
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        client._robots._client = httpx.AsyncClient(  # type: ignore[union-attr]
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        entries = await adapter.fetch(client)

    assert len(entries) == 1
    assert entries[0].name == "Acme [NY]"


# ---------------------------------------------------------------------------
# _domain_to_display_name helper (tested via the repair module)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "website,expected",
    [
        ("https://untitled.stream/", "Untitled"),   # real company: untitled.stream
        ("https://www.acme.io/", "Acme"),
        ("https://sub.acme.co.uk/", "Sub"),
        ("https://my-company.com/", "My Company"),
        # Domains whose apex label is a bare placeholder: should return None.
        ("https://stealth.io/", None),   # "stealth" is in _PLACEHOLDER_NAMES
        ("https://placeholder.com/", None),
        # No host → None
        ("", None),
        (None, None),  # type: ignore[arg-type]
    ],
)
def test_domain_to_display_name(website: str | None, expected: str | None) -> None:
    from nous.pipeline.repair_catalog import _domain_to_display_name

    result = _domain_to_display_name(website) if website else None
    assert result == expected, (
        f"_domain_to_display_name({website!r}) → {result!r}, expected {expected!r}"
    )
