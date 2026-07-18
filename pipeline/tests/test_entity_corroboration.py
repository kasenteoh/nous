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


def test_food_wonder_vs_edtech_wonder_is_a_pinned_blind_spot() -> None:
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
    assert r.suspect is False
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


def test_stylized_lowercase_name_is_proper_noun() -> None:
    """First prod run regression: "xAI" carries an uppercase letter — it is a
    proper-noun occurrence, never "lowercase-only"."""
    text = (
        "Elon Musk's xAI closes a $20 billion funding round. xAI will use "
        "the capital for AI infrastructure, and xAI expects rapid growth."
    )
    r = corroborate_entity(
        "xAI",
        "xAI develops large language models and AI infrastructure for "
        "scientific discovery applications.",
        text,
    )
    assert r.lowercase_only is False
    assert r.suspect is False


def test_outlet_suffix_dash_breaks_adjacency() -> None:
    """First prod run regression: the GN "Title - Outlet" convention.
    "…at $380B Valuation - Anthropic Daily" must not read "Valuation
    Anthropic" as an entity, in either direction."""
    text = (
        "Anthropic hits $965B valuation with $65B funding - MSN. "
        "Anthropic raises the largest round on record - Yahoo Finance. "
        "Anthropic closes $65B - Reuters."
    )
    r = corroborate_entity(
        "Anthropic",
        "Anthropic is an AI safety company building reliable language "
        "models and research assistants.",
        text,
    )
    assert r.extended_occurrences == 0
    assert r.suspect is False


def test_outlet_containing_company_name_still_flags() -> None:
    """The genuine catch the calibration must NOT lose: company "Built"
    carrying a round whose every headline ends "- Built In" (the outlet).
    The outlet phrase follows the dash, so the leading word is fine — but
    "Built In" itself repeats as a proper phrase the company doesn't own."""
    text = (
        "Anthropic Bags $30B Funding Round at $380B Valuation - Built In. "
        "Anthropic Hits Record Valuation In Mega Round - Built In. "
        "Anthropic Funding Round Draws Investor Interest - Built In."
    )
    r = corroborate_entity(
        "Built",
        "Built provides construction finance software for lenders "
        "managing draw schedules and inspections.",
        text,
    )
    assert r.suspect is True
    assert any("Built In" in e for e in r.evidence)


def test_possessive_and_descriptor_prefixes_are_neutral() -> None:
    """Second prod triage: "India's Zepto" / "Fusion Startup Helion" —
    possessives attribute, category nouns describe; neither names a
    different entity."""
    text = (
        "India's Zepto Raises $500M For Quick Commerce. Zepto operates "
        "dark stores across India, and Zepto delivers groceries in "
        "minutes. Startup Zepto Also Expands Into Electronics."
    )
    r = corroborate_entity(
        "Zepto",
        "Zepto runs a quick-commerce grocery delivery network of dark "
        "stores across Indian cities delivering within minutes.",
        text,
    )
    assert r.extended_occurrences == 0
    assert r.suspect is False


def test_own_name_ai_suffix_is_neutral() -> None:
    """"Cognition AI" is the company named Cognition, informally suffixed —
    not another entity."""
    text = (
        "Cognition AI Closes $1B Round. Cognition AI makes the Devin "
        "coding agent, and Cognition AI plans enterprise expansion."
    )
    r = corroborate_entity(
        "Cognition",
        "Cognition develops Devin, an autonomous software engineering "
        "agent for coding tasks and pull requests.",
        text,
    )
    assert r.extended_occurrences == 0
    assert r.suspect is False


def test_head_token_variant_keeps_original_own_tokens() -> None:
    """Second prod triage bug: evaluating the head-token variant "Yuga" must
    not read "Yuga Labs" (the company's own name) as another entity —
    own_tokens carries the original full name."""
    text = (
        "Yuga Labs Raises $450M. Yuga Labs created Bored Ape Yacht Club, "
        "and Yuga Labs plans a metaverse launch."
    )
    r = corroborate_entity(
        "Yuga",
        "Yuga Labs creates NFT collections including Bored Ape Yacht Club "
        "and builds metaverse experiences.",
        text,
        own_tokens={"yuga", "labs", "bayc"},
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
