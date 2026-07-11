"""Competitor-analysis prompt per spec §6.3 (M4).

Input: a target company (name + descriptions + industry_group) and a peer list
of up to 50 same-industry companies (name + short description). Output: a
Pydantic model holding up to 6 ranked competitors with descriptions and
reasoning.

Per CLAUDE.md ("prompts must instruct the model to return null or empty rather
than fabricate"), the template tells the model to return an empty list rather
than invent competitors.

This module is a drop-in user of `nous.llm.client.complete_json`. The caller
(analyze-competitors stage) imports `build_prompt` and `CompetitorAnalysis`
and hands them to `complete_json`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, model_validator

# Version stamped onto rows whose content this prompt produced (competitors
# rows via analyze-competitors). Scheme: "<date>.<same-day-counter>". Bump on
# ANY semantic change to the template, schema, or validators — even a wording
# tweak — so data from a bad revision can be found and re-run.
PROMPT_VERSION: str = "2026-07-10.1"

# Meta-commentary the model sometimes leaks into a competitor's reasoning /
# description instead of just excluding the entry (e.g. "Included temporarily
# for evaluation but should be dropped."). Such text must never reach a page, so
# the entry is dropped entirely. Mirrors the web display guard in
# web/components/Competitors.tsx.
_META_LEAK = re.compile(
    r"should be dropped|for evaluation|temporar|placeholder|"
    r"do not (include|display|show)|not a (real )?competitor",
    re.IGNORECASE,
)

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
    # The LLM's rank is only an ordering hint; CompetitorAnalysis renumbers it to
    # a contiguous 1..N. Unbounded + optional (defaults to sort-last) so a single
    # gapped / offset / omitted rank never sinks the whole analysis — DeepSeek
    # routinely returns ranks like [2, 3, 4, 5, 6] or [6].
    rank: int = MAX_COMPETITORS + 1


class CompetitorAnalysis(BaseModel):
    competitors: list[Competitor] = Field(
        default_factory=list, max_length=MAX_COMPETITORS
    )

    @model_validator(mode="after")
    def _drop_meta_leak(self) -> CompetitorAnalysis:
        """Drop competitors whose reasoning or description leaks the model's own
        selection commentary ("...should be dropped", "for evaluation", ...).
        The model is instructed to EXCLUDE uncertain entries rather than annotate
        them; this is the backstop for when it does so anyway. Defined before
        _renumber_ranks so ranks stay contiguous over the kept set."""
        self.competitors = [
            c
            for c in self.competitors
            if not _META_LEAK.search(c.reasoning)
            and not _META_LEAK.search(c.description)
        ]
        return self

    @model_validator(mode="after")
    def _renumber_ranks(self) -> CompetitorAnalysis:
        # Renumber to a contiguous 1..N by the model's rank order (stable sort,
        # so ties keep input order). The stage writes these to competitors.rank,
        # whose UNIQUE(company_id, rank) requires them distinct — renumbering
        # guarantees that for ANY LLM output.
        #
        # This REPLACES a strict validator that *rejected* gapped/offset ranks.
        # Because DeepSeek almost never returns a clean 1..N (it offsets the
        # start or leaves gaps), that rejection failed validation on nearly every
        # response — even after the client's one retry — so analyze-competitors
        # silently dropped competitors for essentially every company and the
        # competitors table sat empty. Renumbering preserves the model's ordering
        # while making any non-empty response writable.
        self.competitors.sort(key=lambda c: c.rank)
        for new_rank, competitor in enumerate(self.competitors, start=1):
            competitor.rank = new_rank
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

Competitors named in the target's TechCrunch coverage (candidates to validate):
{candidate_block}

Task:
- Identify up to {max_competitors} companies that compete with the target.
- First, REVALIDATE the TechCrunch candidates above: include a candidate only if
  it is genuinely a competitor of the target. Drop any that are not.
- Then add other competitors you are confident about (from the peer list or
  well-known companies). Rank the full combined set together.
- Do not invent fictional companies. If you have no high-confidence competitors,
  return an empty list rather than fabricate.
- Rank them 1..N, where 1 is the most direct competitor. Ranks must be
  consecutive integers starting at 1 with no gaps or duplicates.
- For each competitor, write a 1–2 sentence factual description and a short
  reasoning about why they compete with the target. The reasoning must be about
  the competition itself — NEVER meta-commentary about your own selection
  process (do not write things like "included for evaluation" or "should be
  dropped"). If you are not confident an entry belongs, EXCLUDE it rather than
  add it with a caveat.

Return JSON matching the schema.
"""


def _render_peer_block(peers: list[Peer]) -> str:
    if not peers:
        return "(no peers available in our database)"
    lines = [f"- {p.name}: {p.description_short}" for p in peers]
    return "\n".join(lines)


def _render_candidate_block(candidates: list[str]) -> str:
    if not candidates:
        return "(no TechCrunch coverage names any competitors)"
    return "\n".join(f"- {name}" for name in candidates)


def build_prompt(
    *, target: Target, peers: list[Peer], tc_candidates: list[str] | None = None
) -> str:
    """Render the competitor-analysis prompt.

    The peer list is truncated to MAX_PEERS to keep token cost predictable.
    `tc_candidates` are competitor names surfaced from the target's TechCrunch
    coverage (pass 1); the model revalidates them and combines with its own
    suggestions.
    """
    capped_peers = peers[:MAX_PEERS]
    return PROMPT_TEMPLATE.format(
        name=target.name,
        industry_group=target.industry_group,
        description_short=target.description_short,
        description_long=target.description_long,
        peer_block=_render_peer_block(capped_peers),
        candidate_block=_render_candidate_block(tc_candidates or []),
        max_competitors=MAX_COMPETITORS,
    )
