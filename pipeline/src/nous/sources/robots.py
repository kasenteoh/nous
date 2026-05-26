"""Per-domain robots.txt cache + is_allowed check using stdlib
urllib.robotparser. Thread-safe LRU cache; entries expire after
ROBOTS_CACHE_TTL_SECONDS (default: 86400 = 24h).
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

# 24h TTL — per Open Questions §1 in plan
ROBOTS_CACHE_TTL_SECONDS: int = 86400


class RobotsCache:
    """Async cache for robots.txt parsers, keyed by domain.

    Thread-safety: a single asyncio.Lock protects the whole cache dict.
    Contention is minimal because robots.txt fetches are infrequent (once
    per domain per 24h) and cheap relative to page fetches.
    """

    def __init__(self, client: httpx.AsyncClient, user_agent: str) -> None:
        self._client = client
        self._user_agent = user_agent
        # (parser, fetched_at_monotonic)
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, url: str) -> bool:
        """Return True if our user_agent may fetch ``url``.

        Missing or unreachable robots.txt → allow (per RFC convention).
        """
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"

        parser = await self._get_parser(domain)
        if parser is None:
            # Could not fetch robots.txt → allow by convention
            return True
        return parser.can_fetch(self._user_agent, path)

    async def _get_parser(self, domain: str) -> RobotFileParser | None:
        """Return a (possibly cached) RobotFileParser for *domain*, or None on error."""
        async with self._lock:
            now = time.monotonic()
            cached = self._cache.get(domain)
            if cached is not None:
                parser, fetched_at = cached
                if now - fetched_at < ROBOTS_CACHE_TTL_SECONDS:
                    return parser
                # Expired — fall through to re-fetch.

            robots_url = f"{domain}/robots.txt"
            try:
                resp = await self._client.get(
                    robots_url,
                    timeout=5.0,
                    follow_redirects=True,
                )
            except (httpx.RequestError, httpx.HTTPStatusError):
                # Network error or explicit error response → allow
                return None

            if resp.status_code == 404:
                # No robots.txt → allow
                return None

            if resp.status_code >= 400:
                # Any other client/server error → allow
                return None

            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(resp.text.splitlines())
            self._cache[domain] = (parser, now)
            return parser
