"""Parse employee-count ranges out of the varied prose the employee-count
sources expose ("11-50", "11–50 employees", "1,001-5,000", "5000+", "250").

Returns a normalized ``(min, max)`` tuple of ints, or ``None`` when nothing
recognizable is present. Open-ended "N+" buckets map to ``(N, _OPEN_ENDED_MAX)``.
"""

from __future__ import annotations

import re

# Cap used for open-ended "N+" buckets — large enough to read as "and up"
# without implying a precise upper bound.
_OPEN_ENDED_MAX = 100_000

# Two numbers joined by a dash/en-dash/em-dash or "to": "11-50", "11 – 50", "11 to 50".
_RANGE_RE = re.compile(r"(\d[\d,]*)\s*(?:[-–—]|to)\s*(\d[\d,]*)", re.IGNORECASE)
# Open-ended: "5000+".
_PLUS_RE = re.compile(r"(\d[\d,]*)\s*\+")
# A single count attached to "employees": "1,001 employees", "250 full-time employees".
_COUNT_RE = re.compile(
    r"(\d[\d,]*)\s*(?:\+\s*)?(?:full[- ]?time\s+)?employees", re.IGNORECASE
)
# The whole string is just a number: "250".
_BARE_RE = re.compile(r"^\s*(\d[\d,]*)\s*$")


def _to_int(raw: str) -> int:
    return int(raw.replace(",", ""))


def parse_employee_range(text: str | None) -> tuple[int, int] | None:
    """Best-effort parse of an employee-count range/figure from *text*.

    Tries, in order: an explicit ``N-M`` range, an open-ended ``N+`` bucket, a
    ``N employees`` count, then a bare standalone number. Returns ``None`` if
    none match.
    """
    if not text:
        return None

    match = _RANGE_RE.search(text)
    if match is not None:
        low, high = _to_int(match.group(1)), _to_int(match.group(2))
        return (low, high) if low <= high else (high, low)

    match = _PLUS_RE.search(text)
    if match is not None:
        return (_to_int(match.group(1)), _OPEN_ENDED_MAX)

    match = _COUNT_RE.search(text)
    if match is not None:
        value = _to_int(match.group(1))
        return (value, value)

    match = _BARE_RE.match(text)
    if match is not None:
        value = _to_int(match.group(1))
        return (value, value)

    return None
