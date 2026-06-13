"""Canonical reject-lists for known startup directories and aggregators.

Other pipeline stages (homepage resolver, repair-catalog, funding-source quality)
import from here rather than maintaining their own copies.

AGGREGATOR_HOSTS: frozenset of bare hostnames (no scheme, no path, no www.)
    that are known startup directories, aggregator databases, or investor
    databases.  The is_aggregator_url() helper does subdomain-aware matching.

DIRECTORY_PATH_RE: compiled regex that matches URL *paths* that look like
    directory/listing paths regardless of host — e.g. /orgs/acme,
    /companies/acme-corp.  Use this to catch aggregators that aren't in
    AGGREGATOR_HOSTS yet.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Bare hostnames (without www. prefix) of known startup directories, investor
# databases, and aggregator sites.  Subdomain variants (www.foo.com, etc.) are
# matched automatically by is_aggregator_url().
AGGREGATOR_HOSTS: frozenset[str] = frozenset(
    {
        # Startup-specific directories
        "startupintros.com",
        "f6s.com",
        "getlatka.com",
        "growjo.com",
        # VC / investor databases
        "crunchbase.com",
        "pitchbook.com",
        "tracxn.com",
        "cbinsights.com",
        # Job / talent platforms (carry company profiles)
        "theorg.com",
        "wellfound.com",
        "glassdoor.com",
        # Web-analytics / competitive-intel
        "similarweb.com",
        # Y Combinator company directory (not a company's own homepage)
        "ycombinator.com",
        # Social / professional networks
        "linkedin.com",
        # News / media (mention companies but aren't their homepages)
        "techcrunch.com",
        "bloomberg.com",
        "forbes.com",
        "businessinsider.com",
        "reuters.com",
        "axios.com",
        "fortune.com",
        "wired.com",
        "theinformation.com",
        # Other data aggregators already in duckduckgo.py
        "sec.gov",
        "owler.com",
        "zoominfo.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "wikipedia.org",
        "reddit.com",
        "medium.com",
        "substack.com",
    }
)

# Regex matching URL paths that are characteristic of company-listing / directory
# pages regardless of host.  Applied to the *path* portion of a parsed URL.
# Leading slash is required; the segment must be one of the known directory
# path prefixes followed by a slash or end-of-string.
DIRECTORY_PATH_RE: str = r"^/(orgs|companies|company|startups|profile)(/|$)"

_DIRECTORY_PATH_COMPILED: re.Pattern[str] = re.compile(DIRECTORY_PATH_RE)


def is_aggregator_url(url: str) -> bool:
    """Return True if *url* is hosted on a known aggregator/directory.

    Checks:
    1. The host (or any suffix of the host split on ".") is in AGGREGATOR_HOSTS
       — catches both bare domain and www./subdomain variants.
    2. The URL path matches DIRECTORY_PATH_RE — catches listing paths on hosts
       not yet in AGGREGATOR_HOSTS.

    Examples::

        is_aggregator_url("https://startupintros.com/orgs/acme")  # True
        is_aggregator_url("https://www.crunchbase.com/organization/acme")  # True
        is_aggregator_url("https://acme.com/")  # False
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    # Strip port if present
    if ":" in host:
        host = host.split(":")[0]

    # Strip leading www. before domain matching
    bare = host[4:] if host.startswith("www.") else host

    # Direct match or suffix (subdomain) match
    if bare in AGGREGATOR_HOSTS:
        return True
    parts = bare.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in AGGREGATOR_HOSTS:
            return True

    # Path-pattern match (directory listing path regardless of host)
    return bool(_DIRECTORY_PATH_COMPILED.match(parsed.path))
