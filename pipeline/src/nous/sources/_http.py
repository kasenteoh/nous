"""Shared per-domain throttling + retry policy for the sources package.

Every outbound scrape must respect 1 req/sec per domain (spec §3.2 + §11,
CLAUDE.md non-negotiables). Before this module, HomepageClient, NewsClient and
HeadlessBrowserClient each kept *per-instance* lock/timestamp dicts, so two
transports targeting the same host (e.g. httpx then the Playwright fallback)
double-hit it. The fix: one process-wide :data:`DEFAULT_THROTTLE` registry that
every client shares by default, so independent instances — and different
transports — contend on the same per-domain lock.

Two layers:

- :class:`DomainThrottle` — the registry of per-domain asyncio locks and
  last-request monotonic timestamps. Transport-agnostic: httpx, curl_cffi and
  Playwright paths all acquire the same slot.
- :class:`ThrottledHTTPClient` — throttle policy (one min-interval) plus the
  throttled GET + tenacity retry behavior shared by the httpx-based clients.
  It does NOT own the underlying ``httpx.AsyncClient``; the client is passed
  per call so source clients keep managing (and tests keep injecting) their
  own transports.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

__all__ = ["DEFAULT_THROTTLE", "DomainThrottle", "ThrottledHTTPClient"]


def _is_retryable(exc: BaseException) -> bool:
    """Return True for errors that warrant a retry.

    Permanent errors (DNS failure, connection refused, TLS handshake) are
    represented by httpx.ConnectError and must NOT be retried — they add
    ~7s of dead time per non-existent domain across exp-backoff attempts.
    Only transient failures (rate limits, server errors, timeouts) retry.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        # 429 (rate limit) and 5xx (server error) — usually transient
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    # ConnectError, ProtocolError, etc. — usually permanent (DNS, refused, TLS handshake)
    # TimeoutException is a transient network blip — retry makes sense.
    return isinstance(exc, httpx.TimeoutException)


class DomainThrottle:
    """Registry of per-domain rate-limit slots.

    A slot serialises requests per domain: acquire the domain lock, sleep out
    whatever remains of the caller's min-interval since the *last* request to
    that domain (by anyone sharing this registry), run the request, stamp the
    timestamp on exit. The stamp lands in a ``finally`` so a failed request
    still counts against the interval — a timeout or 5xx almost certainly hit
    the host, and stamping a request that never fired costs at most one polite
    interval.

    ``min_interval`` is per-slot, not per-registry: clients with different
    rates can share one registry and per-domain serialization still holds
    (the lock is keyed only by domain); each acquirer just waits out its own
    interval relative to the shared timestamp.
    """

    def __init__(self) -> None:
        # domain → last request monotonic timestamp
        self._last_request: dict[str, float] = {}
        # Per-domain lock: only one request in flight per domain, preventing
        # thundering-herd when multiple coroutines target the same host.
        self._locks: dict[str, asyncio.Lock] = {}
        # Protects creation of new entries in the dicts above.
        self._registry_lock = asyncio.Lock()

    async def _get_lock(self, domain: str) -> asyncio.Lock:
        """Return (creating if needed) the per-domain Lock."""
        async with self._registry_lock:
            if domain not in self._locks:
                self._locks[domain] = asyncio.Lock()
            return self._locks[domain]

    @asynccontextmanager
    async def slot(self, domain: str, min_interval: float) -> AsyncIterator[None]:
        """Acquire a rate-limit slot for ``domain``.

        Holds the domain lock for the duration of the ``with`` body, so the
        request inside is serialised against every other slot-holder for the
        same domain — regardless of which client instance or transport they
        came from.
        """
        lock = await self._get_lock(domain)
        async with lock:
            wait = min_interval - (time.monotonic() - self._last_request.get(domain, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                self._last_request[domain] = time.monotonic()

    def slot_for_url(self, url: str, min_interval: float) -> AbstractAsyncContextManager[None]:
        """:meth:`slot` keyed by ``url``'s netloc (same keying all clients used)."""
        return self.slot(urlparse(url).netloc, min_interval)

    def reset(self) -> None:
        """Drop all throttle state. For test isolation only — never in production."""
        self._last_request.clear()
        self._locks.clear()


# Process-wide default registry. Sharing it is the point: a HomepageClient and
# the HeadlessBrowserClient fallback constructed independently must still take
# turns on a host. Tests reset it via the autouse fixture in conftest.py.
DEFAULT_THROTTLE = DomainThrottle()


class ThrottledHTTPClient:
    """Per-domain-throttled GET with the shared tenacity retry policy.

    Bundles the two things the httpx-based source clients triplicated: the
    throttle slot (via a :class:`DomainThrottle`, shared process-wide by
    default) and retry-on-429/5xx/timeout. Non-httpx transports (curl_cffi,
    Playwright) reuse :meth:`slot` so they pay the same per-domain toll.
    """

    def __init__(
        self,
        *,
        requests_per_second_per_domain: float = 1.0,
        throttle: DomainThrottle | None = None,
    ) -> None:
        self._min_interval: float = 1.0 / requests_per_second_per_domain
        self._throttle = throttle if throttle is not None else DEFAULT_THROTTLE

    @property
    def throttle(self) -> DomainThrottle:
        return self._throttle

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def slot(self, url: str) -> AbstractAsyncContextManager[None]:
        """Rate-limit slot for ``url``'s domain, at this client's interval.

        For requests that bypass httpx (curl_cffi Chrome impersonation) but
        must still cooperate with the throttle.
        """
        return self._throttle.slot_for_url(url, self._min_interval)

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """Throttled GET, serialised per domain, retried on 429/5xx/timeouts.

        Each retry attempt re-acquires the domain slot, so retries respect the
        throttle too. Raises ``httpx.HTTPStatusError`` on 4xx/5xx (non-retryable
        or retries exhausted) and ``httpx.RequestError`` on network errors.
        """
        async with self.slot(url):
            resp = await client.get(url)
            resp.raise_for_status()
            return resp
