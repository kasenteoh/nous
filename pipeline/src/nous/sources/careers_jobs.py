"""Employee-count *proxy* from a company's own careers page.

Open headcount is a rough size signal: a company hiring for 30 roles is bigger
than one hiring for 2. We fetch the company's careers/jobs pages through the
shared HomepageClient and count job-listing elements from the common ATS embeds
(Greenhouse ``div.opening``, Lever ``.posting``, Ashby) plus links to those
boards, then map the count to a coarse band.

This is deliberately the second-to-last signal (before GitHub): it's a proxy,
not a headcount. Any failure or a zero count returns ``None``.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient

logger = logging.getLogger(__name__)

# Selectors that each, on their own, identify one job posting. We take the max
# single-selector count on a page (not the sum) to avoid counting the same
# posting twice when it matches more than one selector.
_JOB_SELECTORS = (
    "div.opening",  # Greenhouse embedded board
    ".posting",  # Lever
    "[class*='job-post']",
    "[class*='JobPosting']",
    "a[href*='boards.greenhouse.io']",
    "a[href*='jobs.lever.co']",
    "a[href*='jobs.ashbyhq.com']",
)

_CAREERS_PATHS = ("", "/careers", "/jobs")


async def count_job_listings(
    client: HomepageClient, website: str | None
) -> tuple[int, int] | None:
    """Return a coarse ``(min, max)`` band inferred from open-role count, or None."""
    if not website:
        return None
    base = website if website.startswith(("http://", "https://")) else f"https://{website}"

    best = 0
    for path in _CAREERS_PATHS:
        url = urljoin(base + "/", path.lstrip("/")) if path else base
        try:
            result = await client.fetch(url)
        except Exception:  # noqa: BLE001 — best-effort source, degrade to None
            logger.debug("careers_jobs: fetch failed for %s", url, exc_info=True)
            continue
        tree = HTMLParser(result.content)
        page_max = max((len(tree.css(sel)) for sel in _JOB_SELECTORS), default=0)
        best = max(best, page_max)

    return _bucket_from_job_count(best)


def _bucket_from_job_count(count: int) -> tuple[int, int] | None:
    if count <= 0:
        return None
    if count <= 5:
        return (1, 10)
    if count <= 25:
        return (11, 50)
    if count <= 75:
        return (51, 200)
    if count <= 200:
        return (201, 500)
    return (501, 1000)
