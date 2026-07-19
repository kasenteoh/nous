"""Mine a company's homepage from outbound links in news articles about it.

A news article we've already sourced about a company frequently links that
company's own site in-body ("<a href='https://perplexity.ai'>Perplexity</a>").
Re-fetching the *article* (not the company's Cloudflare-origin homepage) and
extracting that link routes around the 403 (ROADMAP "route around, don't
evade"); the article URL is the recorded provenance.

Two precision signals, checked per candidate link, in order:

1. **Domain match** — the link's registrable-domain label normalized-contains
   (or is contained by) the company name. "perplexity.ai" for "Perplexity" is a
   near-certain hit even when the anchor text is "here". Strongest signal.
2. **Anchor match** — the visible anchor text token-subset-matches the company
   name. Catches homepages on a domain that doesn't spell the name.

Links to aggregator/social hosts, the publishing outlet's own domain, and any
URL already in the company's ``rejected_urls`` are dropped before scoring, so a
journalist's Twitter or a competitor mention is never picked. Stored article
``raw_content`` is visible text with no ``<a href>`` left (models.py), so the
caller passes freshly-fetched article HTML here.

Pure functions only — no network — so the parse/select logic is unit-testable.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

from nous.sources.reject_hosts import is_aggregator_url, is_article_url
from nous.util.slugify import name_tokens, names_token_subset, normalize_name
from nous.util.url import canonical_domain, hostname, is_storable_website

# A company-name substring shorter than this is too collision-prone to accept as
# a domain-label match on its own ("ai" appears in a thousand domains).
_MIN_DOMAIN_LABEL_MATCH = 4


def extract_outbound_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return ``(absolute_url, anchor_text)`` for every http(s) anchor in ``html``.

    Relative hrefs are resolved against ``base_url``. Non-http schemes and
    empty/fragment/mailto/tel/js hrefs are dropped. Order and duplicates are
    preserved (the caller scores and de-dups).
    """
    tree = HTMLParser(html)
    out: list[tuple[str, str]] = []
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        text = (node.text(strip=True) or "").strip()
        out.append((absolute, text))
    return out


def _domain_label(url: str) -> str:
    """Normalized registrable-domain label without its public suffix.

    "https://get-clay.com/x" → "getclay"; "https://perplexity.ai" →
    "perplexity"; shared-hosting hosts (returns None from canonical_domain) →
    "".
    """
    domain = canonical_domain(url)
    if not domain:
        return ""
    labels = domain.split(".")
    stem = "".join(labels[:-1]) if len(labels) > 1 else domain
    return normalize_name(stem)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def select_company_link(
    links: list[tuple[str, str]],
    company_name: str,
    *,
    publisher_host: str,
    rejected_domains: frozenset[str] = frozenset(),
) -> str | None:
    """Pick the outbound link most likely to be ``company_name``'s homepage.

    ``publisher_host`` is the article's own hostname (its self-links and
    section links are excluded). ``rejected_domains`` are ``canonical_domain``
    values the company has already rejected. Returns the origin URL of the best
    candidate, or None. Domain-label matches beat anchor-only matches.
    """
    company_norm = normalize_name(company_name)
    if not name_tokens(company_name):
        return None
    pub = (publisher_host or "").lower().removeprefix("www.")

    anchor_hit: str | None = None

    for absolute, anchor in links:
        if (
            not is_storable_website(absolute)
            or is_aggregator_url(absolute)
            or is_article_url(absolute)
        ):
            continue
        host = hostname(absolute)
        if not host or host == pub or host.endswith("." + pub):
            continue
        domain = canonical_domain(absolute)
        if domain is None or domain in rejected_domains:
            continue

        label = _domain_label(absolute)
        domain_match = bool(label) and len(company_norm) >= _MIN_DOMAIN_LABEL_MATCH and (
            company_norm in label or label in company_norm
        )
        if domain_match:
            # First strong hit wins — articles list the subject's own domain.
            return _origin(absolute)

        if anchor_hit is None and anchor and names_token_subset(company_name, anchor):
            anchor_hit = _origin(absolute)

    return anchor_hit
