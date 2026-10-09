"""Pure-unit tests for dedup merge safety (no DB required).

Covers the pieces of the dedup-merge-safety fix that don't need Postgres:
- the domain pass's name-corroboration rule (registrable_label,
  name_explains_domain_label, names_corroborate_domain) and how it partitions
  a shared-domain cluster into auto-mergeable groups;
- merge_companies' in-memory gap-fill: provenance followers travel with
  description_short, and career_extracted_prompt_version takes the lower of
  the two halves' stamps.

The DB round trips (child-table carry-over, LLM routing of uncorroborated
domain pairs) live in test_dedup_companies.py behind the DATABASE_URL gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nous.db.models import Company
from nous.db.upsert import (
    _MERGE_FILL_COLUMNS,
    _fill_survivor_gaps,
    _merged_career_stamp,
)
from nous.pipeline.dedup_companies import (
    _CompanyRow,
    _corroborated_groups,
    name_explains_domain_label,
    names_corroborate_domain,
    registrable_label,
)

# ---------------------------------------------------------------------------
# registrable_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "label"),
    [
        ("acme.com", "acme"),
        ("app.acme.com", "acme"),
        ("get-clay.com", "getclay"),
        ("acme.co.uk", "acme"),
        ("acme.com.au", "acme"),
        ("perplexity.ai", "perplexity"),
        ("co.uk", "co"),  # degenerate: no registrable part beyond the suffix
        ("localhost", "localhost"),
        ("", ""),
    ],
)
def test_registrable_label(domain: str, label: str) -> None:
    assert registrable_label(domain) == label


# ---------------------------------------------------------------------------
# name_explains_domain_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Acme", "acme"),  # whole name
        ("Acme, Inc.", "acme"),  # corporate suffix stripped
        ("Acme Robotics", "acme"),  # leading token
        ("Acme Robotics", "acmerobotics"),  # all tokens
        ("Open AI", "openai"),  # multi-token concatenation
        ("Monday.com", "monday"),  # punctuation splits tokens
        ("Clay", "getclay"),  # marketing prefix
        ("Acme", "acmehq"),  # marketing suffix
        ("X", "x"),  # whole-name match is exempt from the length floor
        ("Café Labs", "cafe"),  # unicode folded
    ],
)
def test_name_explains_label(name: str, label: str) -> None:
    assert name_explains_domain_label(name, label)


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Kalshi", "frenflow"),  # the known wrong-website class
        ("Acme Robotics", "robotics"),  # a non-leading token
        ("Arc Boats", "arc"),  # partial prefix below the length floor
        ("Ai Labs", "ai"),  # partial prefix below the length floor
        ("Acme", "acmededupalias"),  # label longer than the name
        ("Acme", "notacme"),  # unknown prefix is not stripped
        ("Acme", ""),
        ("", "acme"),
    ],
)
def test_name_does_not_explain_label(name: str, label: str) -> None:
    assert not name_explains_domain_label(name, label)


# ---------------------------------------------------------------------------
# names_corroborate_domain
# ---------------------------------------------------------------------------


def test_equal_normalized_names_corroborate_any_domain() -> None:
    # Neither name spells the domain, but they are the same name.
    assert names_corroborate_domain("Acme, Inc.", "ACME", "unrelated-site.com")


def test_both_names_explaining_the_label_corroborate() -> None:
    assert names_corroborate_domain("Acme Robotics", "Acme Inc", "acme.com")
    assert names_corroborate_domain("Hooli", "Hooli XYZ", "www.hooli.com")
    assert names_corroborate_domain("Clay", "Clay Labs", "getclay.com")


def test_intruder_name_does_not_corroborate() -> None:
    """A Kalshi row carrying FrenFlow's site must NOT auto-merge into FrenFlow,
    in either argument order."""
    assert not names_corroborate_domain("FrenFlow", "Kalshi", "frenflow.com")
    assert not names_corroborate_domain("Kalshi", "FrenFlow", "frenflow.com")


def test_neither_name_explaining_the_label_does_not_corroborate() -> None:
    assert not names_corroborate_domain("Acme Robotics", "Acme Inc", "zzz.com")


def test_empty_names_never_corroborate() -> None:
    assert not names_corroborate_domain("", "", "acme.com")
    assert not names_corroborate_domain("Inc.", "LLC", "acme.com")


# ---------------------------------------------------------------------------
# _corroborated_groups
# ---------------------------------------------------------------------------


def _row(
    name: str,
    *,
    description_long: str | None = None,
    created_day: int = 1,
) -> _CompanyRow:
    return _CompanyRow(
        id=uuid4(),
        name=name,
        normalized_name=name.lower(),
        website="https://frenflow.com",
        hq_city=None,
        hq_state=None,
        description_short=None,
        description_long=description_long,
        latest_round_amount=None,
        latest_round_date=None,
        latest_round_type=None,
        created_at=datetime(2026, 1, created_day, tzinfo=UTC),
    )


def test_groups_split_intruder_from_corroborated_members() -> None:
    # Kalshi is the most-enriched row, so the OLD rule would have made it the
    # survivor and folded both FrenFlow rows into it.
    kalshi = _row("Kalshi", description_long="Prediction markets.")
    frenflow = _row("FrenFlow", created_day=2)
    frenflow_inc = _row("FrenFlow Inc", created_day=3)

    groups = _corroborated_groups([frenflow_inc, kalshi, frenflow], "frenflow.com")

    assert [[r.name for r in g] for g in groups] == [
        ["Kalshi"],
        ["FrenFlow", "FrenFlow Inc"],
    ]


def test_groups_join_transitively() -> None:
    """Equality and label edges chain: "Acme" ≡ "ACME Inc" (equal names) and
    "Acme" / "Acme Robotics" both spell acme.com → one group."""
    a = _row("Acme Robotics")
    b = _row("Acme", created_day=2)
    c = _row("ACME Inc", created_day=3)

    groups = _corroborated_groups([c, b, a], "acme.com")

    assert len(groups) == 1
    assert {r.name for r in groups[0]} == {"Acme Robotics", "Acme", "ACME Inc"}


def test_groups_all_singletons_when_nothing_corroborates() -> None:
    groups = _corroborated_groups([_row("Kalshi"), _row("Polymarket")], "frenflow.com")
    assert [len(g) for g in groups] == [1, 1]


# ---------------------------------------------------------------------------
# merge gap-fill: provenance followers + career stamp
# ---------------------------------------------------------------------------


def _company(**kwargs: object) -> Company:
    defaults: dict[str, object] = {
        "name": "Acme",
        "slug": f"acme-{uuid4().hex[:8]}",
        "normalized_name": "acme",
    }
    defaults.update(kwargs)
    return Company(**defaults)


def test_borrowed_description_brings_its_provenance() -> None:
    survivor = _company(describe_fallback_prompt_version="2026-07-01.1")
    loser = _company(
        description_short="Acme builds robots.",
        description_source="fallback",
        describe_fallback_prompt_version="2026-08-01.1",
    )
    _fill_survivor_gaps(survivor, loser)
    assert survivor.description_short == "Acme builds robots."
    assert survivor.description_source == "fallback"
    assert survivor.describe_fallback_prompt_version == "2026-08-01.1"


def test_borrowed_own_site_description_clears_fallback_provenance() -> None:
    """The loser's own-website description arrives with source NULL — it must
    not keep a stale 'fallback' label from the survivor."""
    survivor = _company(description_source="fallback")
    loser = _company(description_short="Own-site copy.", description_source=None)
    _fill_survivor_gaps(survivor, loser)
    assert survivor.description_short == "Own-site copy."
    assert survivor.description_source is None


def test_kept_description_keeps_survivor_provenance() -> None:
    survivor = _company(description_short="Survivor copy.", description_source=None)
    loser = _company(
        description_short="Loser copy.",
        description_source="fallback",
        describe_fallback_prompt_version="2026-08-01.1",
    )
    _fill_survivor_gaps(survivor, loser)
    assert survivor.description_short == "Survivor copy."
    assert survivor.description_source is None
    assert survivor.describe_fallback_prompt_version is None


def test_plain_fill_columns_still_gap_fill() -> None:
    survivor = _company(hq_city=None, website="https://acme.com")
    loser = _company(hq_city="Denver", website="https://other.com")
    _fill_survivor_gaps(survivor, loser)
    assert survivor.hq_city == "Denver"
    assert survivor.website == "https://acme.com"


def test_followers_are_not_independent_fill_columns() -> None:
    assert "description_source" not in _MERGE_FILL_COLUMNS
    assert "describe_fallback_prompt_version" not in _MERGE_FILL_COLUMNS
    assert "career_extracted_prompt_version" not in _MERGE_FILL_COLUMNS


@pytest.mark.parametrize(
    ("survivor", "loser", "merged"),
    [
        ("2026-07-01.1", "2026-07-01.1", "2026-07-01.1"),
        ("2026-08-01.1", "2026-07-01.1", "2026-07-01.1"),
        ("2026-07-01.1", "2026-08-01.2", "2026-07-01.1"),
        (None, "2026-07-01.1", None),  # survivor's own pages never mined
        ("2026-07-01.1", None, None),  # loser's pages never mined
        (None, None, None),
    ],
)
def test_merged_career_stamp_is_the_lower_version(
    survivor: str | None, loser: str | None, merged: str | None
) -> None:
    assert _merged_career_stamp(survivor, loser) == merged


def test_fill_applies_career_stamp_rule() -> None:
    survivor = _company(career_extracted_prompt_version="2026-08-01.1")
    loser = _company(career_extracted_prompt_version=None)
    _fill_survivor_gaps(survivor, loser)
    # Re-selected by extract-career-history's version gate.
    assert survivor.career_extracted_prompt_version is None
