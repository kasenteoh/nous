"""Unit tests for the M4 competitor-analysis prompt module.

Pure unit tests — no DB, no LLM call. Validates the Pydantic schema and the
prompt builder's structural contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nous.llm.prompts.competitor_analysis import (
    MAX_PEERS,
    Competitor,
    CompetitorAnalysis,
    Peer,
    Target,
    build_prompt,
)

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_empty_competitors_list_is_valid() -> None:
    assert CompetitorAnalysis(competitors=[]).competitors == []


def test_single_competitor_with_rank_1_is_valid() -> None:
    ca = CompetitorAnalysis(
        competitors=[
            Competitor(
                name="Beta",
                description="A rival.",
                reasoning="Same market.",
                rank=1,
            )
        ]
    )
    assert len(ca.competitors) == 1


def test_six_competitors_with_consecutive_ranks_is_valid() -> None:
    ca = CompetitorAnalysis(
        competitors=[
            Competitor(name=f"C{i}", description="d", reasoning="r", rank=i)
            for i in range(1, 7)
        ]
    )
    assert [c.rank for c in ca.competitors] == [1, 2, 3, 4, 5, 6]


def test_more_than_six_competitors_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name=f"C{i}", description="d", reasoning="r", rank=i)
                for i in range(1, 8)
            ]
        )


def test_duplicate_ranks_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name="A", description="d", reasoning="r", rank=1),
                Competitor(name="B", description="d", reasoning="r", rank=1),
            ]
        )


def test_gap_in_ranks_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name="A", description="d", reasoning="r", rank=1),
                Competitor(name="B", description="d", reasoning="r", rank=3),
            ]
        )


def test_rank_starting_above_one_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name="A", description="d", reasoning="r", rank=2),
            ]
        )


def test_empty_name_rejected() -> None:
    with pytest.raises(ValidationError):
        Competitor(name="", description="d", reasoning="r", rank=1)


def test_rank_above_six_rejected() -> None:
    with pytest.raises(ValidationError):
        Competitor(name="A", description="d", reasoning="r", rank=7)


def test_rank_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        Competitor(name="A", description="d", reasoning="r", rank=0)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _target() -> Target:
    return Target(
        name="Acme",
        description_short="Acme makes widgets.",
        description_long="Acme is a B2B SaaS for widget logistics.",
        industry_group="SaaS",
    )


def _peer(i: int) -> Peer:
    return Peer(name=f"Peer{i}", description_short=f"Does thing {i}.")


def test_build_prompt_includes_target_fields() -> None:
    prompt = build_prompt(target=_target(), peers=[])
    assert "Acme" in prompt
    assert "Acme makes widgets." in prompt
    assert "Acme is a B2B SaaS for widget logistics." in prompt
    assert "SaaS" in prompt


def test_build_prompt_includes_each_peer() -> None:
    peers = [_peer(i) for i in range(3)]
    prompt = build_prompt(target=_target(), peers=peers)
    for i in range(3):
        assert f"Peer{i}" in prompt
        assert f"Does thing {i}." in prompt


def test_build_prompt_empty_peer_list_renders_without_error() -> None:
    prompt = build_prompt(target=_target(), peers=[])
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_build_prompt_caps_peers_at_max() -> None:
    too_many = [_peer(i) for i in range(MAX_PEERS + 20)]
    prompt = build_prompt(target=_target(), peers=too_many)
    assert f"Peer{MAX_PEERS - 1}" in prompt
    assert f"Peer{MAX_PEERS}" not in prompt


def test_build_prompt_forbids_fabrication_language() -> None:
    """Per spec §11 / CLAUDE.md: prompts must instruct null/empty over fabrication."""
    prompt = build_prompt(target=_target(), peers=[])
    lowered = prompt.lower()
    assert "do not invent" in lowered or "do not fabricate" in lowered
    assert "empty list" in lowered or "return an empty" in lowered


def test_out_of_order_ranks_are_normalized() -> None:
    """Gemini may return competitors out of rank order in the JSON array.
    The validator must sort them before checking, not reject them."""
    ca = CompetitorAnalysis(
        competitors=[
            Competitor(name="B", description="d", reasoning="r", rank=2),
            Competitor(name="A", description="d", reasoning="r", rank=1),
            Competitor(name="C", description="d", reasoning="r", rank=3),
        ]
    )
    assert [c.rank for c in ca.competitors] == [1, 2, 3]
    assert [c.name for c in ca.competitors] == ["A", "B", "C"]
