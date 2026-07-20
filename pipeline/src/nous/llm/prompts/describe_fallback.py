"""Describe-fallback — third-party-grounded description_short (GENERATIVE, GATED).

The description path for companies whose own website nous cannot read (the
Cloudflare-403 scrape cohort + the website-less residue — BACKLOG 2026-07-19
"missing-data residue"; owner-approved re-open of the deferred option "A").
Normal descriptions are written ONLY from the company's own scraped pages;
this prompt writes a SHORT factual description from third-party evidence nous
already holds: Wikidata entity facts and entity-guard-corroborated funding
coverage. The owner-approved framing: every clause traceable to the shown
evidence, attributed on-page as third-party-sourced, never presented as the
company's self-description.

Design rules (the moat is trust — this prompt is generative, so the gates
are stricter than anywhere else):

- **Evidence-bound.** Every claim in the description must be supported by the
  evidence shown in the prompt. No outside knowledge about the company, its
  market, or its products — if the model "knows" something the evidence
  doesn't show, it must not appear.
- **The non-funding-descriptor bar (deferred-design fix #3).** Funding facts
  alone ("raised $10B at a $130B valuation") do NOT license a description —
  a description that only restates money says nothing about what the company
  IS. The evidence must contain at least one product/business descriptor
  ("AI search unicorn", "spaceflight company", "sodium-ion battery maker");
  otherwise return null. The model must also echo the descriptor it relied
  on in ``grounding_descriptor`` so the caller can verify it appears in the
  evidence verbatim-ish (the same grounded-quote discipline as
  source_verification).
- **Null over thin.** An empty description is honest; a padded or guessed one
  is a moat breach. When evidence is thin, conflicting, or reads like it
  describes a different same-named entity, return null with the reason.
- **No funding figures in the text.** Amounts/valuations live in the funding
  timeline with per-fact sources; restating them in an LLM-written sentence
  creates a second, unsourced copy that can go stale.
- **Present-tense, neutral register.** One or two sentences, <= 260 chars,
  matching the site's existing description_short voice. No marketing
  superlatives that the evidence doesn't itself use.

Caller: the ``describe-fallback`` stage (dry-run probe first; the apply path
lands only after the prod dry run clears the quality gate). Sent through
``nous.llm.client.complete_json``; never import a provider SDK here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Stamped into the run summary now and, once the apply path lands, into the
# provenance stamp column. Scheme "<date>.<same-day-counter>"; bump on ANY
# semantic change to the template or schema.
# .3: no template change — the bump re-selects the cohort after the stage's
# long-profile evidence bar was corrected to count distinct OUTLETS (GN
# syndication had collapsed every outlet to news.google.com and zero longs
# were written under .2).
# .4: no template change — re-selects the cohort after suspect articles
# switched from silent-drop to LLM adjudication (title-case headlines had
# false-suspected real coverage and starved the evidence; .3-run catch).
# .5: rule 7 reworded AFFIRMATIVELY — under .4 the model offered a long
# profile once in 365 companies ("optional/better-null" read as "don't
# bother"); rich evidence now makes the profile the expected outcome.
PROMPT_VERSION: str = "2026-07-20.1"

# Cap on the combined evidence block (wikidata facts + article excerpts).
# Descriptors live in headlines/ledes; more text costs tokens without adding
# identity signal, and long inputs invite paraphrase drift. Raised from 6000
# to 9000 for the optional evidence-proportional LONG profile (2026-07-19.2):
# a grounded 1–2 paragraph profile needs more of the corroborated coverage in
# front of the model than a tagline does.
MAX_EVIDENCE_CHARS: int = 9000

# Hard cap enforced by the validator, mirroring the site's existing
# description_short lengths (tagline-sized, card-safe).
MAX_DESCRIPTION_CHARS: int = 260

# Hard cap on the optional LONG profile (2026-07-19.2), validator-enforced like
# MAX_DESCRIPTION_CHARS. Sized for 1–2 grounded paragraphs; a longer body is
# padding by construction, so over-cap nulls the long (never the short).
MAX_LONG_CHARS: int = 1400


class DescribeFallbackResult(BaseModel):
    """A third-party-grounded short description, or an explicit null."""

    description_short: str | None = Field(
        ...,
        description=(
            "One or two present-tense sentences saying what the company IS "
            "and does, supported ONLY by the evidence shown. Null when the "
            "evidence lacks a non-funding product/business descriptor, is "
            "too thin, or may describe a different same-named entity."
        ),
    )
    # OPTIONAL evidence-proportional profile (2026-07-19.2). DEFAULT None so the
    # #245 live recordings — committed before this field existed — still parse
    # (they carry no ``description_long`` key); a live re-record via
    # eval-record.yml follows after merge.
    description_long: str | None = Field(
        default=None,
        description=(
            "One or two present-tense, neutral-register paragraphs expanding "
            "on what the company IS and does, supported ONLY by the evidence "
            "shown. EXPECTED when the evidence contains a product/business "
            "descriptor and spans several independent sources; EVERY claim "
            "must be traceable to the evidence. No funding amounts, "
            "valuations, or investor names. Null only when the evidence "
            "genuinely cannot support multiple grounded sentences — a padded "
            "profile is worse than none."
        ),
    )
    grounding_descriptor: str | None = Field(
        ...,
        description=(
            "The strongest product/business descriptor phrase COPIED from "
            "the evidence that licenses the description (e.g. 'spaceflight "
            "company', 'AI search engine'). Null when description_short is "
            "null. Must be a phrase that actually appears in the evidence — "
            "it is checked."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        ...,
        description=(
            "How unambiguously the evidence describes THIS company. 'low' "
            "when descriptors are secondhand, sparse, or the entity could "
            "be conflated with a same-named other — low-confidence "
            "descriptions are never persisted."
        ),
    )
    null_reason: (
        Literal[
            "no_nonfunding_descriptor",
            "insufficient_evidence",
            "entity_ambiguity",
        ]
        | None
    ) = Field(
        ...,
        description=(
            "Why description_short is null (null when a description was "
            "produced). Makes every skip auditable in the run summary."
        ),
    )

    @model_validator(mode="after")
    def _enforce_gates(self) -> DescribeFallbackResult:
        """Code-level enforcement of the prompt's own rules (never trust
        prose alone): a description requires its grounding descriptor; a
        null requires its reason; length is card-safe. The optional LONG
        profile is nulled when it has no tagline to ride on, or when it
        exceeds its own cap (the short is kept in the over-cap case)."""
        if self.description_short is not None:
            if not (self.grounding_descriptor or "").strip():
                # No descriptor evidence echoed -> the non-funding bar was
                # not met; drop to an honest null rather than keep a
                # possibly-ungrounded description. A long profile is invalid
                # without its tagline, so it goes too.
                self.description_short = None
                self.grounding_descriptor = None
                self.description_long = None
                self.null_reason = "no_nonfunding_descriptor"
                return self
            if len(self.description_short) > MAX_DESCRIPTION_CHARS:
                self.description_short = None
                self.grounding_descriptor = None
                self.description_long = None
                self.null_reason = "insufficient_evidence"
                return self
            self.null_reason = None
            # The LONG profile is a valid rider ONLY on a valid tagline (true
            # here). Over-cap nulls the LONG ONLY — the short tagline stands.
            if (
                self.description_long is not None
                and len(self.description_long) > MAX_LONG_CHARS
            ):
                self.description_long = None
        else:
            # A profile without a tagline is invalid: null the long.
            self.description_long = None
            if self.null_reason is None:
                self.null_reason = "insufficient_evidence"
        return self


PROMPT_TEMPLATE = """You write one short, strictly factual description of a \
company for a startup-data site, using ONLY the evidence provided below.

The site's rule is absolute: no fabrication. Every claim you write must be \
supported by the evidence shown here. You must not use any outside knowledge \
about this company, its products, or its market — if it is not in the \
evidence, it does not exist for this task.

Company name: {company_name}

Evidence (third-party: Wikidata entity facts and press coverage nous has \
verified is about this company):
---
{evidence}
---

Rules:
1. Write "description_short": one or two present-tense sentences (max \
{max_chars} characters) saying what the company IS and does — its product, \
business, or field. Neutral register; no marketing superlatives the evidence \
doesn't itself use.
2. THE DESCRIPTOR BAR: the evidence must contain at least one NON-FUNDING \
product/business descriptor (e.g. "spaceflight company", "AI search engine", \
"sodium-ion battery maker"). Funding amounts, valuations, and investor names \
are NOT descriptors. If no such descriptor exists, return null with \
null_reason "no_nonfunding_descriptor".
3. Copy the strongest descriptor phrase you relied on into \
"grounding_descriptor", as it appears in the evidence. It will be checked \
against the evidence text.
4. Do NOT include funding amounts, valuations, or investor names in \
description_short — those render elsewhere with their own sources.
5. If the evidence is too thin to say what the company does, return null \
with null_reason "insufficient_evidence". If the evidence might describe a \
DIFFERENT company with the same or a similar name (different field, \
different location than the profile suggests, a fuller different name), \
return null with null_reason "entity_ambiguity".
6. Better no description than a guessed one. Null is a correct answer.
7. "description_long": when the evidence above contains a product/business \
descriptor AND spans several independent sources, you are EXPECTED to also \
write a fuller profile — one or two present-tense, neutral paragraphs \
saying what the company does, builds, and is known for, where EVERY claim \
is traceable to the evidence above. Synthesize across the sources; do not \
merely restate the short description. Same bans as the short: no funding \
amounts, valuations, or investor names. Max {max_long_chars} characters. \
Return null for it ONLY when the evidence genuinely cannot support multiple \
grounded sentences (e.g. a lone Wikidata line) — a padded or guessed \
profile is worse than none, but declining to synthesize evidence that is \
present is also a failure. The rules for description_short are UNCHANGED.

Return JSON matching the schema."""


def build_prompt(*, company_name: str, evidence: str) -> str:
    """Format the describe-fallback prompt for one company.

    ``evidence`` is the caller-assembled block (Wikidata facts first, then
    corroborated article titles/excerpts, each with its source URL) already
    truncated to ``MAX_EVIDENCE_CHARS``.
    """
    return PROMPT_TEMPLATE.format(
        company_name=company_name,
        evidence=evidence,
        max_chars=MAX_DESCRIPTION_CHARS,
        max_long_chars=MAX_LONG_CHARS,
    )
