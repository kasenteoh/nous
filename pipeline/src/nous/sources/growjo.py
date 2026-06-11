"""Employee-count signal from growjo.com public company profiles.

GrowJo shows an employee estimate on each company page ("… has 1,001
employees", or a "Number of Employees" stat). We fetch the profile through the
shared HomepageClient and parse that figure. Like Wellfound, GrowJo is
bot-hostile and often returns 403 even via the Chrome-impersonation fallback,
so ``None`` is a common (intended) outcome.
"""

from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser

from nous.sources.homepage import HomepageClient
from nous.util.employee_range import parse_employee_range
from nous.util.slugify import slugify

logger = logging.getLogger(__name__)

# "Number of Employees 1,001" or "… has 1,001 employees" or a "11-50" band.
_NUMBER_OF_EMPLOYEES_RE = re.compile(
    r"number of employees\s*[:\-]?\s*(\d[\d,]*\s*(?:[-–—]|to)\s*\d[\d,]*|\d[\d,]*\s*\+|\d[\d,]*)",
    re.IGNORECASE,
)
_EMPLOYEES_RE = re.compile(
    r"(\d[\d,]*\s*(?:[-–—]|to)\s*\d[\d,]*|\d[\d,]*\s*\+|\d[\d,]*)\s*employees",
    re.IGNORECASE,
)


async def get_employee_range(
    client: HomepageClient, company_name: str
) -> tuple[int, int] | None:
    """Return ``(min, max)`` employee range from GrowJo, or ``None``."""
    slug = slugify(company_name)
    if not slug:
        return None
    url = f"https://growjo.com/company/{slug}"
    try:
        result = await client.fetch(url)
    except Exception:  # noqa: BLE001 — best-effort source, degrade to None
        logger.debug("growjo: fetch failed for %s", company_name, exc_info=True)
        return None

    text = HTMLParser(result.content).text(separator=" ")
    labelled = _NUMBER_OF_EMPLOYEES_RE.search(text)
    if labelled is not None:
        parsed = parse_employee_range(labelled.group(1))
        if parsed is not None:
            return parsed
    near = _EMPLOYEES_RE.search(text)
    if near is not None:
        return parse_employee_range(near.group(0))
    return None
