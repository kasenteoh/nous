"""DuckDuckGo HTML search client.

Free, no API key, no auth. We hit https://html.duckduckgo.com/html/?q=<query>
and parse the result list to find candidate company homepage URLs.

Conservative throttle (1 req per 2 sec by default) and explicit captcha
detection — if DDG serves an anti-bot interstitial we return empty and
move on, never crash the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from selectolax.parser import HTMLParser

from nous.sources.reject_hosts import is_aggregator_host
from nous.util.ssrf import BlockedAddressError

logger = logging.getLogger(__name__)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# The domains-to-skip list used to live here as AGGREGATOR_DOMAINS and drifted
# against reject_hosts.AGGREGATOR_HOSTS. There is now exactly one blocklist —
# nous.sources.reject_hosts — and is_aggregator() below delegates to it.


class DuckDuckGoCaptchaError(Exception):
    """DDG served an anti-bot interstitial. Caller should give up gracefully."""


# Statuses DDG's anti-bot layer serves instead of results. 202 is the soft
# rate limit observed in production (it flooded the 2026-06-11 pipeline runs
# for hours); 403/429 are the hard variants. A 5xx is a server error, not a
# block, and must not trip the breaker.
_BLOCKED_STATUSES: frozenset[int] = frozenset({202, 403, 429})


class DuckDuckGoSearch:
    """Async DDG HTML search client with a global request throttle.

    Circuit breaker: after ``blocked_threshold`` consecutive blocked
    responses (202/403/429 or a captcha interstitial), the client stops
    issuing requests for the rest of the process and every search returns [].
    Once DDG rate-limits an IP it keeps doing so for hours — continuing to
    poll it wastes ~2s+ per company and is exactly the hammering that got the
    IP flagged in the first place.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str,
        *,
        seconds_between_requests: float = 2.0,
        blocked_threshold: int = 5,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._min_interval = seconds_between_requests
        self._last_request_at: float = 0.0
        self._lock = asyncio.Lock()
        self._blocked_threshold = blocked_threshold
        self._consecutive_blocked = 0
        self._breaker_open = False

    @property
    def is_blocked(self) -> bool:
        """True once the circuit breaker has opened for this process."""
        return self._breaker_open

    def _note_blocked(self, query: str, reason: str) -> None:
        self._consecutive_blocked += 1
        if (
            not self._breaker_open
            and self._consecutive_blocked >= self._blocked_threshold
        ):
            self._breaker_open = True
            logger.warning(
                "DDG circuit breaker OPEN after %d consecutive blocked responses "
                "(last: %s for %r) — skipping all DDG searches for the rest of "
                "this run",
                self._consecutive_blocked,
                reason,
                query,
            )

    async def search(self, query: str, *, limit: int = 10) -> list[str]:
        """Run a DDG HTML search, return up to ``limit`` result URLs in order.

        Returns [] on captcha, network error, unexpected response shape, or
        when the circuit breaker is open — callers should not depend on the
        search succeeding.
        """
        if self._breaker_open:
            return []
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._client.post(
                    DDG_HTML_URL,
                    data={"q": query, "kl": "us-en"},
                    headers={"User-Agent": self._user_agent},
                    timeout=15.0,
                    follow_redirects=True,
                )
            except (
                httpx.RequestError,
                httpx.HTTPStatusError,
                BlockedAddressError,
            ) as exc:
                logger.warning("DDG search network error for %r: %s", query, exc)
                self._last_request_at = time.monotonic()
                return []
            self._last_request_at = time.monotonic()

        if resp.status_code != 200:
            logger.warning("DDG returned status %d for %r", resp.status_code, query)
            if resp.status_code in _BLOCKED_STATUSES:
                self._note_blocked(query, f"HTTP {resp.status_code}")
            return []

        body = resp.text
        # Captcha / anti-bot interstitial detection. DDG serves a page with
        # the string "anomaly" or "/static-assets/blocked" in these cases.
        if "anomaly" in body.lower() or "blocked" in body.lower()[:2000]:
            logger.warning("DDG captcha/block detected for %r", query)
            self._note_blocked(query, "captcha interstitial")
            return []

        self._consecutive_blocked = 0
        return list(_extract_result_urls(body, limit=limit))


def _extract_result_urls(html: str, *, limit: int) -> Iterable[str]:
    """Pull result URLs out of DDG HTML.

    DDG result anchors are ``<a class="result__a" href="...">`` where the href
    may be a DDG redirect URL containing the real URL in a ``uddg`` query param.
    """
    tree = HTMLParser(html)
    seen: set[str] = set()
    for anchor in tree.css("a.result__a"):
        href = anchor.attributes.get("href")
        if not href:
            continue
        # DDG often wraps results in a redirect like /l/?kh=-1&uddg=https%3A%2F%2Fexample.com
        if href.startswith("/l/") or href.startswith("//duckduckgo.com/l/"):
            parsed = urlparse(href if href.startswith("/") else "https:" + href)
            params = parse_qs(parsed.query)
            uddg = params.get("uddg", [""])[0]
            if uddg:
                real_url = unquote(uddg)
            else:
                continue
        else:
            real_url = href
        # Normalize: must be http/https
        if not real_url.startswith(("http://", "https://")):
            continue
        if real_url in seen:
            continue
        seen.add(real_url)
        yield real_url
        if len(seen) >= limit:
            return


def is_aggregator(url: str) -> bool:
    """Return True if ``url``'s host is in the shared aggregator blocklist.

    Matches the host and any subdomain (e.g. ``linkedin.com`` matches
    ``www.linkedin.com`` and ``foo.linkedin.com``). Host-only — the caller
    pairs this with reject_hosts.is_aggregator_url when path-pattern
    rejection is also wanted.
    """
    return is_aggregator_host(urlparse(url).netloc)
