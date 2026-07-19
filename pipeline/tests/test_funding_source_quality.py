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


def test_dated_article_path_alone_is_NOT_junk_for_funding_sources() -> None:
    """Load-bearing seam: is_article_url (dated /YYYY/MM/ paths) gates ONLY
    homepage-candidate surfaces. The funding-source junk gate must keep
    accepting dated article URLs on unlisted outlets — they are the legitimate
    shape of most round sources. If this starts failing, someone wired
    is_article_url into is_aggregator_url / _is_junk_source_url."""
    assert not _is_junk_source_url(
        "https://smalloutlet.example/2026/07/08/acme-raises-40m/"
    )


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


# ---------------------------------------------------------------------------
# Blocklist-merge drift guard (W-C.3): duckduckgo.py used to carry its own
# AGGREGATOR_DOMAINS copy, and extract_funding.py its own _IMAGE_HOSTS. Both
# now live in reject_hosts.AGGREGATOR_HOSTS — pin every host that only existed
# in one of the old copies so a future edit can't silently drop it.
# ---------------------------------------------------------------------------


def test_merged_blocklist_retains_every_former_entry() -> None:
    from nous.sources.reject_hosts import AGGREGATOR_HOSTS

    former_ddg_only = {"duckduckgo.com"}
    former_image_hosts_bases = {
        "imgur.com",  # covers i.imgur.com via suffix match
        "redd.it",  # covers i.redd.it / preview.redd.it
        "twimg.com",  # covers pbs.twimg.com
        "discordapp.com",  # covers cdn.discordapp.com
        "giphy.com",  # covers media.giphy.com
    }
    missing = (former_ddg_only | former_image_hosts_bases) - AGGREGATOR_HOSTS
    assert not missing, f"Merged blocklist lost entries: {missing}"


def test_ddg_is_aggregator_delegates_to_shared_list() -> None:
    """duckduckgo.is_aggregator and reject_hosts must agree — they were two
    hand-synced lists before W-C.3; now one delegates to the other."""
    from nous.sources.duckduckgo import is_aggregator
    from nous.sources.reject_hosts import is_aggregator_host

    for url, host in [
        ("https://duckduckgo.com/?q=acme", "duckduckgo.com"),
        ("https://www.linkedin.com/company/acme", "www.linkedin.com"),
        ("https://foo.crunchbase.com/x", "foo.crunchbase.com"),
        ("https://i.imgur.com/a.png", "i.imgur.com"),
        ("https://acme.com/", "acme.com"),
    ]:
        assert is_aggregator(url) == is_aggregator_host(host)
