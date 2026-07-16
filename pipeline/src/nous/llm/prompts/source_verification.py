r"""Source-verification prompt — DISCRIMINATIVE fact-checking of a rendered fact
against its cited source.

Input: a single ``claim`` (a fact nous renders on a company page, e.g. "Acme has
raised a total of $12M") and the ``source_text`` of the citation nous already
stores for that fact. Output: a verdict — ``supported`` / ``unsupported`` /
``uncertain`` — plus, for ``supported`` only, a VERBATIM ``supporting_quote``
copied from the source.

This is the LLM half of the "✓ Verified against source" enhancement (spec
``docs/superpowers/specs/2026-07-14-provenance-ui-design.md`` → "Optional
enhancement: source-verification"). The moat is trust, so the prompt is
adversarially hardened around one rule: **never claim a verification we don't
have.**

- **Discriminative, never generative.** The model judges an existing claim
  against an existing source. It writes no prose, invents no facts, and uses no
  outside knowledge — one hallucinated "supported" destroys the trust this sells.
- **Empty-not-fabricate.** ``uncertain`` is the correct answer whenever the
  source is silent, ambiguous, or only partially relevant — preferred over a
  shaky ``supported``. Only ``supported`` earns the public ✓.
- **Grounded quote.** A ``supported`` verdict MUST carry a quote copied verbatim
  from the source. The schema drops a quote-less ``supported`` to ``uncertain``;
  the stage additionally re-checks the quote is a real substring of the source
  (see :func:`quote_is_grounded`) and treats a non-grounded quote as a
  fabrication signal — belt-and-suspenders against a paraphrased/invented span.

No provider SDK is imported here — the stage sends ``build_prompt`` through
``nous.llm.client.complete_json`` like every other prompt.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Version stamped onto verification rows this prompt produces (once the
# persisting apply stage + fact_verifications table land). Scheme
# "<date>.<same-day-counter>"; bump on ANY semantic change to the template,
# schema, or validators so a claim verified by a bad revision can be found and
# re-verified (mirrors the enrichment / career-history version stamps).
# 2026-07-16.1: quote_is_grounded accepts ellipsis-elided quotes (every
# fragment verbatim, in order, ≥ _MIN_FRAGMENT_CHARS). A semantic change to
# what persists as grounded 'supported', so the bump re-selects the cohort —
# including the facts the stricter check wrongly downgraded to 'uncertain'
# (12 of 500 in the 2026-07-15 apply run were "..."-elided legitimate quotes).
PROMPT_VERSION: str = "2026-07-16.1"

Verdict = Literal["supported", "unsupported", "uncertain"]


class SourceVerification(BaseModel):
    """A discriminative verdict on whether a source supports a claim.

    Empty-not-fabricate is the design centre: ``uncertain`` is the dominant-safe
    answer, and only ``supported`` (with a grounded quote) may ever surface the
    public "✓ Verified against source" affordance.
    """

    verdict: Verdict = Field(
        ...,
        description=(
            "'supported' ONLY if the source text explicitly states (or "
            "unambiguously entails) the claim — and you can quote the exact span "
            "that says so. 'unsupported' if the source addresses the subject but "
            "contradicts the claim (e.g. a different amount, round, or status). "
            "'uncertain' if the source is silent, ambiguous, or only partially "
            "relevant — this is the correct answer whenever you are unsure. Never "
            "guess 'supported'."
        ),
    )
    supporting_quote: str | None = Field(
        default=None,
        description=(
            "A short span copied VERBATIM from the SOURCE text that states the "
            "claim — required when verdict is 'supported', null otherwise. Copy "
            "exactly; never paraphrase, summarize, translate, or invent. If no "
            "exact supporting span exists, the verdict is not 'supported'."
        ),
    )

    @model_validator(mode="after")
    def _enforce_quote_discipline(self) -> SourceVerification:
        # A quote is meaningful only for 'supported'. And a 'supported' with no
        # verbatim quote cannot be trusted — drop it to 'uncertain' rather than
        # let an unquoted "support" flow through (empty-not-fabricate). The
        # stage separately verifies the quote is a real substring of the source
        # (quote_is_grounded); this validator only enforces the structural rule
        # (it has no access to the source text here).
        quote = (self.supporting_quote or "").strip()
        if self.verdict != "supported":
            object.__setattr__(self, "supporting_quote", None)
        elif not quote:
            object.__setattr__(self, "verdict", "uncertain")
            object.__setattr__(self, "supporting_quote", None)
        else:
            object.__setattr__(self, "supporting_quote", quote)
        return self


def _normalize(text: str) -> str:
    """Whitespace-collapsed, case-folded form for a lenient substring compare."""
    return re.sub(r"\s+", " ", text).strip().casefold()


# An elided quote's fragments, split on "..." / "…". Each fragment must clear
# this length so a trivial connective ("the", "and it") can't stitch unrelated
# source text into a fake supporting span.
_ELLIPSIS_RE = re.compile(r"\.{3}|…")
_MIN_FRAGMENT_CHARS = 12


def quote_is_grounded(quote: str | None, source_text: str) -> bool:
    """True when *quote* appears in *source_text* (whitespace/case-normalized).

    The anti-fabrication guard: a 'supported' verdict whose quote is NOT a
    verbatim span of the source means the model paraphrased or invented it, so
    the claim is not actually verified. Normalization tolerates reformatting
    (collapsed whitespace, case) but not invented content. An empty quote is
    never grounded.

    Ellipsis-elided quotes ("Acme ... raised $80 million ... led by X") are
    accepted iff EVERY fragment is itself a verbatim (normalized) substring,
    the fragments appear in order without overlapping, and each is at least
    ``_MIN_FRAGMENT_CHARS`` long — the model often elides mid-sentence
    boilerplate, and rejecting those wholesale cost legitimate ✓s (12/500 in
    the 2026-07-15 apply run) without adding safety: in-order verbatim
    fragments are still the source's own words about the claim. A fragment
    that fails any condition rejects the WHOLE quote (fail closed).
    """
    if not quote or not quote.strip():
        return False
    src = _normalize(source_text)
    fragments = [f for f in (_normalize(p) for p in _ELLIPSIS_RE.split(quote)) if f]
    if not fragments:
        return False
    if len(fragments) == 1:
        # Non-elided quote: the original exact-substring rule, no length floor.
        return fragments[0] in src
    pos = 0
    for fragment in fragments:
        if len(fragment) < _MIN_FRAGMENT_CHARS:
            return False
        idx = src.find(fragment, pos)
        if idx == -1:
            return False
        pos = idx + len(fragment)
    return True


PROMPT_TEMPLATE = """\
You are a strict fact-checker. Decide whether a SOURCE document supports a
specific CLAIM about a company. You are CHECKING an existing claim against an
existing source — you never add facts, never guess, and never use outside
knowledge.

Return exactly one verdict:
- "supported": the SOURCE text explicitly states, or unambiguously entails, the
  CLAIM. You MUST also return a `supporting_quote`: a short span copied VERBATIM
  from the SOURCE that states it.
- "unsupported": the SOURCE addresses this subject but CONTRADICTS the claim or
  states something incompatible with it (a different amount, a different round,
  a different status).
- "uncertain": the SOURCE does not clearly address the claim — it is silent,
  ambiguous, or only partially relevant. Choose this whenever you are not sure.
  Prefer "uncertain" over a shaky "supported".

Rules:
- Judge ONLY against the SOURCE text below. Do not use any outside or prior
  knowledge about the company.
- The `supporting_quote` is REQUIRED for "supported" and must be copied EXACTLY
  from the SOURCE (a verbatim substring) — never paraphrase, summarize,
  translate, or invent. If you cannot find an exact span that states the claim,
  the verdict is NOT "supported".
- Numbers: "supported" needs the SOURCE to state the SAME figure. Treat "$12M",
  "$12 million", and "$12,000,000" as the same; treat rounding words ("about",
  "nearly") as compatible. A near-but-different figure (e.g. $12M vs $15M) is
  "unsupported".
- Leave `supporting_quote` null for "unsupported" and "uncertain".

CLAIM: {claim}

SOURCE:
---
{source_text}
---

Return JSON only.
"""


def build_prompt(*, claim: str, source_text: str) -> str:
    """Build the source-verification prompt for one (claim, source) pair."""
    return PROMPT_TEMPLATE.format(claim=claim, source_text=source_text)
