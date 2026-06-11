"""Employee-count signal from Wellfound (AngelList) public company profiles.

Wellfound profiles show a "Company size" band such as "11-50 employees". We
fetch the profile through the shared HomepageClient and read that band. Note:
Wellfound sits behind Cloudflare and frequently blocks automated traffic even
with the Chrome-impersonation fallback, so this source returns ``None`` for a
large fraction of companies in practice — by design, the stage then falls
through to the next source.
"""

from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.util.employee_range import parse_employee_range
from nous.util.slugify import slugify

logger = logging.getLogger(__name__)

# A range/figure that follows a "Company size" label.
_SIZE_LABEL_RE = re.compile(
    r"company size\s*[:\-]?\s*(\d[\d,]*\s*(?:[-–—]|to)\s*\d[\d,]*|\d[\d,]*\s*\+|\d[\d,]*)",
    re.IGNORECASE,
)
# A range/figure immediately preceding the word "employees".
_EMPLOYEES_RE = re.compile(
    r"(\d[\d,]*\s*(?:[-–—]|to)\s*\d[\d,]*|\d[\d,]*\s*\+)\s*employees",
    re.IGNORECASE,
)


async def get_employee_range(
    client: HomepageClient, company_name: str
) -> tuple[int, int] | None:
    """Return ``(min, max)`` employee range from Wellfound, or ``None``."""
    slug = slugify(company_name)
    if not slug:
        return None
    url = f"https://wellfound.com/company/{slug}"
    try:
        result = await client.fetch(url)
    except Exception:  # noqa: BLE001 — best-effort source, degrade to None
        logger.debug("wellfound: fetch failed for %s", company_name, exc_info=True)
        return None

    text = HTMLParser(result.content).text(separator=" ")
    label = _SIZE_LABEL_RE.search(text)
    if label is not None:
        parsed = parse_employee_range(label.group(1))
        if parsed is not None:
            return parsed
    near = _EMPLOYEES_RE.search(text)
    if near is not None:
        return parse_employee_range(near.group(0))
    return None
