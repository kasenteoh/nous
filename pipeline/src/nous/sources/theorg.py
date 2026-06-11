"""Employee-count signal from theorg.com public org profiles.

The Org renders a company's headcount band as ``"employeeRange":"200-500"`` in
its embedded Next.js data (``__NEXT_DATA__``). We fetch the profile through the
shared HomepageClient (robots/UA/throttle/Chrome-fallback all inherited) and
pull that field out. Best-effort: any failure — robots block, 404, missing
field — returns ``None`` rather than raising.
"""

from __future__ import annotations

import logging
import re

from nous.sources.homepage import HomepageClient
from nous.util.employee_range import parse_employee_range
from nous.util.slugify import slugify

logger = logging.getLogger(__name__)

_EMPLOYEE_RANGE_RE = re.compile(r'"employeeRange"\s*:\s*"([^"]+)"')


async def get_employee_range(
    client: HomepageClient, company_name: str
) -> tuple[int, int] | None:
    """Return ``(min, max)`` employee range from theorg, or ``None``."""
    slug = slugify(company_name)
    if not slug:
        return None
    url = f"https://theorg.com/org/{slug}"
    try:
        result = await client.fetch(url)
    except Exception:  # noqa: BLE001 — best-effort source, degrade to None
        logger.debug("theorg: fetch failed for %s", company_name, exc_info=True)
        return None

    match = _EMPLOYEE_RANGE_RE.search(result.content)
    if match is None:
        return None
    return parse_employee_range(match.group(1))
