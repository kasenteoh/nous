"""Unit tests for the career-history extraction prompt schema + validators.

Pure (no DB, no LLM): they exercise the empty-not-fabricate hardening — the
Pydantic validators that clean, de-duplicate, and drop pedigree-less people so a
bad LLM response can't smuggle noise past the schema.
"""

from __future__ import annotations

from nous.llm.prompts.career_history import (
    PROMPT_VERSION,
    CareerHistoryExtraction,
    PersonCareer,
    PriorRole,
    build_prompt,
)


def test_prior_role_trims_and_nulls_blank_role() -> None:
    pr = PriorRole(company="  Stripe  ", role="   ")
    assert pr.company == "Stripe"
    assert pr.role is None


def test_prior_role_drops_implausible_years() -> None:
    pr = PriorRole(company="Google", start_year=12, end_year=2015)
    assert pr.start_year is None  # 12 is not a plausible calendar year
    assert pr.end_year == 2015


def test_person_career_drops_empty_company_role() -> None:
    person = PersonCareer(
        name="Jane Doe",
        prior_roles=[
            PriorRole(company="   "),  # cleans to empty → dropped
            PriorRole(company="Meta", role="Engineer"),
        ],
    )
    assert [pr.company for pr in person.prior_roles] == ["Meta"]


def test_person_career_dedupes_roles_case_insensitively() -> None:
    person = PersonCareer(
        name="Jane Doe",
        prior_roles=[
            PriorRole(company="Google", role="Engineer"),
            PriorRole(company="google", role="engineer"),  # dup
            PriorRole(company="Google", role="Director"),  # distinct role
        ],
    )
    assert len(person.prior_roles) == 2


def test_extraction_drops_pedigreeless_and_nameless_people() -> None:
    extraction = CareerHistoryExtraction(
        people=[
            PersonCareer(name="Has Pedigree", prior_roles=[PriorRole(company="AWS")]),
            PersonCareer(name="No Pedigree", prior_roles=[]),  # dropped
            PersonCareer(name="   ", prior_roles=[PriorRole(company="IBM")]),  # dropped
        ]
    )
    assert [p.name for p in extraction.people] == ["Has Pedigree"]


def test_empty_extraction_is_the_default() -> None:
    # The dominant-correct output for the ~85% with no named pedigree.
    assert CareerHistoryExtraction().people == []


def test_null_company_role_dropped_not_fatal() -> None:
    # complete_json's system prompt globally allows null for undetermined fields,
    # so a single `company: null` must drop ONLY that role — not fail-and-discard
    # the whole company's extraction. The sibling valid role must survive.
    extraction = CareerHistoryExtraction.model_validate_json(
        '{"people":[{"name":"Jane Doe","prior_roles":['
        '{"company":null,"role":"Engineer"},{"company":"Stripe"}]}]}'
    )
    assert len(extraction.people) == 1
    assert [pr.company for pr in extraction.people[0].prior_roles] == ["Stripe"]


def test_person_with_only_null_company_roles_is_dropped() -> None:
    extraction = CareerHistoryExtraction.model_validate_json(
        '{"people":[{"name":"Jane Doe","prior_roles":[{"company":null}]}]}'
    )
    assert extraction.people == []


def test_build_prompt_includes_company_roster_and_text() -> None:
    prompt = build_prompt(
        company_name="Acme",
        roster=[("Jane Doe", "CEO"), ("John Roe", "CTO")],
        cleaned_text="Jane was previously at Stripe.",
    )
    assert "Acme" in prompt
    assert "- Jane Doe — CEO" in prompt
    assert "- John Roe — CTO" in prompt
    assert "previously at Stripe" in prompt
    # The anti-fabrication instruction must survive in the rendered prompt.
    assert "empty" in prompt.lower()


def test_build_prompt_handles_empty_roster() -> None:
    prompt = build_prompt(company_name="Acme", roster=[], cleaned_text="text")
    assert "(none provided)" in prompt


def test_prompt_version_scheme() -> None:
    # "YYYY-MM-DD.N" — three dash-separated date parts plus a same-day counter.
    date_part, counter = PROMPT_VERSION.split(".")
    assert len(date_part.split("-")) == 3
    assert counter.isdigit()
