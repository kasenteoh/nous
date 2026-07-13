"""Canonical US state normalization — a pure USPS-code map, no deps.

``companies.hq_state`` entered the catalog in inconsistent forms: some rows
carry the 2-letter code ("CA"), others the full name ("California") or odd
casing ("ca", "CA "). The enrichment prompt asks the LLM for a 2-letter code,
but historical rows and mixed sources left the column ragged, and the web
renders whatever casing is stored.

The web location route (``web/app/location/[state]/page.tsx``) resolves a URL
segment by **uppercasing** it and matching it against the stored ``hq_state``
(``q.eq("hq_state", opts.state)`` in ``web/lib/queries.ts``). So the form the
routing already expects — and the only form that resolves — is the **2-letter
UPPERCASE USPS code**. This module makes that the single canonical storage form,
which is strictly routing-safe: rows already "CA" are untouched, and full-name
rows (whose ``/location/California`` links 404 today because the route
uppercases to "CALIFORNIA" and nothing is stored that way) start resolving to
the working ``/location/CA``.

Scope: the 50 states + DC. Foreign regions, US territories, and unrecognized
junk return ``None`` from :func:`canonical_us_state`, so callers leave those
values untouched rather than overwriting a non-US value with a guess.
"""

from __future__ import annotations

# Canonical STORED form is the 2-letter uppercase USPS code (the key); the full
# name is the display expansion. 50 states + DC.
US_STATE_CODE_TO_NAME: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

# The already-canonical values: the 2-letter uppercase codes. Membership here is
# the "already normalized" test.
US_STATE_CODES: frozenset[str] = frozenset(US_STATE_CODE_TO_NAME)

# Lowercased full name -> code, for the reverse lookup. Includes the common
# "Washington DC" spellings the LLM tends to emit for the District of Columbia.
US_STATE_NAME_TO_CODE: dict[str, str] = {
    name.lower(): code for code, name in US_STATE_CODE_TO_NAME.items()
}
US_STATE_NAME_TO_CODE.update(
    {
        "washington dc": "DC",
        "washington d.c.": "DC",
        "washington, d.c.": "DC",
    }
)


def canonical_us_state(value: str | None) -> str | None:
    """Return the canonical 2-letter uppercase USPS code for a US state.

    Handles case and surrounding whitespace, accepting either a code or a full
    name::

        "California" / "CALIFORNIA" / "california" -> "CA"
        "ca" / "CA " / " Ca " -> "CA"
        "CA" -> "CA"  (already canonical — returned unchanged)
        "Washington DC" -> "DC"

    Returns ``None`` for anything that is not one of the 50 states or DC —
    foreign regions ("Ontario", "London"), US territories, a city mistakenly in
    the state slot, and empty/whitespace/garbage. Callers use ``None`` as the
    signal to leave the stored value untouched rather than clobbering a non-US
    value with a guess.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    upper = stripped.upper()
    if upper in US_STATE_CODES:
        return upper
    return US_STATE_NAME_TO_CODE.get(stripped.lower())
