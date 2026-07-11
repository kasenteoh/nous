"""Unit tests for the golden-set grounding proxy (nous.evals.scoring).

Pinned by the first live DeepSeek re-recording (2026-07-11), which surfaced
one proxy artifact worth fixing and one real fabrication worth keeping:

- "JetBrains IDEs" against an input that says "JetBrains extensions" is
  legitimate analyst paraphrase — "IDE"/"IDEs" is a generic initialism of the
  same class as "API"/"SDK" and must not count as fabrication evidence.
- Naming a competitor the input never mentions ("GitHub Gists" for a snippet
  manager whose site names no alternatives) IS fabrication and must keep
  costing grounding, so the floor stays meaningful.
"""

from __future__ import annotations

from nous.evals.scoring import grounding_fraction

_INPUT = (
    "Snipvault saves code snippets for teams.\n"
    "Extensions for VS Code and JetBrains. Free for personal use.\n"
    "Teams: $5 per user per month."
)


def test_initialism_paraphrase_is_not_fabrication() -> None:
    """'JetBrains IDEs' grounds against 'JetBrains extensions' — the IDE
    initialism is stopworded, JetBrains itself is still checked (and found)."""
    text = "The product ships integrations for VS Code and JetBrains IDEs."
    assert grounding_fraction(text, _INPUT) == 1.0


def test_invented_competitor_still_fails_grounding() -> None:
    """A competitor absent from the input keeps costing grounding."""
    text = "It competes with general-purpose managers like GitHub Gists."
    assert grounding_fraction(text, _INPUT) < 1.0


def test_invented_number_still_fails_grounding() -> None:
    """Numeric color the input never states ('3 a.m.') keeps costing."""
    grounded = "Teams pay $5 per user per month for Snipvault."
    invented = "Teams pay $5 per user per month, debugging at 3 a.m. with Snipvault."
    assert grounding_fraction(grounded, _INPUT) == 1.0
    assert grounding_fraction(invented, _INPUT) < 1.0


def test_named_entities_from_input_ground() -> None:
    """Ordinary case: entities and figures present in the input pass."""
    text = (
        "Snipvault sells snippet management to teams, with VS Code and"
        " JetBrains extensions, free personal use, and a $5 per user per"
        " month team plan."
    )
    assert grounding_fraction(text, _INPUT) == 1.0
