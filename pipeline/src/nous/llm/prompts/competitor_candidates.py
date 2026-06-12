"""Competitor-candidate extraction prompt (pass 1 of the two-step flow).

Input: a target company and the text of its TechCrunch articles. Output: the
names of competitor companies that the coverage names or strongly implies, each
tagged with the article URL it came from. These candidates are then fed into the
competitor-analysis prompt (pass 2), which revalidates them against the target
and combines them with general-knowledge ("LLM-inferred") competitors.

Per CLAUDE.md, the prompt instructs the model to return an empty list rather
than invent competitors.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Per-article text cap for the candidate prompt. Intentionally below the shared
# MAX_PROMPT_INPUT_CHARS ceiling (32_000 in nous.llm.client): TechCrunch funding
# pieces are short and only the competitor-name signal is needed, so a tight cap
# bounds multi-article token cost without losing useful signal.
MAX_ARTICLE_CHARS = 6_000
MAX_ARTICLES = 8


class TechCrunchArticle(BaseModel):
    url: str
    text: str


class CandidateMention(BaseModel):
    name: str = Field(..., min_length=1, description="Competitor company name.")
    article_url: str = Field(
        ...,
        description="The URL of the article this competitor was named in.",
    )


class CompetitorCandidates(BaseModel):
    candidates: list[CandidateMention] = Field(default_factory=list)


PROMPT_TEMPLATE = """\
You are reading TechCrunch coverage about a company and extracting the OTHER
companies it names as competitors or close alternatives.

Target company: {name}

For each article below, find companies that the article presents as competitors,
rivals, or direct alternatives to {name} (e.g. "competes with X", "going up
against Y", "an alternative to Z").

Rules:
- Only include companies the coverage actually names in a competitive context.
- Do NOT include the target company itself, investors, customers, or partners.
- Do NOT invent companies. If an article names no competitors, include nothing
  for it. Return an empty list if none of the articles name any.
- For each competitor, record the exact `article_url` it appeared in.

Articles:
{article_block}

Return JSON matching the schema.
"""


def _render_article_block(articles: list[TechCrunchArticle]) -> str:
    blocks = []
    for art in articles[:MAX_ARTICLES]:
        blocks.append(f"[{art.url}]\n{art.text[:MAX_ARTICLE_CHARS]}")
    return "\n\n---\n\n".join(blocks)


def build_candidates_prompt(
    *, target_name: str, articles: list[TechCrunchArticle]
) -> str:
    return PROMPT_TEMPLATE.format(
        name=target_name,
        article_block=_render_article_block(articles),
    )
