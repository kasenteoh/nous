"""Prompt specs: what the harness knows about each evaluated prompt.

A :class:`PromptSpec` bundles, for one prompt:

- the response schema (the SAME Pydantic model the runtime validates with),
- how to build the live prompt from a fixture case (record mode),
- how to score parsed responses against ground truth (offline mode).

The harness core (:mod:`nous.evals.harness`) is generic over specs, so
adding a prompt to the golden set means writing one spec + fixtures — no
harness changes. Currently scoped to the highest-value prompts:
``company_description`` (the judge), ``company_description_long`` (the
dedicated long-form profile), ``funding_extraction``, and ``career_history``
(the talent-flow founder-background rider).

Scoring runs on responses that already passed the runtime
parse/validate path (``schema.model_validate_json`` — including model
validators like company_description's implausible-roster drop) and applies
the same post-validation normalization the calling stage applies
(tag normalization + industry canonicalization for enrichment).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from nous.evals.schema import CaseSpec, PromptReport
from nous.evals.scoring import (
    Accuracy,
    SlotTally,
    grounding_fraction,
    mean,
    paragraph_count,
    word_count,
)
from nous.llm.client import MAX_PROMPT_INPUT_CHARS
from nous.llm.prompts.career_history import CareerHistoryExtraction
from nous.llm.prompts.career_history import build_prompt as build_career_history_prompt
from nous.llm.prompts.company_description import CompanyDescription
from nous.llm.prompts.company_description import build_prompt as build_description_prompt
from nous.llm.prompts.company_description_long import (
    MAX_DESCRIPTION_INPUT_CHARS,
    CompanyLongDescription,
)
from nous.llm.prompts.company_description_long import (
    build_prompt as build_long_description_prompt,
)
from nous.llm.prompts.describe_fallback import (
    MAX_EVIDENCE_CHARS as DESCRIBE_FALLBACK_MAX_EVIDENCE_CHARS,
)
from nous.llm.prompts.describe_fallback import (
    DescribeFallbackResult,
)
from nous.llm.prompts.describe_fallback import (
    build_prompt as build_describe_fallback_prompt,
)
from nous.llm.prompts.funding_extraction import (
    FundingExtraction,
)
from nous.llm.prompts.funding_extraction import (
    build_prompt as build_funding_prompt,
)
from nous.llm.prompts.funding_extraction import (
    build_website_prompt as build_funding_website_prompt,
)
from nous.llm.prompts.source_verification import (
    SourceVerification,
    quote_is_grounded,
)
from nous.llm.prompts.source_verification import (
    build_prompt as build_source_verification_prompt,
)

# The runtime moat check, reused verbatim so the golden set verifies the SAME
# descriptor-in-evidence gate the stage applies (the source_verification golden
# set reuses quote_is_grounded the same way).
from nous.pipeline.describe_fallback import _descriptor_in_evidence
from nous.util.industry import normalize_industry
from nous.util.slugify import normalize_name
from nous.util.text import truncate_to_chars

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


@dataclass(frozen=True)
class CaseEvaluation:
    """One fixture case, loaded and parsed, ready for scoring."""

    case_id: str
    spec: CaseSpec
    input_text: str
    expected: BaseModel
    # None when the recorded response failed runtime schema validation —
    # counted by the gated parse_rate metric and excluded from field metrics.
    recorded: BaseModel | None


@dataclass(frozen=True)
class PromptSpec:
    """Everything the generic harness needs to eval one prompt."""

    name: str
    schema: type[BaseModel]
    build_prompt: Callable[[CaseSpec, str], str]
    score: Callable[[Sequence[CaseEvaluation]], PromptReport]


def _issue(issues: dict[str, list[str]], case_id: str, message: str) -> None:
    issues.setdefault(case_id, []).append(message)


# ---------------------------------------------------------------------------
# company_description (the judge: classification + description_short)
# ---------------------------------------------------------------------------

_MAX_SHORT_CHARS = 450


def _build_company_description_prompt(case: CaseSpec, input_text: str) -> str:
    # Mirror enrich_companies: truncate the cleaned text before building.
    cleaned = truncate_to_chars(input_text, MAX_PROMPT_INPUT_CHARS)
    return build_description_prompt(company_name=case.company_name, cleaned_text=cleaned)


def _normalize_industry_label(value: str | None) -> str | None:
    normalized = normalize_industry(value)
    return normalized.casefold() if normalized is not None else None


def _description_structure_ok(
    recorded: CompanyDescription,
) -> tuple[bool, list[str]]:
    """Bounds on the judge's only free-text field, description_short.

    The long-description structure checks (paragraph/word floors) moved to
    the dedicated company_description_long scorer with the W-F prompt split.
    """
    problems: list[str] = []
    short = recorded.description_short.strip()
    if not short:
        problems.append("description_short is empty")
    elif len(short) > _MAX_SHORT_CHARS:
        problems.append(f"description_short too long ({len(short)} > {_MAX_SHORT_CHARS} chars)")
    return (not problems, problems)


def score_company_description(cases: Sequence[CaseEvaluation]) -> PromptReport:
    """Score company_description recordings against ground truth.

    Gated metrics:
    - parse_rate — recordings surviving runtime schema validation.
    - website_state_accuracy / is_startup_accuracy — exact match
      (``is_startup`` includes None: an unwarranted true/false is a miss).
    - slots_* — micro P/R/F1 over the nullable extraction slots
      (industry after canonicalization, hq_city, hq_state, hq_country,
      founded_year).
    - people_precision / people_recall — casefolded name sets, AFTER the
      schema's implausible-roster validator has run (the runtime path).
    - structure_pass_rate — length bounds on description_short (the judge's
      only free-text field since the W-F split).
    - grounding_mean / grounding_min — no-fabrication proxy: proper nouns
      and numbers in description_short must appear in the input text.

    Informational (not gated): tags_* — canonicalized-tag set overlap
    (both sides pass through the runtime alias map in util/tags.py, so
    near-synonyms like ci-observability/ci-cd count as agreement). Tags
    were briefly gated on exact-set F1, but the first live re-recording
    showed DeepSeek picks a systematically different (equally reasonable)
    tag vocabulary than a fixture author — exact overlap ran ~0.3 against a
    0.98 simulated floor. Tag quality is subjective and vocabulary-sensitive,
    so the metric is reported for visibility but does not gate.
    """
    from nous.util.tags import canonicalize_tag  # runtime tag normalizer + alias map

    issues: dict[str, list[str]] = {}
    parse = Accuracy()
    website_state = Accuracy()
    is_startup = Accuracy()
    category = Accuracy()
    structure = Accuracy()
    slots = SlotTally()
    people = SlotTally()
    tags = SlotTally()
    groundings: list[float] = []
    provenance: dict[str, int] = {}

    for case in cases:
        expected = case.expected
        assert isinstance(expected, CompanyDescription)
        parse.add(case.recorded is not None)
        if case.recorded is None:
            _issue(issues, case.case_id, "recorded response failed runtime schema validation")
            continue
        recorded = case.recorded
        assert isinstance(recorded, CompanyDescription)

        website_state.add(recorded.website_state == expected.website_state)
        if recorded.website_state != expected.website_state:
            _issue(
                issues,
                case.case_id,
                f"website_state: expected {expected.website_state!r},"
                f" got {recorded.website_state!r}",
            )
        is_startup.add(recorded.is_startup == expected.is_startup)
        if recorded.is_startup != expected.is_startup:
            _issue(
                issues,
                case.case_id,
                f"is_startup: expected {expected.is_startup!r}, got {recorded.is_startup!r}",
            )
        category.add(
            recorded.primary_category.strip().casefold()
            == expected.primary_category.strip().casefold()
        )

        scalar_slots: list[tuple[str, object | None, object | None]] = [
            (
                "industry",
                _normalize_industry_label(expected.industry),
                _normalize_industry_label(recorded.industry),
            ),
            (
                "hq_city",
                expected.hq_city.casefold() if expected.hq_city else None,
                recorded.hq_city.casefold() if recorded.hq_city else None,
            ),
            (
                "hq_state",
                expected.hq_state.upper() if expected.hq_state else None,
                recorded.hq_state.upper() if recorded.hq_state else None,
            ),
            (
                "hq_country",
                expected.hq_country.strip().upper() if expected.hq_country else None,
                recorded.hq_country.strip().upper() if recorded.hq_country else None,
            ),
            ("founded_year", expected.founded_year, recorded.founded_year),
        ]
        for field, exp_val, got_val in scalar_slots:
            slots.add(
                expected_present=exp_val is not None,
                got_present=got_val is not None,
                match=exp_val == got_val,
            )
            if exp_val != got_val:
                _issue(
                    issues, case.case_id, f"{field}: expected {exp_val!r}, got {got_val!r}"
                )

        expected_people = {p.name.strip().casefold() for p in expected.people}
        recorded_people = {p.name.strip().casefold() for p in recorded.people}
        people.add_sets(expected_people, recorded_people)
        if expected_people != recorded_people:
            _issue(
                issues,
                case.case_id,
                f"people: missing {sorted(expected_people - recorded_people)},"
                f" extra {sorted(recorded_people - expected_people)}",
            )

        expected_tags = {canonicalize_tag(t) for t in expected.tags if t.strip()}
        recorded_tags = {canonicalize_tag(t) for t in recorded.tags if t.strip()}
        tags.add_sets(expected_tags, recorded_tags)
        if expected_tags != recorded_tags:
            _issue(
                issues,
                case.case_id,
                f"tags: missing {sorted(expected_tags - recorded_tags)},"
                f" extra {sorted(recorded_tags - expected_tags)}",
            )

        structure_ok, structure_problems = _description_structure_ok(recorded)
        structure.add(structure_ok)
        for problem in structure_problems:
            _issue(issues, case.case_id, f"structure: {problem}")

        source = f"{case.input_text}\n{case.spec.company_name}"
        grounding = grounding_fraction(recorded.description_short, source)
        groundings.append(grounding)
        if grounding < 1.0:
            _issue(
                issues,
                case.case_id,
                f"grounding: {grounding:.3f} — description asserts tokens absent from input",
            )

    metrics: dict[str, float] = {
        "parse_rate": parse.value,
        "website_state_accuracy": website_state.value,
        "is_startup_accuracy": is_startup.value,
        "slots_precision": slots.precision,
        "slots_recall": slots.recall,
        "slots_f1": slots.f1,
        "people_precision": people.precision,
        "people_recall": people.recall,
        "structure_pass_rate": structure.value,
        "grounding_mean": mean(groundings),
        "grounding_min": min(groundings) if groundings else 1.0,
        # Informational (not gated): free-string category label agreement,
        # and tag overlap (vocabulary-sensitive — see docstring).
        "primary_category_accuracy": category.value,
        "tags_f1": tags.f1,
        "tags_precision": tags.precision,
        "tags_recall": tags.recall,
    }
    return PromptReport(
        prompt="company_description",
        case_count=len(cases),
        provenance_counts=provenance,
        metrics=metrics,
        gated=[
            "parse_rate",
            "website_state_accuracy",
            "is_startup_accuracy",
            "slots_precision",
            "slots_recall",
            "slots_f1",
            "people_precision",
            "people_recall",
            "structure_pass_rate",
            "grounding_mean",
            "grounding_min",
        ],
        issues=issues,
    )


# ---------------------------------------------------------------------------
# company_description_long (the dedicated long-form profile)
# ---------------------------------------------------------------------------

# Inputs at or above this length are "rich": the prompt's depth floor
# (>= 4 substantial paragraphs, ~350-600 words) applies. Below it the honest
# response is fewer, shorter paragraphs — padding a thin site is a failure.
_LONG_RICH_INPUT_CHARS = 1_500
# Rich-input floors. The word floor sits slightly below the prompt's ~350
# target so a tight-but-real 4-paragraph profile doesn't flap the gate.
_LONG_RICH_MIN_PARAGRAPHS = 4
_LONG_RICH_MIN_WORDS = 300
# Ceilings (any input): beyond these the model is padding, not profiling.
_LONG_MAX_PARAGRAPHS = 8
_LONG_MAX_WORDS = 800
# Thin-input caps: an honest profile of a thin site is short. Exceeding
# these on a sub-rich input is the padding signature the prompt forbids.
_LONG_THIN_MAX_PARAGRAPHS = 3
_LONG_THIN_MAX_WORDS = 250


def _build_company_description_long_prompt(case: CaseSpec, input_text: str) -> str:
    # Mirror enrich_companies: the describe call gets the LARGER 48k budget.
    cleaned = truncate_to_chars(input_text, MAX_DESCRIPTION_INPUT_CHARS)
    return build_long_description_prompt(
        company_name=case.company_name, cleaned_text=cleaned
    )


def _long_structure_ok(
    description: str, *, rich: bool
) -> tuple[bool, list[str]]:
    problems: list[str] = []
    paragraphs = paragraph_count(description)
    words = word_count(description)
    if rich:
        if paragraphs < _LONG_RICH_MIN_PARAGRAPHS:
            problems.append(
                f"only {paragraphs} paragraphs on a rich input"
                f" (floor {_LONG_RICH_MIN_PARAGRAPHS})"
            )
        if words < _LONG_RICH_MIN_WORDS:
            problems.append(
                f"only {words} words on a rich input (floor {_LONG_RICH_MIN_WORDS})"
            )
    else:
        if paragraphs > _LONG_THIN_MAX_PARAGRAPHS:
            problems.append(
                f"{paragraphs} paragraphs on a thin input"
                f" (cap {_LONG_THIN_MAX_PARAGRAPHS}) — padding"
            )
        if words > _LONG_THIN_MAX_WORDS:
            problems.append(
                f"{words} words on a thin input (cap {_LONG_THIN_MAX_WORDS}) — padding"
            )
    if paragraphs > _LONG_MAX_PARAGRAPHS:
        problems.append(
            f"{paragraphs} paragraphs (cap {_LONG_MAX_PARAGRAPHS})"
        )
    if words > _LONG_MAX_WORDS:
        problems.append(f"{words} words (cap {_LONG_MAX_WORDS})")
    return (not problems, problems)


def score_company_description_long(cases: Sequence[CaseEvaluation]) -> PromptReport:
    """Score company_description_long recordings against ground truth.

    Gated metrics:
    - parse_rate — recordings surviving runtime schema validation.
    - insufficiency_accuracy — null agreement: the model returns null exactly
      when the hand-checked expectation is null. Describing an undescribable
      site and refusing a describable one both cost this metric.
    - structure_pass_rate — depth floors on rich inputs (>= 4 paragraphs,
      >= 300 words) and padding caps on thin inputs (<= 3 paragraphs,
      <= 250 words), plus global ceilings. Only scored when both sides agree
      a description exists (null misses are already insufficiency misses).
    - grounding_mean / grounding_min — the no-fabrication proxy over the
      long description: proper nouns and numbers must appear in the input.

    Informational: rich_word_mean (average word count on rich non-null
    recordings — the "did descriptions actually get longer" dial).
    """
    issues: dict[str, list[str]] = {}
    parse = Accuracy()
    insufficiency = Accuracy()
    structure = Accuracy()
    groundings: list[float] = []
    rich_words: list[float] = []
    provenance: dict[str, int] = {}

    for case in cases:
        expected = case.expected
        assert isinstance(expected, CompanyLongDescription)
        parse.add(case.recorded is not None)
        if case.recorded is None:
            _issue(issues, case.case_id, "recorded response failed runtime schema validation")
            continue
        recorded = case.recorded
        assert isinstance(recorded, CompanyLongDescription)

        expected_null = expected.description_long is None
        recorded_null = recorded.description_long is None
        insufficiency.add(expected_null == recorded_null)
        if expected_null != recorded_null:
            _issue(
                issues,
                case.case_id,
                "insufficiency: expected "
                + ("null" if expected_null else "a description")
                + ", got "
                + ("null" if recorded_null else "a description"),
            )

        if expected_null or recorded.description_long is None:
            # Nothing further to check: a wrong null/non-null is already an
            # insufficiency miss, and a correct null has no text to score.
            continue

        rich = len(case.input_text) >= _LONG_RICH_INPUT_CHARS
        structure_ok, structure_problems = _long_structure_ok(
            recorded.description_long, rich=rich
        )
        structure.add(structure_ok)
        for problem in structure_problems:
            _issue(issues, case.case_id, f"structure: {problem}")
        if rich:
            rich_words.append(float(word_count(recorded.description_long)))

        source = f"{case.input_text}\n{case.spec.company_name}"
        grounding = grounding_fraction(recorded.description_long, source)
        groundings.append(grounding)
        if grounding < 1.0:
            _issue(
                issues,
                case.case_id,
                f"grounding: {grounding:.3f} — description asserts tokens absent from input",
            )

    metrics: dict[str, float] = {
        "parse_rate": parse.value,
        "insufficiency_accuracy": insufficiency.value,
        "structure_pass_rate": structure.value,
        "grounding_mean": mean(groundings),
        "grounding_min": min(groundings) if groundings else 1.0,
        # Informational (not gated): are rich descriptions actually long?
        "rich_word_mean": mean(rich_words),
    }
    return PromptReport(
        prompt="company_description_long",
        case_count=len(cases),
        provenance_counts=provenance,
        metrics=metrics,
        gated=[
            "parse_rate",
            "insufficiency_accuracy",
            "structure_pass_rate",
            "grounding_mean",
            "grounding_min",
        ],
        issues=issues,
    )


# ---------------------------------------------------------------------------
# funding_extraction
# ---------------------------------------------------------------------------


def _build_funding_prompt(case: CaseSpec, input_text: str) -> str:
    if case.variant == "website":
        return build_funding_website_prompt(
            company_name=case.company_name, page_text=input_text
        )
    return build_funding_prompt(company_name=case.company_name, article_text=input_text)


def _norm_investor(name: str) -> str:
    return " ".join(name.split()).casefold()


def score_funding_extraction(cases: Sequence[CaseEvaluation]) -> PromptReport:
    """Score funding_extraction recordings against ground truth.

    Gated metrics:
    - parse_rate — recordings surviving runtime schema validation.
    - announcement_accuracy — the is_funding_announcement gate, which
      decides whether a round row is written at all.
    - fields_* — micro P/R/F1 over the nullable scalar slots (round_type,
      amount_raised_usd, valuation_post_money_usd, announced_date,
      total_raised_usd, status_event). An invented amount is a false
      positive; a missed stated total is a false negative.
    - investors_* — micro P/R/F1 over ("lead"|"other", name) labeled sets,
      so promoting a participant to lead costs precision AND recall.

    Informational: confidence_accuracy, status_confidence_accuracy,
    valuation_source_presence_accuracy (free-string source attributions are
    compared by presence, not text).
    """
    issues: dict[str, list[str]] = {}
    parse = Accuracy()
    announcement = Accuracy()
    confidence = Accuracy()
    status_confidence = Accuracy()
    valuation_source_presence = Accuracy()
    fields = SlotTally()
    investors = SlotTally()
    provenance: dict[str, int] = {}

    for case in cases:
        expected = case.expected
        assert isinstance(expected, FundingExtraction)
        parse.add(case.recorded is not None)
        if case.recorded is None:
            _issue(issues, case.case_id, "recorded response failed runtime schema validation")
            continue
        recorded = case.recorded
        assert isinstance(recorded, FundingExtraction)

        announcement.add(
            recorded.is_funding_announcement == expected.is_funding_announcement
        )
        if recorded.is_funding_announcement != expected.is_funding_announcement:
            _issue(
                issues,
                case.case_id,
                f"is_funding_announcement: expected {expected.is_funding_announcement},"
                f" got {recorded.is_funding_announcement}",
            )

        scalar_slots: list[tuple[str, object | None, object | None]] = [
            (
                "round_type",
                expected.round_type.strip().casefold() if expected.round_type else None,
                recorded.round_type.strip().casefold() if recorded.round_type else None,
            ),
            ("amount_raised_usd", expected.amount_raised_usd, recorded.amount_raised_usd),
            (
                "valuation_post_money_usd",
                expected.valuation_post_money_usd,
                recorded.valuation_post_money_usd,
            ),
            ("announced_date", expected.announced_date, recorded.announced_date),
            ("total_raised_usd", expected.total_raised_usd, recorded.total_raised_usd),
            ("status_event", expected.status_event, recorded.status_event),
        ]
        for field, exp_val, got_val in scalar_slots:
            fields.add(
                expected_present=exp_val is not None,
                got_present=got_val is not None,
                match=exp_val == got_val,
            )
            if exp_val != got_val:
                _issue(
                    issues, case.case_id, f"{field}: expected {exp_val!r}, got {got_val!r}"
                )

        expected_investors = {("lead", _norm_investor(n)) for n in expected.lead_investors} | {
            ("other", _norm_investor(n)) for n in expected.other_investors
        }
        recorded_investors = {("lead", _norm_investor(n)) for n in recorded.lead_investors} | {
            ("other", _norm_investor(n)) for n in recorded.other_investors
        }
        investors.add_sets(
            {f"{role}:{name}" for role, name in expected_investors},
            {f"{role}:{name}" for role, name in recorded_investors},
        )
        if expected_investors != recorded_investors:
            _issue(
                issues,
                case.case_id,
                f"investors: missing {sorted(expected_investors - recorded_investors)},"
                f" extra {sorted(recorded_investors - expected_investors)}",
            )

        confidence.add(recorded.confidence == expected.confidence)
        status_confidence.add(recorded.status_confidence == expected.status_confidence)
        valuation_source_presence.add(
            (recorded.valuation_source is not None)
            == (expected.valuation_source is not None)
        )

    metrics: dict[str, float] = {
        "parse_rate": parse.value,
        "announcement_accuracy": announcement.value,
        "fields_precision": fields.precision,
        "fields_recall": fields.recall,
        "fields_f1": fields.f1,
        "investors_precision": investors.precision,
        "investors_recall": investors.recall,
        "investors_f1": investors.f1,
        # Informational (not gated).
        "confidence_accuracy": confidence.value,
        "status_confidence_accuracy": status_confidence.value,
        "valuation_source_presence_accuracy": valuation_source_presence.value,
    }
    return PromptReport(
        prompt="funding_extraction",
        case_count=len(cases),
        provenance_counts=provenance,
        metrics=metrics,
        gated=[
            "parse_rate",
            "announcement_accuracy",
            "fields_precision",
            "fields_recall",
            "fields_f1",
            "investors_precision",
            "investors_recall",
            "investors_f1",
        ],
        issues=issues,
    )


# ---------------------------------------------------------------------------
# career_history (founder prior-employer extraction — the talent-flow rider)
# ---------------------------------------------------------------------------


def _build_career_history_prompt(case: CaseSpec, input_text: str) -> str:
    # Mirror extract-career-history: truncate to the shared 32k budget and pass
    # the case's leadership roster as the attribution allow-list.
    cleaned = truncate_to_chars(input_text, MAX_PROMPT_INPUT_CHARS)
    return build_career_history_prompt(
        company_name=case.company_name, roster=case.roster, cleaned_text=cleaned
    )


def _career_people(extraction: CareerHistoryExtraction) -> set[str]:
    """Normalized names of founders/execs the extraction gave a prior employer."""
    return {normalize_name(p.name) for p in extraction.people if p.name}


def _career_edges(extraction: CareerHistoryExtraction) -> set[str]:
    """(person → prior company) edges as ``normname|priorco`` keys."""
    return {
        f"{normalize_name(p.name)}|{pr.company.strip().casefold()}"
        for p in extraction.people
        for pr in p.prior_roles
    }


def _career_prior_grounding(
    extraction: CareerHistoryExtraction, input_text: str
) -> float:
    """Fraction of prior-company-name tokens that appear in the input text.

    A dedicated no-fabrication proxy rather than the shared ``grounding_fraction``
    (which skips each fragment's FIRST word to duck sentence-initial capitals —
    that makes it BLIND to a short/leading company name, e.g. a fabricated single
    word "Honda" would falsely score 1.0). Here EVERY alphanumeric token of every
    prior-company name is checked (case-insensitive substring) against the input,
    so a fabricated employer absent from the bio actually drops the score. Returns
    1.0 when no prior company is asserted (an empty extraction fabricates nothing).
    """
    source = input_text.casefold()
    checked = 0
    found = 0
    for person in extraction.people:
        for pr in person.prior_roles:
            for token in re.findall(r"[a-z0-9&]+", pr.company.casefold()):
                if len(token) < 2:  # skip stray single chars ("&", initials)
                    continue
                checked += 1
                if token in source:
                    found += 1
    return found / checked if checked else 1.0


def score_career_history(cases: Sequence[CaseEvaluation]) -> PromptReport:
    """Score career_history recordings against ground truth.

    Gated metrics:
    - parse_rate — recordings surviving runtime schema validation.
    - empty_accuracy — the empty-not-fabricate gate: the model returns at least
      one pedigree exactly when the hand-checked expectation has one. Inventing a
      pedigree on a bio that names none, and missing one that's clearly stated,
      both cost this metric (the ~85%-are-empty reality makes it the key dial).
    - people_precision / people_recall — normalized founder-name sets (an
      off-roster advisor or a fabricated founder costs precision).
    - moves_precision / moves_recall — (person → prior company) edge sets, the
      core extraction quality.
    - grounding_mean / grounding_min — no-fabrication proxy: every prior-company
      name must appear (verbatim) in the input text.
    """
    issues: dict[str, list[str]] = {}
    parse = Accuracy()
    empty = Accuracy()
    people = SlotTally()
    moves = SlotTally()
    groundings: list[float] = []
    provenance: dict[str, int] = {}

    for case in cases:
        expected = case.expected
        assert isinstance(expected, CareerHistoryExtraction)
        parse.add(case.recorded is not None)
        if case.recorded is None:
            _issue(issues, case.case_id, "recorded response failed runtime schema validation")
            continue
        recorded = case.recorded
        assert isinstance(recorded, CareerHistoryExtraction)

        empty.add(bool(expected.people) == bool(recorded.people))
        if bool(expected.people) != bool(recorded.people):
            _issue(
                issues,
                case.case_id,
                "empty: expected "
                + ("a pedigree" if expected.people else "no pedigree")
                + ", got "
                + ("a pedigree" if recorded.people else "no pedigree"),
            )

        exp_people, got_people = _career_people(expected), _career_people(recorded)
        people.add_sets(exp_people, got_people)
        if exp_people != got_people:
            _issue(
                issues,
                case.case_id,
                f"people: missing {sorted(exp_people - got_people)},"
                f" extra {sorted(got_people - exp_people)}",
            )

        exp_edges, got_edges = _career_edges(expected), _career_edges(recorded)
        moves.add_sets(exp_edges, got_edges)
        if exp_edges != got_edges:
            _issue(
                issues,
                case.case_id,
                f"moves: missing {sorted(exp_edges - got_edges)},"
                f" extra {sorted(got_edges - exp_edges)}",
            )

        # Grounding: every prior-company name the model returned must appear in
        # the input (the verbatim-copy / no-fabrication rule). Uses a dedicated
        # per-token check, NOT grounding_fraction (whose first-word skip is blind
        # to short/leading company names — see _career_prior_grounding).
        grounding = _career_prior_grounding(recorded, case.input_text)
        groundings.append(grounding)
        if grounding < 1.0:
            _issue(
                issues,
                case.case_id,
                f"grounding: {grounding:.3f} — a prior company is absent from the input",
            )

    metrics: dict[str, float] = {
        "parse_rate": parse.value,
        "empty_accuracy": empty.value,
        "people_precision": people.precision,
        "people_recall": people.recall,
        "moves_precision": moves.precision,
        "moves_recall": moves.recall,
        "moves_f1": moves.f1,
        "grounding_mean": mean(groundings),
        "grounding_min": min(groundings) if groundings else 1.0,
    }
    return PromptReport(
        prompt="career_history",
        case_count=len(cases),
        provenance_counts=provenance,
        metrics=metrics,
        gated=[
            "parse_rate",
            "empty_accuracy",
            "people_precision",
            "people_recall",
            "moves_precision",
            "moves_recall",
            "grounding_mean",
            "grounding_min",
        ],
        issues=issues,
    )


# ---------------------------------------------------------------------------
# source_verification (discriminative fact-vs-source verification)
# ---------------------------------------------------------------------------


def _build_source_verification_prompt(case: CaseSpec, input_text: str) -> str:
    # Mirror verify-sources: truncate the source to the shared 32k budget; the
    # claim to check rides on the case spec.
    source = truncate_to_chars(input_text, MAX_PROMPT_INPUT_CHARS)
    return build_source_verification_prompt(claim=case.claim, source_text=source)


def score_source_verification(cases: Sequence[CaseEvaluation]) -> PromptReport:
    """Score source_verification recordings against ground truth.

    Gated metrics:
    - parse_rate — recordings surviving runtime schema validation.
    - verdict_accuracy — exact match of the supported/unsupported/uncertain
      verdict (the key dial). The schema's quote-discipline validator has already
      run on the recorded response, so a quote-less 'supported' has become
      'uncertain' before scoring.
    - grounding_mean / grounding_min — the no-fabrication proxy: every
      'supported' verdict's quote MUST be a verbatim substring of the source. A
      'supported' with a non-grounded quote scores 0, so grounding_min → 0 the
      instant one fabrication slips through (the gate's sharpest tooth).
    """
    issues: dict[str, list[str]] = {}
    parse = Accuracy()
    verdict = Accuracy()
    groundings: list[float] = []
    provenance: dict[str, int] = {}

    for case in cases:
        expected = case.expected
        assert isinstance(expected, SourceVerification)
        parse.add(case.recorded is not None)
        if case.recorded is None:
            _issue(issues, case.case_id, "recorded response failed runtime schema validation")
            continue
        recorded = case.recorded
        assert isinstance(recorded, SourceVerification)

        verdict.add(recorded.verdict == expected.verdict)
        if recorded.verdict != expected.verdict:
            _issue(
                issues,
                case.case_id,
                f"verdict: expected {expected.verdict!r}, got {recorded.verdict!r}",
            )

        # No-fabrication: a 'supported' verdict must quote the source verbatim.
        if recorded.verdict == "supported":
            grounded = quote_is_grounded(recorded.supporting_quote, case.input_text)
            groundings.append(1.0 if grounded else 0.0)
            if not grounded:
                _issue(
                    issues,
                    case.case_id,
                    "grounding: 'supported' quote is not a verbatim source substring",
                )
        else:
            groundings.append(1.0)  # nothing asserted → nothing to fabricate

    metrics: dict[str, float] = {
        "parse_rate": parse.value,
        "verdict_accuracy": verdict.value,
        "grounding_mean": mean(groundings),
        "grounding_min": min(groundings) if groundings else 1.0,
    }
    return PromptReport(
        prompt="source_verification",
        case_count=len(cases),
        provenance_counts=provenance,
        metrics=metrics,
        gated=["parse_rate", "verdict_accuracy", "grounding_mean", "grounding_min"],
        issues=issues,
    )


# ---------------------------------------------------------------------------
# describe_fallback (third-party-grounded description for the unscrapable residue)
# ---------------------------------------------------------------------------


def _build_describe_fallback_prompt(case: CaseSpec, input_text: str) -> str:
    # Mirror the describe-fallback stage: the caller-assembled evidence block
    # (Wikidata facts + corroborated article title/excerpts) IS the input, and
    # it is truncated to the prompt's evidence budget before building.
    evidence = truncate_to_chars(input_text, DESCRIBE_FALLBACK_MAX_EVIDENCE_CHARS)
    return build_describe_fallback_prompt(
        company_name=case.company_name, evidence=evidence
    )


def score_describe_fallback(cases: Sequence[CaseEvaluation]) -> PromptReport:
    """Score describe_fallback recordings against ground truth.

    This is the site's only GENERATIVE-from-third-party path, so the gate is the
    no-fabrication descriptor check, mirroring source_verification's grounding_min.

    Gated metrics:
    - parse_rate — recordings surviving runtime schema validation (incl. the
      model validator that nulls a description missing its grounding descriptor).
    - descriptor_grounding_min — the moat gate: every PRODUCED description's
      echoed grounding_descriptor MUST appear in the evidence (the same
      ``_descriptor_in_evidence`` check the stage applies). One ungrounded echo
      drives this to 0, so the floor is 1.0 — the no-fabrication tooth.
    - null_accuracy — over expected-null cases, the rate the model also returned
      null (funding-only / ambiguous / thin evidence → null is the correct
      answer; describing anyway is a fabrication).
    - described_accuracy — over expected-described cases, the rate the model also
      produced a description (missing a genuinely groundable description is a
      recall miss).

    Informational: descriptor_grounding_mean (the average, alongside the gated min).
    """
    issues: dict[str, list[str]] = {}
    parse = Accuracy()
    null_acc = Accuracy()
    described_acc = Accuracy()
    groundings: list[float] = []
    provenance: dict[str, int] = {}

    for case in cases:
        expected = case.expected
        assert isinstance(expected, DescribeFallbackResult)
        parse.add(case.recorded is not None)
        if case.recorded is None:
            _issue(issues, case.case_id, "recorded response failed runtime schema validation")
            continue
        recorded = case.recorded
        assert isinstance(recorded, DescribeFallbackResult)

        exp_described = expected.description_short is not None
        rec_described = recorded.description_short is not None

        if exp_described:
            described_acc.add(rec_described)
            if not rec_described:
                _issue(
                    issues,
                    case.case_id,
                    "described: expected a grounded description, got null",
                )
        else:
            null_acc.add(not rec_described)
            if rec_described:
                _issue(
                    issues,
                    case.case_id,
                    "null: expected null (funding-only/ambiguous/thin), got a description",
                )

        # No-fabrication: a PRODUCED description's echoed descriptor must be a
        # (normalized) substring of the evidence — the same runtime moat check.
        if rec_described:
            grounded = _descriptor_in_evidence(recorded.grounding_descriptor, case.input_text)
            groundings.append(1.0 if grounded else 0.0)
            if not grounded:
                _issue(
                    issues,
                    case.case_id,
                    "grounding: descriptor is not present in the evidence",
                )
        else:
            groundings.append(1.0)  # nothing asserted → nothing to fabricate

    metrics: dict[str, float] = {
        "parse_rate": parse.value,
        "null_accuracy": null_acc.value,
        "described_accuracy": described_acc.value,
        "descriptor_grounding_min": min(groundings) if groundings else 1.0,
        # Informational (not gated): the average alongside the gated min.
        "descriptor_grounding_mean": mean(groundings),
    }
    return PromptReport(
        prompt="describe_fallback",
        case_count=len(cases),
        provenance_counts=provenance,
        metrics=metrics,
        gated=[
            "parse_rate",
            "null_accuracy",
            "described_accuracy",
            "descriptor_grounding_min",
        ],
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PROMPT_SPECS: tuple[PromptSpec, ...] = (
    PromptSpec(
        name="company_description",
        schema=CompanyDescription,
        build_prompt=_build_company_description_prompt,
        score=score_company_description,
    ),
    PromptSpec(
        name="company_description_long",
        schema=CompanyLongDescription,
        build_prompt=_build_company_description_long_prompt,
        score=score_company_description_long,
    ),
    PromptSpec(
        name="funding_extraction",
        schema=FundingExtraction,
        build_prompt=_build_funding_prompt,
        score=score_funding_extraction,
    ),
    PromptSpec(
        name="career_history",
        schema=CareerHistoryExtraction,
        build_prompt=_build_career_history_prompt,
        score=score_career_history,
    ),
    PromptSpec(
        name="source_verification",
        schema=SourceVerification,
        build_prompt=_build_source_verification_prompt,
        score=score_source_verification,
    ),
    PromptSpec(
        name="describe_fallback",
        schema=DescribeFallbackResult,
        build_prompt=_build_describe_fallback_prompt,
        score=score_describe_fallback,
    ),
)


def get_spec(name: str) -> PromptSpec:
    for spec in PROMPT_SPECS:
        if spec.name == name:
            return spec
    known = ", ".join(s.name for s in PROMPT_SPECS)
    raise KeyError(f"Unknown prompt {name!r}; known prompts: {known}")
