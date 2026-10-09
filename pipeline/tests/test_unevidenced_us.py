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
    ("hq_state", "payload", "expected"),
    [
        # City-only / foreign region / garbage, nothing stated → unevidenced.
        (None, {"hq_city": "Mumbai"}, True),
        ("Ontario", {"hq_country": None}, True),
        ("San Francisco", None, True),
        (None, {}, True),
        # Real US state (code or name) → evidenced.
        ("CA", None, False),
        ("California", None, False),
        # The enrich LLM stated a country → evidenced (even if it said US
        # with no state — that's the model's explicit statement).
        (None, {"hq_country": "US"}, False),
        # Blank stated country is no statement.
        (None, {"hq_country": "  "}, True),
    ],
)
def test_is_unevidenced_us(
    hq_state: str | None,
    payload: dict[str, object] | None,
    expected: bool,
) -> None:
    assert (
        is_unevidenced_us(hq_state=hq_state, enriched_payload=payload)
        is expected
    )
