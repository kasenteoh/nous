"""Async HTTP client for fetching company homepages.

- Per-domain throttle (1 req/sec, spec §3.2 + §11), shared process-wide via
  nous.sources._http so independent clients and other transports (Playwright,
  curl_cffi) take turns on a host.
- User-Agent enforced on every request (constructor rejects empty).
- robots.txt checked before every fetch via RobotsCache.
- tenacity retries on 429 / 5xx / timeouts (3 attempts, exp backoff) — see
  nous.sources._http for the shared policy.
- Reasonable timeouts (30s overall; 5s for robots.txt).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser

from nous.sources._http import DomainThrottle, ThrottledHTTPClient
from nous.sources.duckduckgo import DuckDuckGoSearch, is_aggregator
from nous.sources.parked import looks_parked
from nous.sources.reject_hosts import is_aggregator_url, is_article_url
from nous.sources.robots import RobotsBlockedError, RobotsCache
from nous.util.ssrf import (
    BlockedAddressError,
    assert_public_url,
    guarded_async_client,
)
from nous.util.title_subject import name_is_dominant_subject
from nous.util.url import canonical_domain

# Re-export for backwards compatibility — callers that did
# `from nous.sources.homepage import RobotsBlockedError` continue to work,
# but the canonical home is now nous.sources.robots.
__all__ = ["FetchResult", "HomepageClient", "RobotsBlockedError"]

logger = logging.getLogger(__name__)


class FetchResult(BaseModel):
    url: str            # final URL after redirects
    status_code: int
    content: str
    content_type: str   # e.g. "text/html"

CANDIDATE_TLDS: tuple[str, ...] = (".com", ".io", ".ai", ".co")
CANDIDATE_PATHS: tuple[str, ...] = (
    "/", "/about", "/about-us", "/product", "/products", "/company", "/team",
)

# The curl_cffi fallback follows redirects manually (so each hop can be
# SSRF-validated). Cap the chain to avoid redirect loops / tar-pits.
_MAX_FALLBACK_REDIRECTS = 5


def _name_in_strong_position(html: str, slug_base: str, company_name: str) -> bool:
    """Return True when the company is the *dominant subject* of the page <title>
    or an <h1> — not merely one brand listed among others.

    "Strong position" already guards against directory / aggregator pages that
    mention a company name only in body text.  This adds a *dominance* check on
    top, because a strong-position mention is not enough on its own: a DIFFERENT
    company's homepage that lists the target among several brands also satisfies
    "name appears in an <h1>".  In production this attached **Kalshi** (a
    prediction market) to **FrenFlow**'s site — whose <h1> reads "copy-trade
    across Polymarket, Kalshi, Predict.fun, Hyperliquid" — and **AgentMail** to a
    "Series V" page.

    A title/h1 accepts the page when EITHER:
    - the slug form (``lightning-ai`` / ``lightning ai``) is the dominant subject
      of the element, OR
    - the prose company name is the dominant subject (see
      :func:`name_is_dominant_subject`).

    Dominance rejects a *competing leading brand* ("FrenFlow — …" for Kalshi) and
    a *brand list* where the company is not the first item, while still accepting
    ordinary single-subject homepages ("Acme — tagline", "Welcome to Acme", a
    bare "Acme", "Acme vs Bar").

    Uses the hyphen-normalised slug (e.g. "lightning-ai" → "lightning ai") so
    both slug forms are tested against each strong element.
    """
    tree = HTMLParser(html)

    slug_spaced = slug_base.replace("-", " ")

    def _dominant(text: str) -> bool:
        if not text:
            return False
        # Test the prose name and both slug forms; the slug is what the resolver
        # guessed the domain from, so a homepage whose title is just the slug
        # ("lightning-ai") must still pass.
        return (
            name_is_dominant_subject(text, company_name)
            or name_is_dominant_subject(text, slug_base)
            or name_is_dominant_subject(text, slug_spaced)
        )

    title_node = tree.css_first("title")
    if title_node is not None and _dominant(title_node.text(strip=True)):
        return True

    return any(_dominant(h1.text(strip=True)) for h1 in tree.css("h1"))


class HomepageClient:
    """Async context-manager that fetches company homepages with per-domain rate limiting.

    Throttle state lives in the process-wide registry (nous.sources._http) by
    default, so a second HomepageClient — or the headless-browser fallback —
    hitting the same host waits its turn. Pass ``throttle`` to isolate (tests).
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
                "Websites block anonymous crawlers — this is non-negotiable."
            )
        self._user_agent = user_agent
        self._http = ThrottledHTTPClient(
            requests_per_second_per_domain=requests_per_second_per_domain,
            throttle=throttle,
        )

        self._client: httpx.AsyncClient | None = None
        self._robots: RobotsCache | None = None
        self._search_client: DuckDuckGoSearch | None = None

    async def __aenter__(self) -> HomepageClient:
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
        # Reuse the main client for DDG searches. The DuckDuckGoSearch client
        # manages its own global throttle (2 req/sec by default, spec §3.2).
        self._search_client = DuckDuckGoSearch(
            self._client,
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
        """Return the underlying clients, raising if not inside ``async with``."""
        if self._client is None or self._robots is None:
            raise RuntimeError("HomepageClient must be used as an async context manager.")
        return self._client, self._robots

    async def _throttled_get(self, url: str) -> httpx.Response:
        """Rate-limited GET, serialised per domain, with the shared retry policy
        (429 / 5xx / timeouts — see nous.sources._http)."""
        client, _ = self._assert_open()
        return await self._http.get(client, url)

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a single URL.

        Raises:
            RobotsBlockedError: if robots.txt disallows the URL.
            httpx.HTTPStatusError: on 4xx (non-429) after retries exhausted.
            httpx.RequestError: on network errors after retries exhausted.

        On HTTP 403 from the httpx path, transparently retries with curl_cffi
        impersonating Chrome 120 — bypasses Cloudflare/WAF that fingerprint
        at the TLS layer (e.g. adquick.com 403s every UA from plain httpx but
        returns 200 to a real Chrome TLS handshake). robots.txt is always
        checked first; the fallback never sidesteps the robots policy.
        """
        _, robots = self._assert_open()

        allowed = await robots.is_allowed(url)
        if not allowed:
            raise RobotsBlockedError(f"robots.txt disallows: {url}")

        try:
            resp = await self._throttled_get(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                logger.info(
                    "fetch: httpx got 403 for %s — retrying with Chrome impersonation",
                    url,
                )
                try:
                    return await self._fetch_with_chrome_impersonation(url)
                except Exception as inner:
                    logger.info(
                        "fetch: Chrome impersonation also failed for %s: %s: %s",
                        url,
                        type(inner).__name__,
                        inner,
                    )
                    # Re-raise the original 403 so the caller's metrics still
                    # categorize this as "blocked", not "Chrome client crash".
                    raise exc from None
            raise

        content_type = resp.headers.get("content-type", "")
        # Strip charset suffix if present: "text/html; charset=utf-8" → "text/html"
        content_type_clean = content_type.split(";")[0].strip()

        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            content=resp.text,
            content_type=content_type_clean,
        )

    async def _fetch_with_chrome_impersonation(self, url: str) -> FetchResult:
        """Fallback fetcher using curl_cffi with a real Chrome 120 TLS+HTTP2
        fingerprint. Bypasses Cloudflare/WAF that block requests at the TLS
        layer rather than via User-Agent string.

        Reuses the per-domain throttle (we still want to be polite even when
        switching transports). Raises httpx.HTTPStatusError on non-2xx so the
        caller's existing error handling treats it the same as the primary
        path.
        """
        # Imported lazily so the dependency isn't required at module-load time
        # — useful for environments where curl_cffi isn't installed yet (older
        # branches in CI, local dev without `uv sync`, etc.).
        from curl_cffi.requests import AsyncSession

        # SSRF-check before taking the slot: a blocked URL never fires a
        # request, so it must not consume (or stamp) a throttle interval.
        # Redirect targets are re-validated inside; those failures DO stamp,
        # correctly — a request already hit the host by then.
        await assert_public_url(url)

        # Same throttle slot as the httpx path (and every other transport):
        # switching to curl_cffi must not double-hit the host.
        async with self._http.slot(url), AsyncSession() as session:
            resp = await session.get(
                url,
                impersonate="chrome120",
                timeout=30,
                allow_redirects=False,
            )
            hops = 0
            while (
                resp.status_code in (301, 302, 303, 307, 308)
                and hops < _MAX_FALLBACK_REDIRECTS
            ):
                location = resp.headers.get("location")
                if not location:
                    break
                url = urljoin(url, location)
                await assert_public_url(url)  # re-validate the redirect target
                resp = await session.get(
                    url,
                    impersonate="chrome120",
                    timeout=30,
                    allow_redirects=False,
                )
                hops += 1

        if resp.status_code in (301, 302, 303, 307, 308):
            # The manual redirect loop above exhausted _MAX_FALLBACK_REDIRECTS
            # (or hit a Location-less redirect) and never reached a final
            # response. Don't hand a 3xx back as page content — signal a failed
            # fetch the same way httpx would for an over-long redirect chain
            # (httpx.TooManyRedirects is an httpx.RequestError), which every
            # caller already handles.
            raise httpx.RequestError(
                f"too many redirects in chrome fallback for {url}"
            )

        if resp.status_code >= 400:
            # Synthesize an httpx-like exception so the caller's except chain
            # matches what it expects from the primary fetcher.
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} from Chrome-impersonation fetch",
                request=httpx.Request("GET", url),
                response=httpx.Response(
                    resp.status_code,
                    content=resp.content,
                    request=httpx.Request("GET", url),
                ),
            )

        content_type = resp.headers.get("content-type", "")
        content_type_clean = content_type.split(";")[0].strip()

        return FetchResult(
            url=str(resp.url),
            status_code=resp.status_code,
            content=resp.text,
            content_type=content_type_clean,
        )

    async def search_companies(self, query: str, limit: int = 10) -> list[str]:
        """Search DuckDuckGo for candidate company homepage URLs.

        Delegates to the internal DuckDuckGoSearch client. Returns an empty
        list if DDG is unavailable or returns a captcha interstitial.
        """
        if self._search_client is None:
            raise RuntimeError("HomepageClient must be used as an async context manager.")
        return await self._search_client.search(query, limit=limit)

    @property
    def ddg_blocked(self) -> bool:
        """True once DDG has rate-limited this process (circuit breaker open).

        Lets callers annotate results that were computed without the DDG
        fallback — a "no match" under an open breaker is weaker evidence than
        one where the search actually ran.
        """
        return self._search_client is not None and self._search_client.is_blocked


async def resolve_homepage(
    client: HomepageClient,
    slug_base: str,
    company_name: str,
    *,
    tlds: Iterable[str] = CANDIDATE_TLDS,
    rejected_urls: Iterable[str] = (),
) -> str | None:
    """Phase 1: try ``{slug_base}{tld}`` for each TLD in order.

    Candidates whose canonical domain matches a previously rejected URL
    (``rejected_urls`` — confirmed-wrong domains recorded by enrichment) are
    skipped before fetching.  On a 200 response the page is checked in order:
    (1) parked/for-sale (see nous.sources.parked), (2) known-aggregator host or
    directory path (see nous.sources.reject_hosts), (3) company name present in
    a *strong* position — the page ``<title>`` or an ``<h1>`` element.  A bare
    body-text mention is not sufficient, which prevents startup-directory pages
    that list the company name in prose from being accepted.

    Phase 2: if all TLD guesses miss, query DuckDuckGo for
    ``"{company_name}" startup``, apply the same aggregator/parked/strong-name
    checks, and return the first surviving candidate.

    Returns None if both phases miss.
    """
    # Note: canonical_domain returns None for shared-hosting hosts (its
    # dedup-identity contract), so rejected URLs on shared hosting are not
    # blocked here — acceptable because the parked/enrichment checks re-reject
    # them on refetch.
    rejected_domains = {
        d for d in (canonical_domain(u) for u in rejected_urls) if d is not None
    }

    # Phase 1: TLD heuristic
    for tld in tlds:
        url = f"https://{slug_base}{tld}"
        if canonical_domain(url) in rejected_domains:
            continue
        try:
            result = await client.fetch(url)
        except RobotsBlockedError:
            continue
        except httpx.HTTPStatusError:
            continue
        except httpx.RequestError:
            continue
        except BlockedAddressError:
            # SSRF guard rejected this candidate (internal/unresolvable host or
            # a redirect to one). Skip it like a connection error and try the
            # next TLD — never error the whole company.
            continue

        # A parked page always mentions the domain name, so this check MUST
        # run before the name-mention acceptance below.
        if looks_parked(result.content):
            continue

        # Reject known startup-directory and aggregator hosts (e.g. a TLD
        # guess that accidentally hits tracxn.com or theorg.com).  Also
        # rejects any URL whose path looks like a directory listing, and
        # dated-article paths on ANY host (never a homepage).
        if is_aggregator_url(result.url) or is_article_url(result.url):
            continue

        # Require the company name to appear in a *strong* position — the page
        # <title> or an <h1>.  A bare body-text match is not enough: directory
        # pages (e.g. startupintros.com/orgs/acme) mention the name in prose
        # but their title/h1 describe the directory, not the company itself.
        if _name_in_strong_position(result.content, slug_base, company_name):
            return result.url  # final URL after any redirects

    # Phase 2: DuckDuckGo search fallback. Treat any failure (network error,
    # captcha interstitial, malformed HTML, missing context-manager state) as
    # "no candidates" rather than letting the stage record an error — DDG is
    # supplementary; a broken fallback shouldn't reclassify a no-match as an
    # error and prevent website_resolved_at from being set.
    query = f'"{company_name}" startup'
    try:
        candidates = await client.search_companies(query, limit=10)
    except Exception:
        logger.warning(
            "DDG search fallback failed for %s; treating as no candidates",
            company_name,
            exc_info=True,
        )
        candidates = []

    for candidate_url in candidates:
        # Reject known aggregator hosts, directory-path patterns, and
        # dated-article paths BEFORE fetching — saves a round-trip.
        if (
            is_aggregator(candidate_url)
            or is_aggregator_url(candidate_url)
            or is_article_url(candidate_url)
        ):
            continue
        if canonical_domain(candidate_url) in rejected_domains:
            continue
        try:
            result = await client.fetch(candidate_url)
        except (
            RobotsBlockedError,
            httpx.HTTPStatusError,
            httpx.RequestError,
            BlockedAddressError,
        ):
            continue
        if looks_parked(result.content):
            continue
        # Post-fetch: also reject aggregator/article URLs returned by redirects.
        if is_aggregator_url(result.url) or is_article_url(result.url):
            continue
        # Require strong-position match (title or h1) — same criterion as
        # Phase 1; body-only mentions are not sufficient.
        if _name_in_strong_position(result.content, slug_base, company_name):
            return result.url

    return None
