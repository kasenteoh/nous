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
        # Startup-news aggregators / content sites the old resolver accepted as
        # company homepages (2026-07-16 QA: helix→machinebrief, away→marketspy,
        # amiato→failory — the wrong-site descriptions + mined rounds followed).
        "machinebrief.com",
        "marketspy.com",
        "failory.com",
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
        # Business/tech press seen carrying funding coverage that the old
        # resolver stored as company "homepages" (2026-07-19: blue-origin
        # carried a nypost.com ARTICLE URL as its website, so enrichment could
        # never describe it — the helix/machinebrief class on hosts the list
        # didn't yet name). Curated to major outlets; the dated-article-path
        # check below catches the long tail of outlets generically.
        "nypost.com",
        "wsj.com",
        "nytimes.com",
        "cnbc.com",
        "cnn.com",
        "ft.com",
        "marketwatch.com",
        "barrons.com",
        "fool.com",
        "msn.com",
        "aol.com",
        "yahoo.com",
        "news.google.com",
        "theverge.com",
        "venturebeat.com",
        "geekwire.com",
        "theglobeandmail.com",
        "washingtonpost.com",
        "latimes.com",
        "theguardian.com",
        "apnews.com",
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
        # Search engines (never a company homepage; also avoids recursive
        # results when filtering DDG search candidates)
        "duckduckgo.com",
        # Image / media-CDN hosts (formerly extract_funding._IMAGE_HOSTS):
        # never a company homepage and never acceptable as a funding source.
        # Base domains only — subdomain variants (i.imgur.com, pbs.twimg.com,
        # cdn.discordapp.com, preview.redd.it) match via the suffix walk.
        "imgur.com",
        "redd.it",
        "twimg.com",
        "discordapp.com",
        "giphy.com",
        # Access-gate / infrastructure hosts: a homepage probe that redirects
        # into a Cloudflare Access login lands on
        # <team>.cloudflareaccess.com/cdn-cgi/access/login/<real-host>?…
        # — the resolver once stored that whole JWT-bearing login URL as the
        # company website (2026-07-17 QA: away → away.ai behind CF Access).
        # Never a homepage; the suffix walk catches every team subdomain.
        "cloudflareaccess.com",
    }
)

# Regex matching URL paths that are characteristic of company-listing / directory
# pages regardless of host.  Applied to the *path* portion of a parsed URL.
# Leading slash is required; the segment must be one of the known directory
# path prefixes followed by a slash or end-of-string.
DIRECTORY_PATH_RE: str = r"^/(orgs|companies|company|startups|profile)(/|$)"

_DIRECTORY_PATH_COMPILED: re.Pattern[str] = re.compile(DIRECTORY_PATH_RE)

# Infrastructure paths that are never a company homepage on ANY host: Cloudflare
# serves challenge/access/login flows under /cdn-cgi/ on the protected domain
# itself (https://away.ai/cdn-cgi/access/login?...), so a host allowlist alone
# can't catch the on-domain variant.
_INFRA_PATH_COMPILED: re.Pattern[str] = re.compile(r"^/cdn-cgi(/|$)")

# Dated-article paths (/2026/07/08/..., /2026/7/...): the near-universal news
# CMS convention. A company HOMEPAGE never lives under a dated path, so this is
# a generic never-a-homepage signal that catches article URLs on outlets the
# host list doesn't enumerate (the blue-origin case was a nypost.com article
# stored as the website; the long tail is tempo.co, techcentral.ie, ...).
#
# DELIBERATELY a separate helper, NOT folded into is_aggregator_url:
# is_aggregator_url is shared with extract_funding's funding-source junk gate,
# where dated publisher paths are exactly what legitimate funding-article
# sources look like. is_article_url must only ever gate HOMEPAGE-candidate
# surfaces (resolver, fallback re-mining, article outbound links, wrong-website
# repair selection).
_ARTICLE_PATH_COMPILED: re.Pattern[str] = re.compile(r"^/20\d{2}/\d{1,2}(/|$)")


def is_aggregator_host(host: str) -> bool:
    """Return True if *host* (a bare netloc, port/case tolerated) is in
    AGGREGATOR_HOSTS, either directly or via a subdomain suffix match
    (``pbs.twimg.com`` matches ``twimg.com``).

    Host-only variant of :func:`is_aggregator_url` — the single matching
    implementation shared with ``duckduckgo.is_aggregator`` so the two can
    never drift apart again.
    """
    host = host.lower()
    # Strip port if present
    if ":" in host:
        host = host.split(":")[0]

    # Strip leading www. before domain matching
    bare = host[4:] if host.startswith("www.") else host

    # Direct match or suffix (subdomain) match
    if bare in AGGREGATOR_HOSTS:
        return True
    parts = bare.split(".")
    return any(".".join(parts[i:]) in AGGREGATOR_HOSTS for i in range(len(parts) - 1))


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
    if is_aggregator_host(parsed.netloc):
        return True

    # Infrastructure path (Cloudflare challenge/login flows) on any host.
    if _INFRA_PATH_COMPILED.match(parsed.path):
        return True

    # Path-pattern match (directory listing path regardless of host)
    return bool(_DIRECTORY_PATH_COMPILED.match(parsed.path))


def is_article_url(url: str) -> bool:
    """Return True if *url* has a dated news-article path (``/YYYY/MM/...``) —
    never a company homepage, on any host.

    Gate HOMEPAGE-candidate surfaces only (see _ARTICLE_PATH_COMPILED's note):
    never wire this into funding-source acceptance, where dated publisher
    paths are the legitimate shape of most sources.
    """
    return bool(_ARTICLE_PATH_COMPILED.match(urlparse(url).path))
