"""Unit tests for infer-hq-country pure helpers + apply logic (no DB, no network)."""

from __future__ import annotations

from datetime import UTC, datetime

from nous.db.models import Company
from nous.llm.prompts.hq_country import HqCountryJudgment
from nous.pipeline.infer_hq_country import (
    InferHqCountrySummary,
    _apply_judgment,
    _candidate_urls,
    _evidence_supported,
    _normalize_iso2,
)

NOW = datetime(2026, 6, 15, tzinfo=UTC)


def _co() -> Company:
    return Company(name="Acme", slug="acme", normalized_name="acme",
                   website="https://acme.example/", description_short="Does things.")


def test_normalize_iso2() -> None:
    assert _normalize_iso2("dk") == "DK"
    assert _normalize_iso2("  Us ") == "US"
    assert _normalize_iso2("USA") is None      # not 2 letters
    assert _normalize_iso2("") is None
    assert _normalize_iso2(None) is None


def test_candidate_urls_same_origin_deduped() -> None:
    urls = _candidate_urls("https://acme.example/some/path?x=1")
    assert urls[0] == "https://acme.example/about"
    assert "https://acme.example/contact" in urls
    assert all(u.startswith("https://acme.example/") for u in urls)
    assert len(urls) == len(set(urls))


def test_candidate_urls_bad_input() -> None:
    assert _candidate_urls("not-a-url") == []
    assert _candidate_urls("") == []


def test_evidence_supported_substring_match_returns_source_url() -> None:
    sources = [("https://acme.example/contact", "Acme GmbH\n  Berlin, GERMANY")]
    # Normalized (case + whitespace) substring match.
    assert _evidence_supported("berlin, germany", sources) == (
        "https://acme.example/contact"
    )


def test_evidence_supported_rejects_absent_or_trivial_quote() -> None:
    sources = [("u", "We build software for teams.")]
    assert _evidence_supported("Hamburg, Germany", sources) is None  # not present
    assert _evidence_supported(None, sources) is None
    assert _evidence_supported("a", sources) is None                 # too short


def test_apply_non_us_excludes_with_source() -> None:
    co = _co()
    summary = InferHqCountrySummary()
    j = HqCountryJudgment(hq_country="DE", evidence_quote="Berlin, Germany")
    sources = [("https://acme.example/contact", "Acme GmbH, Berlin, Germany")]
    _apply_judgment(co, j, sources, now=NOW, summary=summary, dry_run=False)
    assert co.hq_country == "DE"
    assert co.exclusion_reason == "non_us"
    assert "https://acme.example/contact" in (co.exclusion_detail or "")
    assert "Berlin, Germany" in (co.exclusion_detail or "")
    assert co.excluded_at == NOW
    assert co.hq_country_checked_at == NOW
    assert summary.excluded_non_us == 1


def test_apply_us_sets_country_no_exclusion() -> None:
    co = _co()
    summary = InferHqCountrySummary()
    j = HqCountryJudgment(hq_country="US", evidence_quote="San Francisco, CA")
    sources = [("https://acme.example/contact", "Acme Inc, San Francisco, CA")]
    _apply_judgment(co, j, sources, now=NOW, summary=summary, dry_run=False)
    assert co.hq_country == "US"
    assert co.exclusion_reason is None
    assert co.hq_country_checked_at == NOW
    assert summary.set_us == 1


def test_apply_unsupported_evidence_leaves_unknown() -> None:
    co = _co()
    summary = InferHqCountrySummary()
    # Country claimed, but the quote is NOT in the source text -> do not act.
    j = HqCountryJudgment(hq_country="DE", evidence_quote="Munich, Germany")
    sources = [("u", "We build great software.")]
    _apply_judgment(co, j, sources, now=NOW, summary=summary, dry_run=False)
    assert co.hq_country is None
    assert co.exclusion_reason is None
    assert co.hq_country_checked_at == NOW
    assert summary.left_unknown == 1


def test_evidence_supported_rejects_substring_inside_word() -> None:
    # "usa" must NOT match inside "usable" — guards coincidental fragments.
    assert _evidence_supported("usa", [("u", "Our usable software platform")]) is None


def test_evidence_supported_accepts_single_token_at_word_boundary() -> None:
    # A bare city at a word boundary is still valid evidence (recall preserved).
    assert _evidence_supported(
        "copenhagen", [("u", "We are based in Copenhagen.")]
    ) == "u"


def test_apply_dry_run_writes_nothing_but_counts() -> None:
    co = _co()
    summary = InferHqCountrySummary()
    j = HqCountryJudgment(hq_country="DE", evidence_quote="Berlin, Germany")
    sources = [("https://acme.example/contact", "Acme GmbH, Berlin, Germany")]
    _apply_judgment(co, j, sources, now=NOW, summary=summary, dry_run=True)
    assert co.hq_country is None
    assert co.exclusion_reason is None
    assert co.hq_country_checked_at is None
    assert summary.excluded_non_us == 1  # intent still counted
