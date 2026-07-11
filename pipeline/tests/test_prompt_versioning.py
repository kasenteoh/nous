"""Pure-unit tests for prompt-version provenance stamping (no DB required).

Covers the pieces of W-E.2 that don't need Postgres:
- every persisting prompt module exposes a PROMPT_VERSION in the shared
  date-based scheme;
- the models carry the nullable TEXT stamp columns migration 0031 adds;
- the sync applier helpers (extract-funding's status/total-raised, the
  infer-hq-country judgment, the funding-round merge) stamp the version
  exactly when they write LLM-derived content, and never otherwise;
- merge_companies' gap-fill list carries the stamps whose content columns
  travel on merge, and only those.

The stage-level round trips (enrich / judge / analyze / reconcile) live in
test_prompt_versioning_db.py behind the usual DATABASE_URL gate.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Text

from nous.db.models import Company, Competitor, FundingRound
from nous.db.upsert import _MERGE_FILL_COLUMNS, _merge_extraction_into_round
from nous.llm.prompts import (
    company_description,
    company_eligibility,
    competitor_analysis,
    funding_extraction,
    hq_country,
)
from nous.llm.prompts.funding_extraction import FundingExtraction
from nous.llm.prompts.hq_country import HqCountryJudgment
from nous.pipeline.extract_funding import (
    _apply_status_event,
    _apply_total_raised,
)
from nous.pipeline.infer_hq_country import (
    InferHqCountrySummary,
    _apply_judgment,
)

# The scheme every PROMPT_VERSION uses: "<date-of-change>.<same-day-counter>".
_VERSION_SCHEME = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")

# Every prompt module that persists LLM output — the audit result W-E.2 is
# built on. news_company / company_match / competitor_candidates produce
# decisions or pass-1 inputs, not persisted content columns, so they are
# deliberately absent.
_PERSISTING_PROMPTS = (
    company_description,
    company_eligibility,
    competitor_analysis,
    funding_extraction,
    hq_country,
)


def test_every_persisting_prompt_has_a_version() -> None:
    for module in _PERSISTING_PROMPTS:
        version = module.PROMPT_VERSION
        assert isinstance(version, str)
        assert _VERSION_SCHEME.match(version), (
            f"{module.__name__}.PROMPT_VERSION={version!r} does not match "
            "the '<YYYY-MM-DD>.<n>' scheme"
        )


def test_models_have_stamp_columns() -> None:
    """The 0031 columns exist on the models, nullable TEXT, exactly as the
    migration declares them."""
    company_cols = Company.__table__.c
    for name in (
        "enrichment_prompt_version",
        "eligibility_prompt_version",
        "hq_country_prompt_version",
        "funding_prompt_version",
    ):
        column = company_cols[name]
        assert isinstance(column.type, Text)
        assert column.nullable
    for table in (FundingRound.__table__, Competitor.__table__):
        column = table.c["prompt_version"]
        assert isinstance(column.type, Text)
        assert column.nullable


def test_merge_fill_carries_the_travelling_stamps_only() -> None:
    """Stamps whose content columns gap-fill on a dedup merge travel with
    them; the ones whose content columns don't are deliberately absent."""
    assert "enrichment_prompt_version" in _MERGE_FILL_COLUMNS
    assert "hq_country_prompt_version" in _MERGE_FILL_COLUMNS
    assert "eligibility_prompt_version" not in _MERGE_FILL_COLUMNS
    assert "funding_prompt_version" not in _MERGE_FILL_COLUMNS


# ---------------------------------------------------------------------------
# extract-funding company-side appliers
# ---------------------------------------------------------------------------


def _company(**kwargs: object) -> Company:
    defaults: dict[str, object] = {
        "name": "Acme Corp",
        "slug": "acme-prompt-ver",
        "normalized_name": "acme corp",
        "status": "active",
    }
    defaults.update(kwargs)
    return Company(**defaults)


def _extraction(**kwargs: object) -> FundingExtraction:
    defaults: dict[str, object] = {
        "is_funding_announcement": False,
        "confidence": "high",
    }
    defaults.update(kwargs)
    return FundingExtraction(**defaults)


def test_status_change_stamps_funding_prompt_version() -> None:
    company = _company()
    outcome = _apply_status_event(
        company,
        _extraction(status_event="acquired", status_confidence="high"),
        source_url="https://news.example/deal",
    )
    assert outcome == "changed"
    assert company.funding_prompt_version == funding_extraction.PROMPT_VERSION


def test_status_source_backfill_restamps() -> None:
    """A backfill re-confirms the status via the current prompt, so the row
    joins that revision's re-check cohort."""
    company = _company(status="acquired", status_source_url=None)
    outcome = _apply_status_event(
        company,
        _extraction(status_event="acquired", status_confidence="medium"),
        source_url="https://news.example/deal",
    )
    assert outcome == "backfilled"
    assert company.funding_prompt_version == funding_extraction.PROMPT_VERSION


def test_low_confidence_status_leaves_stamp_untouched() -> None:
    company = _company()
    outcome = _apply_status_event(
        company,
        _extraction(status_event="acquired", status_confidence="low"),
        source_url="https://news.example/deal",
    )
    assert outcome is None
    assert company.funding_prompt_version is None


def test_total_raised_apply_stamps_funding_prompt_version() -> None:
    company = _company()
    applied = _apply_total_raised(
        company,
        _extraction(total_raised_usd=Decimal("285000000")),
        source_url="https://news.example/total",
        as_of=date(2026, 7, 1),
    )
    assert applied is True
    assert company.funding_prompt_version == funding_extraction.PROMPT_VERSION


def test_no_total_leaves_stamp_untouched() -> None:
    company = _company()
    applied = _apply_total_raised(
        company,
        _extraction(),
        source_url="https://news.example/total",
        as_of=date(2026, 7, 1),
    )
    assert applied is False
    assert company.funding_prompt_version is None


# ---------------------------------------------------------------------------
# infer-hq-country judgment applier
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 10, tzinfo=UTC)
_ABOUT_URL = "https://acme.example/about"


def test_non_us_judgment_stamps_hq_country_prompt_version() -> None:
    company = _company()
    _apply_judgment(
        company,
        HqCountryJudgment(
            hq_country="DK", evidence_quote="Copenhagen, Denmark"
        ),
        [(_ABOUT_URL, "Our office: Copenhagen, Denmark — come visit.")],
        now=_NOW,
        summary=InferHqCountrySummary(),
        dry_run=False,
    )
    assert company.hq_country == "DK"
    assert company.hq_country_prompt_version == hq_country.PROMPT_VERSION


def test_us_confirmation_stamps_hq_country_prompt_version() -> None:
    company = _company()
    _apply_judgment(
        company,
        HqCountryJudgment(
            hq_country="US", evidence_quote="San Francisco, CA 94107"
        ),
        [(_ABOUT_URL, "HQ: 500 Market St, San Francisco, CA 94107.")],
        now=_NOW,
        summary=InferHqCountrySummary(),
        dry_run=False,
    )
    assert company.hq_country == "US"
    assert company.hq_country_prompt_version == hq_country.PROMPT_VERSION


def test_unknown_judgment_leaves_stamp_null() -> None:
    """No content written → no stamp; only the attempt is recorded."""
    company = _company()
    _apply_judgment(
        company,
        HqCountryJudgment(),
        [(_ABOUT_URL, "A page that states no location at all.")],
        now=_NOW,
        summary=InferHqCountrySummary(),
        dry_run=False,
    )
    assert company.hq_country is None
    assert company.hq_country_prompt_version is None
    assert company.hq_country_checked_at == _NOW


def test_dry_run_never_stamps() -> None:
    company = _company()
    _apply_judgment(
        company,
        HqCountryJudgment(
            hq_country="DK", evidence_quote="Copenhagen, Denmark"
        ),
        [(_ABOUT_URL, "Our office: Copenhagen, Denmark — come visit.")],
        now=_NOW,
        summary=InferHqCountrySummary(),
        dry_run=True,
    )
    assert company.hq_country is None
    assert company.hq_country_prompt_version is None


# ---------------------------------------------------------------------------
# funding-round merge
# ---------------------------------------------------------------------------


def test_merge_into_existing_round_restamps() -> None:
    """A merge folds the new extraction's content into the row, so the stamp
    is refreshed to the current version (last-writer-wins)."""
    existing = FundingRound(
        company_id=uuid4(),
        round_type="Series A",
        amount_raised=None,
        announced_date=date(2026, 6, 1),
        primary_news_url="https://news.example/first",
        extraction_confidence="low",
        prompt_version=None,  # pre-versioning row
    )
    _merge_extraction_into_round(
        existing,
        _extraction(
            round_type="Series A",
            amount_raised_usd=Decimal("50000000"),
            announced_date=date(2026, 6, 3),
        ),
    )
    assert existing.amount_raised == Decimal("50000000")
    assert existing.prompt_version == funding_extraction.PROMPT_VERSION
