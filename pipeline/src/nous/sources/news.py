"""Async news ingestion: Google News RSS + article body fetcher.

Mirrors the discipline of ``sources/homepage.py``:

- Per-domain 1 req/sec throttle (spec §3.2 + §11), shared process-wide via
  ``nous.sources._http`` so it cooperates with every other client on a host.
- robots.txt checked on every fetch via RobotsCache.
- Tenacity retries on 429 / 5xx / timeouts (shared policy in ``_http``).
- User-Agent identifies nous on every request — reuse SEC_USER_AGENT site-wide.

Boundaries:

- ``NewsArticleResult`` is the Pydantic model crossing the source/pipeline
  boundary. The RSS adapter returns "shallow" results (no body); the article
  body is fetched lazily via ``NewsClient.fetch_article_body`` so we can
  filter on title/snippet before incurring the per-article HTTP cost.
- ``fetch_article_body`` returns ``None`` on robots-block, 4xx, 5xx, or when
  the extracted visible text is below MIN_BODY_CHARS — anything below that
  threshold is almost certainly a redirect interstitial or a paywall stub.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote_plus

import feedparser
import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser

from nous.sources._http import DomainThrottle, ThrottledHTTPClient
from nous.sources.robots import RobotsBlockedError, RobotsCache
from nous.util.slugify import strip_corporate_suffix
from nous.util.ssrf import BlockedAddressError, guarded_async_client
from nous.util.url import canonical_url, hostname

# Re-export so callers that did ``from nous.sources.news import RobotsBlockedError``
# continue to work. The canonical definition lives in ``nous.sources.robots``.
__all__ = [
    "FUNDING_KEYWORDS",
    "MIN_BODY_CHARS",
    "RELEVANCE_BODY_PORTION_CHARS",
    "NewsArticleResult",
    "NewsClient",
    "ResolvedArticle",
    "RobotsBlockedError",
    "article_mentions_company",
]

logger = logging.getLogger(__name__)

# Funding-signal keywords (spec §5.5). Matched case-insensitively against the
# combined title + snippet of each RSS entry. The list is intentionally
# conservative — broader phrasing would let too much commentary through.
FUNDING_KEYWORDS: tuple[str, ...] = (
    "raised",
    "raises",
    "funding",
    "seed",
    "series a",
    "series b",
    "series c",
    "series d",
    "series e",
    "valuation",
    "closes",
    "led by",
)

# Below this size in cleaned-text chars, the fetched page is almost certainly
# a paywall stub, JS-only shell, or redirect interstitial — not useful as
# input to the funding-extraction LLM call.
MIN_BODY_CHARS: int = 500

# HTML tags whose contents add noise to article text extraction. We strip
# these subtrees before reading visible text. Order matters only for
# readability; ``decompose`` is idempotent.
_NOISE_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "aside",
    "iframe",
    "noscript",
    "form",
    "svg",
)

_WHITESPACE_RE = re.compile(r"\s+")

GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"

# The Google-News host. RSS <link>s point at news.google.com/rss/articles/...
# redirects; ``resolve_article`` treats a response still on this host (after
# following redirects) as the consent interstitial — i.e. no real article.
_GOOGLE_NEWS_HOST = "news.google.com"

# Feed-syndication surfaces: endpoints a site *publishes* for programmatic
# readers (RSS/Atom) and serves with HTTP 200 to identified clients, even
# though the site's robots.txt ``Disallow: /`` blocks its *interactive* crawl
# surface. Google News is the canonical case — news.google.com/robots.txt
# disallows ``/`` for ``*`` with an allow-list that omits ``/rss``, yet
# /rss/search returns a valid 200 feed and the spec (nous-technical-spec.md
# §5.5) sanctions this exact URL for funding discovery. Honoring robots.txt
# literally here means *every* per-company Google News query silently returns
# nothing — the feed is unreachable despite being designed for exactly this.
#
# We treat these prefixes as exempt from the robots gate ONLY: the per-domain
# 1 req/sec throttle and our identifying User-Agent still apply on every fetch.
# Keep this list as narrow as possible — it is a deliberate, audited exception
# to the project's robots discipline, not a general bypass.
_ROBOTS_EXEMPT_PREFIXES: tuple[str, ...] = ("https://news.google.com/rss/",)


def _is_robots_exempt(url: str) -> bool:
    """True if ``url`` is a published feed surface exempt from the robots gate."""
    return url.startswith(_ROBOTS_EXEMPT_PREFIXES)


class NewsArticleResult(BaseModel):
    """Shallow news article record from an RSS feed.

    ``raw_content`` holds the RSS snippet / summary, not the fetched body —
    body fetching is a separate step (``NewsClient.fetch_article_body``)
    because most RSS hits don't survive the keyword filter.
    """

    url: str  # canonical
    title: str
    source: str  # hostname (e.g. "techcrunch.com")
    published_date: date | None
    raw_content: str


class ResolvedArticle(BaseModel):
    """A Google-News redirect resolved to its real publisher article.

    Returned by ``NewsClient.resolve_article`` when a Google-News RSS link's
    opaque redirect successfully chases through to a publisher page with a
    real, robots-allowed, non-thin article body. The pipeline stores ``body``
    as the article content (so the funding-extraction LLM sees full text, not
    the thin RSS snippet) and ``url``/``source`` as the destination publisher
    for attribution + dedup.
    """

    url: str  # the final publisher URL after the redirect chain
    source: str  # publisher hostname (e.g. "reuters.com")
    body: str  # cleaned visible article text (>= MIN_BODY_CHARS)


def _compile_keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Compile ``keywords`` into a single word-boundary alternation regex.

    Substring matching produced a live false-positive class (W-D): an LLM-evals
    piece was kept because "e**valuation**s" contains "valuation" — likewise
    "praised"/"appraises" contain "raised"/"raises", "encloses" contains
    "closes", and "misled by" contains "led by". Each keyword therefore matches
    only between ``\\b`` word boundaries. Multi-word keywords ("series a",
    "led by") tolerate any run of whitespace OR hyphens between their words, so
    "Series-A round" and a line-wrapped "led\\nby" still hit.

    Keywords are lowercase by convention (the matcher lowercases its input);
    all begin and end in word characters, which ``\\b`` relies on.
    """
    alternatives = (
        r"[\s\-]+".join(re.escape(word) for word in keyword.split())
        for keyword in keywords
    )
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b")


_FUNDING_KEYWORD_RE = _compile_keyword_pattern(FUNDING_KEYWORDS)


def _matches_funding_keyword(text: str) -> bool:
    """Case-insensitive whole-word match against FUNDING_KEYWORDS.

    Whole-word (not substring): see :func:`_compile_keyword_pattern` for the
    false-positive class this guards against.
    """
    return _FUNDING_KEYWORD_RE.search(text.lower()) is not None


# How much of a resolved/fetched article body we scan for the company name when
# the headline alone doesn't contain it. The lede (first paragraph or two) names
# the funded company; scanning the whole body would re-admit the false positives
# we're guarding against (a generic word like "ramp" appearing incidentally deep
# in an unrelated piece).
RELEVANCE_BODY_PORTION_CHARS: int = 600

# Common English words that double as single-word startup names (e.g. the
# "Aardvark" biotech). A Google News query of ``"<word>" funding`` for any of
# these matches a flood of unrelated articles that merely use the word, so a
# name built from one of these needs the *full* name phrase in the headline (or
# a funding-flavored headline plus a body mention) before we attribute the
# article. The single-token / <=2-token rule below already makes every short
# name strict; this set additionally hardens longer names whose head token is a
# generic word. Deliberately small and hand-curated — it is a false-positive
# guard, not a dictionary; unknown short names are caught by the token-count
# rule regardless.
_COMMON_NAME_WORDS: frozenset[str] = frozenset(
    {
        "aardvark",
        "anchor",
        "apple",
        "away",
        "arc",
        "atom",
        "beam",
        "bench",
        "block",
        "bolt",
        "brave",
        "bridge",
        "cake",
        "canvas",
        "cargo",
        "chime",
        "clear",
        "cloud",
        "coda",
        "compass",
        "cricket",
        "current",
        "dash",
        "drift",
        "echo",
        "ember",
        "fast",
        "flow",
        "forge",
        "front",
        "glow",
        "grid",
        "harvest",
        "hive",
        "honey",
        "ivy",
        "jet",
        "lattice",
        "leap",
        "lemon",
        "level",
        "lime",
        "loop",
        "mint",
        "monarch",
        "notion",
        "oak",
        "orbit",
        "otter",
        "owl",
        "pace",
        "panda",
        "patch",
        "pepper",
        "pilot",
        "pinecone",
        "plaid",
        "pulse",
        "ramp",
        "raven",
        "ripple",
        "river",
        "rocket",
        "root",
        "scale",
        "shield",
        "slack",
        "slate",
        "spark",
        "splash",
        "sprout",
        "square",
        "stack",
        "stripe",
        "summit",
        "swift",
        "tide",
        "torch",
        "vault",
        "wave",
        "wren",
        "zest",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase, fold to alphanumerics, split into tokens for phrase matching.

    Tokenizing both sides (name and text) and matching on a contiguous token
    *sub-sequence* — rather than a substring — avoids boundary false positives
    ("Ramp" must not match inside "cRAMPed") while staying punctuation/spacing
    insensitive ("Acme, Inc." vs "Acme Inc" vs "acme").
    """
    return _TOKEN_RE.findall(text.lower())


def _phrase_in_tokens(needle: list[str], haystack: list[str]) -> bool:
    """True if ``needle`` appears as a contiguous sub-sequence of ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return False
    first = needle[0]
    width = len(needle)
    return any(
        tok == first and haystack[i : i + width] == needle
        for i, tok in enumerate(haystack)
    )


def _company_name_tokens(name: str) -> list[str]:
    """Tokens of a company name with its corporate suffix stripped.

    "Aardvark Therapeutics, Inc." -> ["aardvark", "therapeutics"]; "Acme Inc"
    -> ["acme"]. Suffix-stripping keeps the token count (and the short-name
    riskiness test) about the *distinctive* part of the name, not boilerplate.
    """
    return _tokenize(strip_corporate_suffix(name))


# Tokens that, adjacent to a single-common-word company name, mark it as the
# SUBJECT of a funding sentence rather than an incidental use of the word.
# "Away raises $50M" / "travel startup Away…" attribute; "diversify away from
# China will need funding" / "take funding away from schools" do not (2026-07
# QA: the "Away" luggage brand collected a timeline of articles that merely
# used the word). Following-verbs and preceding-markers are checked on the
# token stream, so case and punctuation never matter (title-case headlines
# capitalize every word, which defeats a case-sensitivity rule instead).
_FUNDING_VERBS_AFTER: frozenset[str] = frozenset(
    {
        "raises",
        "raised",
        "raise",
        "raising",
        "secures",
        "secured",
        "lands",
        "landed",
        "closes",
        "closed",
        "nabs",
        "nabbed",
        "banks",
        "announces",
        "announced",
        "gets",
        "hits",
        "valued",
        "reaches",
        "scores",
        "adds",
        "attracts",
    }
)
_COMPANY_MARKERS_BEFORE: frozenset[str] = frozenset(
    {
        "startup",
        "startups",
        "company",
        "brand",
        "maker",
        "firm",
        "app",
        "platform",
        "unicorn",
    }
)


def _common_word_name_in_context(token: str, haystack: list[str]) -> bool:
    """True when ``token`` appears as the SUBJECT of a funding phrase.

    For a company named by one bare dictionary word ("Away", "Clear"), a
    whole-token match is meaningless — every article using the word matches.
    Accepted occurrence shapes (tokenized, so punctuation/case-free):

    - a funding verb within the next TWO tokens — "Away raises …" and
      "Aardvark Therapeutics raises …" (the tracked name may be a prefix of
      the article's fuller name);
    - a company marker immediately before — "travel startup Away …";
    - the appositive shape — "Ramp, the corporate card startup, announced":
      NAME + "the" + a marker within the next three tokens.

    "diversify away from China will need funding" and title-case "Take
    Funding Away From Jeffco Schools" match none of these.
    """
    for i, tok in enumerate(haystack):
        if tok != token:
            continue
        if any(t in _FUNDING_VERBS_AFTER for t in haystack[i + 1 : i + 3]):
            return True
        if i > 0 and haystack[i - 1] in _COMPANY_MARKERS_BEFORE:
            return True
        if (
            i + 1 < len(haystack)
            and haystack[i + 1] == "the"
            and any(t in _COMPANY_MARKERS_BEFORE for t in haystack[i + 2 : i + 5])
        ):
            return True
    return False


def article_mentions_company(
    company_name: str,
    title: str,
    *,
    snippet: str = "",
    body: str | None = None,
) -> bool:
    """Relevance guard for the per-company Google News path.

    Google News ranks ``"<name>" funding`` loosely, so for generic or
    common-word company names it returns articles that merely contain the word
    — e.g. the "Aardvark" biotech matched a PBS-funding story, a rugby
    fundraiser, and a day-care-funding piece, none about the company. This
    requires the company name to *actually appear* (as a whole-token phrase)
    before an article is attributed, biased toward dropping borderline matches.

    Tiers (strictness rises as the name gets more collision-prone):

    - Distinctive names (>= 3 tokens after suffix-strip, head token not a common
      word): keep when the full name phrase is in the title OR the first
      ``RELEVANCE_BODY_PORTION_CHARS`` of the resolved body. Long names rarely
      collide, so a lede mention is trustworthy.
    - Risky names (<= 2 tokens, or head token a common dictionary word): the
      headline is the strongest curated signal — keep when the full name phrase
      is in the *title*. A body-only mention is trusted only when the *title*
      itself is funding-flavored (so a stray "ramp"/"scale" deep in an unrelated
      article does not qualify).

    ``snippet`` (the RSS summary) is accepted for symmetry / future use but is
    intentionally NOT treated as strong as the title — Google News snippets are
    often a generic sentence that repeats the query terms.
    """
    name_tokens = _company_name_tokens(company_name)
    if not name_tokens:
        # No distinctive token to anchor on (e.g. a name that was all
        # punctuation/suffix). Fail closed — better to drop than misattribute.
        return False

    title_tokens = _tokenize(title)

    # A ONE-token dictionary-word name ("Away", "Clear") is the maximally
    # collision-prone case: a bare whole-token match attributes every article
    # that merely USES the word ("diversify away from China will need
    # funding"). Require the word to be the subject of a funding phrase —
    # in the title, or in the lede when the title is funding-flavored.
    if len(name_tokens) == 1 and name_tokens[0] in _COMMON_NAME_WORDS:
        word = name_tokens[0]
        if _common_word_name_in_context(word, title_tokens):
            return True
        if body and _matches_funding_keyword(title):
            lede_tokens = _tokenize(body[:RELEVANCE_BODY_PORTION_CHARS])
            return _common_word_name_in_context(word, lede_tokens)
        return False

    title_has = _phrase_in_tokens(name_tokens, title_tokens)

    body_has = False
    if body:
        body_has = _phrase_in_tokens(
            name_tokens, _tokenize(body[:RELEVANCE_BODY_PORTION_CHARS])
        )

    risky = len(name_tokens) <= 2 or name_tokens[0] in _COMMON_NAME_WORDS
    if not risky:
        return title_has or body_has

    if title_has:
        return True
    # Risky name, name only in the body: require a funding-flavored headline so
    # an incidental body mention of a generic word doesn't get attributed.
    return body_has and _matches_funding_keyword(title)


def _strip_html(text: str) -> str:
    """Strip HTML tags from a snippet using selectolax; collapse whitespace."""
    if not text:
        return ""
    parsed = HTMLParser(text)
    visible = parsed.text(separator=" ", strip=True)
    return _WHITESPACE_RE.sub(" ", visible).strip()


def _struct_time_to_date(value: object) -> date | None:
    """Convert a feedparser ``published_parsed`` struct_time to a date.

    feedparser parses dates into stdlib time.struct_time tuples; we only
    keep year/month/day for our schema's ``published_date`` column.
    Returns None on any malformed input.
    """
    if value is None:
        return None
    try:
        # struct_time exposes tm_year/tm_mon/tm_mday
        return date(value.tm_year, value.tm_mon, value.tm_mday)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None


def _extract_article_text(html: str) -> str:
    """Parse ``html`` with selectolax, drop noise subtrees, return collapsed text."""
    tree = HTMLParser(html)
    for selector in _NOISE_TAGS:
        for node in tree.css(selector):
            node.decompose()
    root = tree.body or tree
    text = root.text(separator=" ", strip=True)
    return _WHITESPACE_RE.sub(" ", text).strip()


class NewsClient:
    """Async news client. Per-domain throttle + robots + retries.

    Usage:

        async with NewsClient(user_agent="nous-bot (you@example.com)") as nc:
            entries = await nc.google_news_rss("\\"OpenAI\\" funding")
            for entry in entries:
                body = await nc.fetch_article_body(entry.url)
    """

    def __init__(
        self,
        user_agent: str,
        requests_per_second_per_domain: float = 1.0,
        throttle: DomainThrottle | None = None,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError(
                "user_agent must be a non-empty string containing a contact email. "
                "Most news sites block anonymous crawlers."
            )
        self._user_agent = user_agent
        # Throttle state is process-wide by default (nous.sources._http), so
        # this client and e.g. HomepageClient take turns on a shared host.
        self._http = ThrottledHTTPClient(
            requests_per_second_per_domain=requests_per_second_per_domain,
            throttle=throttle,
        )

        self._client: httpx.AsyncClient | None = None
        self._robots: RobotsCache | None = None

    async def __aenter__(self) -> NewsClient:
        self._client = guarded_async_client(
            headers={"User-Agent": self._user_agent},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        self._robots = RobotsCache(
            client=guarded_async_client(
                headers={"User-Agent": self._user_agent},
                timeout=httpx.Timeout(5.0),
                follow_redirects=True,
            ),
            user_agent=self._user_agent,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._robots is not None:
            await self._robots._client.aclose()
            self._robots = None

    def _assert_open(self) -> tuple[httpx.AsyncClient, RobotsCache]:
        if self._client is None or self._robots is None:
            raise RuntimeError("NewsClient must be used as an async context manager.")
        return self._client, self._robots

    async def _get_with_retry(self, url: str) -> httpx.Response:
        """Rate-limited GET, serialised per domain, with the shared retry policy
        (429 / 5xx / timeouts — see nous.sources._http)."""
        client, _ = self._assert_open()
        return await self._http.get(client, url)

    async def fetch_text(self, url: str) -> str:
        """Robots-checked, throttled, retried GET. Returns response body text.

        Published feed surfaces (``_ROBOTS_EXEMPT_PREFIXES``, e.g. Google News
        RSS) skip the robots gate — see that constant's docstring — but still
        pay the per-domain throttle and carry our identifying User-Agent.
        """
        _, robots = self._assert_open()
        if not _is_robots_exempt(url):
            allowed = await robots.is_allowed(url)
            if not allowed:
                raise RobotsBlockedError(f"robots.txt disallows: {url}")
        resp = await self._get_with_retry(url)
        return resp.text

    async def google_news_rss(
        self,
        query: str,
        lookback_days: int = 7,
    ) -> list[NewsArticleResult]:
        """Fetch Google News RSS for ``query``, return funding-keyword matches only.

        Filtering:
        - Entries older than ``lookback_days`` are dropped. Google News doesn't
          honor a server-side date filter on the RSS endpoint reliably, so we
          filter client-side. Entries with no parseable date are kept (we'd
          rather over-include than silently drop signal).
        - Title + snippet (HTML-stripped) must contain at least one
          FUNDING_KEYWORDS hit. Spec §5.5.

        Dedup:
        - URLs are canonicalized (tracking params + fragment dropped) before
          dedup. The same article appearing under two Google News redirect
          URLs with differing tracking suffixes collapses to one entry.
        """
        rss_url = f"{GOOGLE_NEWS_RSS_BASE}?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            xml_text = await self.fetch_text(rss_url)
        except RobotsBlockedError:
            logger.warning("Google News RSS blocked by robots.txt for query %r", query)
            return []
        except (httpx.HTTPStatusError, httpx.RequestError, BlockedAddressError) as exc:
            logger.warning("Google News RSS fetch failed for %r: %s", query, exc)
            return []

        return self._parse_rss(
            xml_text,
            lookback_days=lookback_days,
            require_keywords=True,
        )

    async def fetch_article_body(self, url: str) -> str | None:
        """Fetch ``url`` and return cleaned visible text.

        Returns None on:
        - robots.txt block
        - HTTP 4xx (after retries; 4xx is not retried)
        - HTTP 5xx (after retries are exhausted)
        - Network error (after retries)
        - SSRF-blocked or unresolvable host (BlockedAddressError)
        - Extracted text shorter than MIN_BODY_CHARS (paywall / JS shell)
        """
        try:
            html_text = await self.fetch_text(url)
        except RobotsBlockedError:
            logger.info("robots.txt blocked article body fetch: %s", url)
            return None
        except httpx.HTTPStatusError as exc:
            logger.info("HTTP %d on article body fetch: %s", exc.response.status_code, url)
            return None
        except httpx.RequestError as exc:
            logger.info("network error on article body fetch %s: %s", url, exc)
            return None
        except BlockedAddressError as exc:
            # Non-public or unresolvable publisher host (SSRF guard, fails closed
            # on DNS failure). Unreachable like a network error → return None.
            logger.info("blocked address on article body fetch %s: %s", url, exc)
            return None

        text = _extract_article_text(html_text)
        if len(text) < MIN_BODY_CHARS:
            logger.info(
                "article body too short (%d chars < %d) — likely paywall: %s",
                len(text),
                MIN_BODY_CHARS,
                url,
            )
            return None
        return text

    async def resolve_article(self, url: str) -> ResolvedArticle | None:
        """Follow a Google-News redirect to its publisher and return the body.

        Google-News RSS ``<link>``s are opaque redirects (news.google.com/rss/
        articles/CBMi...). Historically the pipeline stored only the headline +
        snippet because that link never yields a body when fetched naively. This
        chases the redirect to the real publisher and extracts the article text,
        so the funding-extraction LLM gets full prose instead of a one-line
        headline — which lifts extraction confidence off the floor (Task A1).

        Discipline (identical to every other fetch here):
        - The initial GET targets the Google-News redirect, which is exempt from
          the robots gate (``_ROBOTS_EXEMPT_PREFIXES``) but still pays the
          per-domain throttle and carries our User-Agent.
        - robots.txt IS enforced on the RESOLVED publisher URL — the exemption
          does not extend to the destination. A disallow returns None.
        - The per-domain throttle covers both the news.google.com hop and the
          publisher hop (``_get_with_retry`` → ``_throttled_get``).

        Returns None (caller falls back to the headline) when:
        - the link does not redirect away from news.google.com (consent
          interstitial — no real article),
        - the resolved response is not HTML,
        - robots.txt disallows the publisher URL,
        - the extracted text is below MIN_BODY_CHARS (paywall / JS shell),
        - any fetch error (4xx/5xx after retries, network error, SSRF-block).
        """
        try:
            resp = await self._get_with_retry(url)
        except httpx.HTTPStatusError as exc:
            logger.info(
                "HTTP %d resolving Google-News redirect: %s",
                exc.response.status_code,
                url,
            )
            return None
        except httpx.RequestError as exc:
            logger.info("network error resolving Google-News redirect %s: %s", url, exc)
            return None
        except BlockedAddressError as exc:
            logger.info("blocked address resolving Google-News redirect %s: %s", url, exc)
            return None

        final_url = str(resp.url)
        # No redirect off Google News → the consent/interstitial shell, not an
        # article. Nothing to resolve; the caller keeps the headline.
        if hostname(final_url) == _GOOGLE_NEWS_HOST:
            logger.info("Google-News link did not redirect to a publisher: %s", url)
            return None

        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type.lower():
            logger.info(
                "resolved Google-News target is not HTML (%s): %s",
                content_type or "<none>",
                final_url,
            )
            return None

        # robots.txt is enforced on the publisher (the Google-News exemption
        # does NOT extend to the destination).
        _, robots = self._assert_open()
        try:
            allowed = await robots.is_allowed(final_url)
        except BlockedAddressError:
            # Unresolvable / non-public robots host — treat as unreachable.
            logger.info("blocked address on robots for resolved target: %s", final_url)
            return None
        if not allowed:
            logger.info("robots.txt disallows resolved publisher URL: %s", final_url)
            return None

        text = _extract_article_text(resp.text)
        if len(text) < MIN_BODY_CHARS:
            logger.info(
                "resolved article body too short (%d < %d): %s",
                len(text),
                MIN_BODY_CHARS,
                final_url,
            )
            return None

        return ResolvedArticle(
            url=final_url,
            source=hostname(final_url),
            body=text,
        )

    def _parse_rss(
        self,
        xml_text: str,
        *,
        lookback_days: int,
        require_keywords: bool,
    ) -> list[NewsArticleResult]:
        """Parse RSS XML into deduplicated, optionally keyword-filtered results.

        Used by both ``google_news_rss`` and the TechCrunch adapter — the
        adapter passes ``require_keywords=False`` because the TC venture tag
        is itself the funding filter.
        """
        parsed = feedparser.parse(xml_text)
        cutoff: date | None = None
        if lookback_days >= 0:
            cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).date()

        seen: set[str] = set()
        results: list[NewsArticleResult] = []
        for entry in parsed.entries:
            link = entry.get("link")
            title = entry.get("title")
            if not link or not title:
                continue

            url_canon = canonical_url(link)
            if url_canon in seen:
                continue

            snippet = _strip_html(entry.get("summary") or "")
            published = _struct_time_to_date(entry.get("published_parsed"))
            if cutoff is not None and published is not None and published < cutoff:
                continue

            if require_keywords:
                haystack = f"{title}\n{snippet}"
                if not _matches_funding_keyword(haystack):
                    continue

            # Source: Google News supplies a <source> element with the real
            # publisher; fall back to the URL's hostname for direct feeds.
            source: str
            src = entry.get("source")
            if isinstance(src, dict) and src.get("href"):
                source = hostname(str(src["href"]))
            else:
                source = hostname(link)

            seen.add(url_canon)
            results.append(
                NewsArticleResult(
                    url=url_canon,
                    title=title,
                    source=source,
                    published_date=published,
                    raw_content=snippet,
                )
            )
        return results
