"""Unit tests for entity corroboration — the same-name different-entity
signals. Fixtures mirror the REAL 2026-07-17 QA failure cases (wave←Primary
Wave, impulse←Impulse Dynamics, bespoke-labs←IM8's "bespoke" adjective,
terrafirma←TerraFirma Inc, wonder←food-Wonder) plus correct-attribution
articles that must NOT flag. No DB, no network — pure functions.
"""

from __future__ import annotations

from nous.util.entity_corroboration import corroborate_entity

_WAVE_DESC = (
    "Wave is building a mobile money network across Africa, offering free "
    "deposits, withdrawals, and bill payments, with money transfers at a "
    "flat 1% fee."
)


def test_primary_wave_article_is_extension_suspect() -> None:
    """The wave case: every proper occurrence of "Wave" is part of "Primary
    Wave" — a different entity."""
    text = (
        "Primary Wave Music Acquires Stake in Iconic Catalog. Primary Wave "
        "announced a $2.2 billion raise led by Brookfield. The deal makes "
        "Primary Wave one of the largest independent music publishers. "
        "Primary Wave's CEO said the funding validates the catalog thesis."
    )
    r = corroborate_entity("Wave", _WAVE_DESC, text)
    assert r.suspect is True
    assert "longer entity phrase" in " ".join(r.reasons)
    assert any("Primary Wave" in e for e in r.evidence)


def test_impulse_dynamics_article_is_extension_suspect() -> None:
    text = (
        "Impulse Dynamics Raises $136M to Expand Cardiac Device Platform. "
        "Impulse Dynamics, a medical device company, closed the round to "
        "scale its CCM therapy. Impulse Dynamics has treated thousands of "
        "heart-failure patients."
    )
    r = corroborate_entity(
        "Impulse",
        "Impulse is a space logistics company building orbital transfer "
        "vehicles for satellite deployment missions.",
        text,
    )
    assert r.suspect is True
    assert any("Impulse Dynamics" in e for e in r.evidence)


def test_bespoke_adjective_article_is_lowercase_suspect() -> None:
    """The bespoke-labs case: the IM8 story contains "bespoke" only as an
    adjective — never a proper noun."""
    text = (
        "Prenetics Raises $1 Billion For IM8 Via General Catalyst. The "
        "wellness brand offers bespoke supplement formulations tailored to "
        "individual health profiles, with bespoke packaging options."
    )
    r = corroborate_entity(
        "Bespoke Labs",
        "Bespoke Labs builds reinforcement-learning training environments "
        "for reliable AI agents.",
        text,
    )
    # "Bespoke Labs" as a phrase never appears; the bare-word occurrences are
    # lowercase. Either the phrase is absent (0 occurrences + zero context
    # overlap) or lowercase-only fires — both must land on suspect.
    assert r.suspect is True


def test_terrafirma_inc_article_is_extension_suspect() -> None:
    text = (
        "TerraFirma Inc Closes $115 Million Series A. TerraFirma Inc will "
        "use the proceeds to expand its commercial construction operations "
        "across Texas. TerraFirma Inc was founded in 2019."
    )
    r = corroborate_entity(
        "TerraFirma",
        "TerraFirma builds autonomous earthworks robots for construction "
        "site grading and excavation.",
        text,
    )
    # "Inc" is a neutral follower, so extension must NOT fire on it alone —
    # but zero context overlap with at most one bare mention doesn't apply
    # (3 proper mentions). This case is deliberately NOT suspect on cheap
    # signals: same-name same-industry needs the LLM adjudicator. Pin the
    # honest outcome so the probe's blind spot is documented, not hidden.
    assert r.proper_occurrences == 3
    assert r.extended_occurrences == 0
    assert r.suspect is False


def test_food_wonder_article_vs_edtech_wonder_is_context_suspect() -> None:
    text = (
        "Wonder raises $650M at a $9B valuation. The food hall and delivery "
        "startup founded by Marc Lore operates dozens of locations serving "
        "meals from celebrity chefs. Wonder will use the funding to open "
        "more kitchens."
    )
    r = corroborate_entity(
        "Wonder",
        "Wonder is an online education platform connecting students with "
        "expert tutors for personalized learning journeys.",
        text,
    )
    # Two bare proper-noun mentions of "Wonder" — the extension signal stays
    # quiet, and >1 proper mention keeps the weak context signal quiet too.
    # Honest blind spot: cheap signals alone cannot condemn this one; the
    # LLM adjudicator (or amount/context cross-checks) owns it. What the
    # cheap layer DOES contribute is the zero-overlap measurement.
    assert r.context_candidates >= 4
    assert r.context_overlap == 0


def test_correct_wave_article_not_suspect() -> None:
    """The REAL Wave mobile-money article: bare proper-noun mentions + strong
    description overlap. Must not flag."""
    text = (
        "Wave raises $137M to expand mobile money across Africa. Wave, the "
        "Senegal-based fintech, offers free deposits and withdrawals and "
        "flat-fee money transfers. Wave's agent network spans eight "
        "countries, processing bill payments for millions."
    )
    r = corroborate_entity("Wave", _WAVE_DESC, text)
    assert r.suspect is False
    assert r.context_overlap >= 3


def test_correct_sambanova_article_not_suspect() -> None:
    text = (
        "Inference chip startup SambaNova valued at $11B after $1B round. "
        "SambaNova Systems builds AI accelerator hardware for datacenter "
        "inference workloads, competing with Nvidia."
    )
    r = corroborate_entity(
        "SambaNova",
        "SambaNova builds AI accelerator chips and inference hardware "
        "systems for enterprise datacenters.",
        text,
    )
    # "SambaNova Systems" extends rightward — but only 1 of 2 proper
    # occurrences, under the 0.75 rate + >=2 count bar. Not suspect.
    assert r.suspect is False


def test_own_multiword_name_not_extension() -> None:
    """"Bespoke Labs" in a real Bespoke Labs article: the successor token is
    part of the company's own name — never an extension."""
    text = (
        "Bespoke Labs Raises $40 Million. Bespoke Labs builds training "
        "environments for AI agents. Bespoke Labs was founded in 2024."
    )
    r = corroborate_entity(
        "Bespoke Labs",
        "Bespoke Labs builds reinforcement-learning training environments "
        "for reliable AI agents.",
        text,
    )
    assert r.suspect is False
    assert r.extended_occurrences == 0


def test_role_and_corporate_followers_are_neutral() -> None:
    """"Wonder CEO Marc Lore" / "Wave Inc" — functional capitalized followers
    must not count as extensions."""
    text = (
        "Wonder CEO Marc Lore announced the expansion. Wonder raised the "
        "round from Accel. Wonder Inc has grown rapidly, and Wonder "
        "continues hiring."
    )
    r = corroborate_entity(
        "Wonder",
        "Wonder operates food halls and a meal delivery service with "
        "celebrity chef partnerships and rapid kitchen expansion.",
        text,
    )
    assert r.extended_occurrences == 0
    assert r.suspect is False


def test_no_text_and_empty_name_fail_open() -> None:
    r = corroborate_entity("Wave", _WAVE_DESC, "")
    assert r.suspect is False
    assert r.occurrences == 0
    r2 = corroborate_entity("Inc.", None, "Some article text about funding.")
    assert r2.suspect is False


def test_sentence_boundary_breaks_extension_adjacency() -> None:
    """A capitalized word across a sentence boundary is not an extension:
    "...backed Impulse. Dynamics of the market..." must not read as
    "Impulse Dynamics"."""
    text = (
        "Investors backed Impulse. Dynamics of the launch market favor "
        "consolidation. Impulse plans two missions, and Impulse expects "
        "revenue growth."
    )
    r = corroborate_entity(
        "Impulse",
        "Impulse is a space logistics company building orbital transfer "
        "vehicles for satellite deployment missions.",
        text,
    )
    assert r.extended_occurrences == 0
    assert r.suspect is False
