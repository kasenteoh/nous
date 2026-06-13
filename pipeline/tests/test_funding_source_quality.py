"""Pure unit tests for funding-source quality helpers (Task 2.7.1).

No DB required.  Tests the _is_junk_source_url logic by exercising it through
the module's public API — specifically by asserting the reject-list coverage
expected of the image-host additions in extract_funding.py.

We import the private helper directly since it's tested here in isolation;
the integration behaviour is covered in test_extract_funding.py.
"""

from __future__ import annotations

import pytest

# The helper is intentionally private (underscore-prefixed) but tested directly
# to keep the unit tests focused and fast.  Integration tests in
# test_extract_funding.py cover the observable DB-level behaviour.
from nous.pipeline.extract_funding import _is_junk_source_url

# ---------------------------------------------------------------------------
# Image / CDN hosts — must be rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://imgur.com/gallery/abc123",
        "https://i.imgur.com/abc123.png",
        "https://i.redd.it/abc123.jpg",
        "https://preview.redd.it/abc123.png",
        "https://pbs.twimg.com/media/abc123.jpg",
    ],
)
def test_image_hosts_are_rejected(url: str) -> None:
    assert _is_junk_source_url(url), f"Expected {url!r} to be rejected as junk"


# ---------------------------------------------------------------------------
# Aggregator / directory hosts — delegated to is_aggregator_url, spot-checked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://crunchbase.com/organization/acme",
        "https://www.crunchbase.com/organization/acme",
        "https://linkedin.com/company/acme",
        "https://pitchbook.com/profiles/company/acme",
        "https://techcrunch.com/2024/03/15/acme-raises-20m/",
    ],
)
def test_aggregator_hosts_are_rejected(url: str) -> None:
    assert _is_junk_source_url(url), f"Expected {url!r} to be rejected as aggregator"


# ---------------------------------------------------------------------------
# Company own-sites — must NOT be rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.com/",
        "https://www.acme.com/about",
        "https://acmehq.io/press",
        "https://getacme.com/funding",
        "https://webco.example/",
    ],
)
def test_own_domain_urls_are_not_rejected(url: str) -> None:
    assert not _is_junk_source_url(url), (
        f"Expected {url!r} to be accepted (company own domain)"
    )
