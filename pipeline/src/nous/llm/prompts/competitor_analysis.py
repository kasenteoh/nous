"""Competitor-analysis prompt per spec §6.3 (M4).

Input: a target company (name + descriptions + industry_group) and a peer list
of up to 50 same-industry companies (name + short description). Output: a
Pydantic model holding up to 6 ranked competitors with descriptions and
reasoning.

Per CLAUDE.md ("prompts must instruct the model to return null or empty rather
than fabricate"), the template tells Gemini to return an empty list rather
than invent competitors.

This module is a drop-in user of `nous.llm.client.complete_json`. The caller
(analyze-competitors stage) imports `build_prompt` and `CompetitorAnalysis`
and hands them to `complete_json`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

MAX_PEERS = 50
MAX_COMPETITORS = 6


class Target(BaseModel):
    name: str
    description_short: str
    description_long: str
    industry_group: str


class Peer(BaseModel):
    name: str
    description_short: str


class Competitor(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    reasoning: str = Field(..., min_length=1)
    rank: int = Field(..., ge=1, le=MAX_COMPETITORS)


class CompetitorAnalysis(BaseModel):
    competitors: list[Competitor] = Field(
        default_factory=list, max_length=MAX_COMPETITORS
    )

    @model_validator(mode="after")
    def _ranks_must_be_one_through_n(self) -> CompetitorAnalysis:
        ranks = [c.rank for c in self.competitors]
        expected = list(range(1, len(ranks) + 1))
        if ranks != expected:
            raise ValueError(
                f"ranks must be 1..N with no gaps or duplicates; got {ranks}"
            )
        return self


PROMPT_TEMPLATE = """\
You are identifying competitors for a software company.

Target company:
- Name: {name}
- Industry: {industry_group}
- Short description: {description_short}
- Long description:
{description_long}

Peer list (other companies indexed in our database in the same industry):
{peer_block}

Task:
- Identify up to {max_competitors} companies that compete with the target.
- Prefer companies from the peer list when reasonable.
- You may also name well-known competitors that are not in the peer list.
- Do not invent fictional companies. If you have no high-confidence competitors,
  return an empty list rather than fabricate.
- Rank them 1..N, where 1 is the most direct competitor. Ranks must be
  consecutive integers starting at 1 with no gaps or duplicates.
- For each competitor, write a 1–2 sentence description and a short
  reasoning explaining why they compete with the target.

Return JSON matching the schema.
"""


def _render_peer_block(peers: list[Peer]) -> str:
    if not peers:
        return "(no peers available in our database)"
    lines = [f"- {p.name}: {p.description_short}" for p in peers]
    return "\n".join(lines)


def build_prompt(*, target: Target, peers: list[Peer]) -> str:
    """Render the competitor-analysis prompt with the given target and peer list.

    The peer list is truncated to MAX_PEERS to keep token cost predictable.
    """
    capped_peers = peers[:MAX_PEERS]
    return PROMPT_TEMPLATE.format(
        name=target.name,
        industry_group=target.industry_group,
        description_short=target.description_short,
        description_long=target.description_long,
        peer_block=_render_peer_block(capped_peers),
        max_competitors=MAX_COMPETITORS,
    )
