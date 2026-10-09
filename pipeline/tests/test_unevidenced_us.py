"""Pure tests for the unevidenced-US predicate (normalize-hq-state pass 2).

A stored hq_country='US' is only trusted when something supports it: a real US
state or a country the enrich LLM stated. A city alone is not
evidence (the "London"/"Bangalore" → US leak the old tier-3 rule created).
The ccTLD tier never yields "US", so the website plays no part.
"""

from __future__ import annotations

import pytest

from nous.pipeline.normalize_hq_state import is_unevidenced_us


@pytest.mark.parametrize(
    ("hq_state", "hq_city", "payload", "expected"),
    [
        # City-only / foreign region / garbage, nothing stated → the tier-3
        # leak signature → unevidenced.
        (None, "Mumbai", {}, True),
        ("Ontario", None, {"hq_country": None}, True),
        ("San Francisco", None, None, True),
        (None, "Tel Aviv", None, True),
        # Neither state nor city: tier 3 could not have produced this "US"
        # (explicit judge verdict / legacy) → out of scope, never reset. The
        # 2026-10-09 broad predicate wrongly reset 495 of these.
        (None, None, {}, False),
        (None, None, None, False),
        ("  ", "", None, False),
        # Real US state (code or name) → evidenced.
        ("CA", "Oakland", None, False),
        ("California", None, None, False),
        # The enrich LLM stated a country → evidenced.
        (None, "Austin", {"hq_country": "US"}, False),
        # Blank stated country is no statement.
        (None, "Austin", {"hq_country": "  "}, True),
    ],
)
def test_is_unevidenced_us(
    hq_state: str | None,
    hq_city: str | None,
    payload: dict[str, object] | None,
    expected: bool,
) -> None:
    assert (
        is_unevidenced_us(hq_state=hq_state, hq_city=hq_city, enriched_payload=payload)
        is expected
    )
