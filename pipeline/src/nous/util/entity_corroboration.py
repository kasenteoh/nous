"""Entity corroboration — does an article's text corroborate THIS company?

The 2026-07-17 QA sweep exposed the attribution class one level below the
``article_mentions_company`` relevance guard: same-name DIFFERENT-entity
rounds, which the mention guard passes BY CONSTRUCTION (the article about
Primary Wave really does say "Wave"; the IM8 story really does contain the
word "bespoke"). This module scores cheap, deterministic ($0, no LLM)
signals that separate "this article is about our company" from "this article
is about a same-named other entity":

- **lowercase-only**: every occurrence of the name is a common-word usage
  ("bespoke supplements"), never a proper noun. The strongest wrongness
  signal for dictionary-word names.
- **extension**: proper-noun occurrences are consistently embedded in a
  LONGER capitalized phrase that is not this company's own name ("Primary
  Wave", "Third Wave Automation", "Impulse Dynamics", "TerraFirma Inc").
  The dominant observed wrong-entity shape.
- **context overlap**: distinctive tokens from the company's own description
  found in the article text. An article about the right company almost
  always shares vocabulary with its profile; a wrong-entity article about a
  music-rights fund shares none with a mobile-money description.

Consumers: the ``audit-round-entities`` probe (retroactive, report-only) and
the ingest-time entity guard (these signals as the cheap pre-filter; LLM
company-match adjudication only for the ambiguous middle).

Fail-open philosophy for the PROBE (report, never delete): a round with no
stored text is "unknown", not suspect. Individual signals are surfaced with
reasons so an operator (or the LLM adjudicator) sees WHY.
"""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel, Field

from nous.util.slugify import strip_corporate_suffix

# Corporate suffixes / functional capitalized words that legitimately follow a
# company name without indicating a DIFFERENT entity: "Wave Inc raised…",
# "Wonder CEO Marc Lore", "Impulse Series D", and Title-Case headline verbs
# ("Bespoke Labs Raises $40M"). Extension through one of these is not
# evidence of another company. (The verb set mirrors sources/news.py's
# _FUNDING_VERBS_AFTER; kept local to avoid a util -> sources dependency.)
_NEUTRAL_FOLLOWERS: frozenset[str] = frozenset(
    {
        "inc",
        "llc",
        "corp",
        "corporation",
        "ltd",
        "co",
        "ceo",
        "cto",
        "coo",
        "cfo",
        "founder",
        "cofounder",
        "co-founder",
        "president",
        "chairman",
        "chief",
        "series",
        "ipo",
        # Title-Case headline verbs / auxiliaries (superset of sources/
        # news.py's _FUNDING_VERBS_AFTER plus common non-funding headline
        # verbs — review finding: "Acme Plans $50M Expansion" must not read
        # as an entity named "Acme Plans").
        "raises",
        "raised",
        "raise",
        "raising",
        "secures",
        "secured",
        "lands",
        "landed",
        "closes",
        "closed",
        "announces",
        "announced",
        "nabs",
        "nabbed",
        "banks",
        "bags",
        "bagged",
        "grabs",
        "grabbed",
        "gets",
        "got",
        "adds",
        "attracts",
        "receives",
        "received",
        "completes",
        "completed",
        "reaches",
        "scores",
        "launches",
        "launched",
        "expands",
        "acquires",
        "acquired",
        "valued",
        "hits",
        "tops",
        "joins",
        "opens",
        "brings",
        "wins",
        "won",
        "plans",
        "aims",
        "seeks",
        "files",
        "reports",
        "reveals",
        "says",
        "said",
        "targets",
        "enters",
        "moves",
        "hires",
        "buys",
        "sells",
        "starts",
        "begins",
        "makes",
        "sets",
        "eyes",
        "unveils",
        "debuts",
        "partners",
        "taps",
        "picks",
        "names",
        "will",
        "has",
        "was",
        "is",
        "to",
        # Title-Case adverbs/auxiliaries mid-headline ("Zepto Also Expands")
        "also",
        "now",
        "just",
        "still",
        "may",
        "might",
        "could",
        "can",
        "reportedly",
        "officially",
        "finally",
        "nearly",
        "almost",
        "again",
        "soon",
        "more",
        # Financial nouns / verbs following a company name in funding
        # headlines ("xAI Private Funding", "Flexport Valuation Hits…") —
        # third prod triage. NOT "capital"/"group"/"labs": those decide real
        # other-entity names (Drip Capital, Amber Group).
        "private",
        "funding",
        "round",
        "deal",
        "valuation",
        "investment",
        "investors",
        "stock",
        "shares",
        "lets",
        "strikes",
        "signs",
        "seals",
        "represents",
        "chatbot",
        # Informal same-company suffix: coverage writes "Cognition AI" for
        # the company named Cognition. "<Name> AI" is not another entity.
        "ai",
    }
)

# Title-Case DESCRIPTOR nouns that precede a company name in headlines
# ("Fusion Startup Helion…", "Legal AI Platform Legora Raises…") — category
# words, not entity names. A real other-entity prefix ("Primary Wave",
# "Third Wave") is a proper name, never a category noun. Kept small and
# generic; second prod probe triage is the source.
_NEUTRAL_PRECEDERS: frozenset[str] = frozenset(
    {
        "startup",
        "startups",
        "company",
        "firm",
        "maker",
        "developer",
        "platform",
        "app",
        "giant",
        "unicorn",
        "rival",
        "competitor",
        "fintech",
        "biotech",
        "insurtech",
        "edtech",
        "healthtech",
        "proptech",
        "exclusive",
        "breaking",
        "report",
        "reports",
        "sources",
        "update",
        "watch",
        "venture-backed",
        "the",
        "a",
        "an",
        "industry",
        "start-up",
        "startup's",
    }
)

# Preceding words shaped like "<X>-backed" / "London-based" / "founder-led":
# investor/geography attributions, never a different entity's name.
_ATTRIBUTIVE_PREFIX_RE = re.compile(
    r"-(backed|based|led|founded|owned|focused|native|headquartered)$",
    re.IGNORECASE,
)

# Generic words too common in startup coverage to be distinctive description
# context ("the AI software platform company" describes half the catalog).
_GENERIC_CONTEXT: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "based",
        "building",
        "builds",
        "business",
        "companies",
        "company",
        "customers",
        "data",
        "develops",
        "digital",
        "enables",
        "for",
        "from",
        "helps",
        "in",
        "into",
        "is",
        "its",
        "of",
        "offering",
        "offers",
        "on",
        "platform",
        "products",
        "provides",
        "services",
        "software",
        "solution",
        "solutions",
        "startup",
        "startups",
        "technology",
        "that",
        "the",
        "their",
        "through",
        "to",
        "tools",
        "us",
        "users",
        "using",
        "with",
        "ai",
        "app",
        "apps",
    }
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9''\-]*")

# Adjacency boundary: a sentence terminator OR a space-adjacent dash/pipe
# (the Google-News "Title - Outlet" convention). A no-space hyphen
# ("Impulse-Dynamics") is genuine entity punctuation and stays adjacent.
_BOUNDARY_RE = re.compile(r"[.!?:]|\s[-–—|]|[-–—|]\s")

# Minimum distinctive description tokens for the context-overlap signal to be
# meaningful; below this the signal reports as unavailable rather than firing
# on noise.
_MIN_CONTEXT_CANDIDATES = 4


class CorroborationResult(BaseModel):
    """Signals for one (company, text) pair. ``suspect`` is the probe verdict;
    ``reasons`` name the signals that fired, ``evidence`` the top extended
    phrase(s) so a report reads as an explanation, not a score."""

    occurrences: int = 0
    proper_occurrences: int = 0
    extended_occurrences: int = 0
    lowercase_only: bool = False
    context_candidates: int = 0
    context_overlap: int = 0
    suspect: bool = False
    reasons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


def _name_token_pattern(tokens: list[str]) -> re.Pattern[str]:
    """Case-insensitive whole-word pattern for the name token sequence,
    tolerating punctuation/whitespace between tokens."""
    joined = r"[\s\-.,:;'']+".join(re.escape(t) for t in tokens)
    return re.compile(rf"(?<![A-Za-z0-9]){joined}(?![A-Za-z0-9])", re.IGNORECASE)


def _is_proper(word: str) -> bool:
    """Proper-noun-shaped: carries ANY uppercase letter. Initial-cap is the
    normal case; stylized names ("xAI", "iPhone"-class) must count too — the
    first prod probe run false-flagged xAI as "lowercase-only". An all-
    lowercase common-word usage ("bespoke supplements") stays non-proper."""
    return any(ch.isupper() for ch in word)


def _prev_word(text: str, pos: int) -> tuple[str | None, int]:
    """The word immediately before ``pos`` and its start offset, or (None, pos)
    when a boundary (sentence terminator / outlet dash — _BOUNDARY_RE)
    intervenes. The first prod probe run showed "…at $380B Valuation - Built
    In" reading "Valuation Built" as an entity — hence the boundary rule."""
    m = re.search(r"([A-Za-z][A-Za-z0-9''\-]*)([^A-Za-z0-9]*)$", text[:pos])
    if m and not _BOUNDARY_RE.search(m.group(2)):
        return m.group(1), m.start(1)
    return None, pos


def _next_word(text: str, pos: int) -> tuple[str | None, int]:
    """The word immediately after ``pos`` and its end offset, or (None, pos)
    when a boundary intervenes."""
    m = re.match(r"([^A-Za-z0-9]*)([A-Za-z][A-Za-z0-9''\-]*)", text[pos:])
    if m and not _BOUNDARY_RE.search(m.group(1)):
        return m.group(2), pos + m.end(2)
    return None, pos


def corroborate_entity(
    company_name: str,
    description: str | None,
    text: str,
    *,
    own_tokens: set[str] | None = None,
    own_context: str | None = None,
) -> CorroborationResult:
    """Score whether ``text`` (article title + body) corroborates
    ``company_name`` as ITS subject. See module docstring for the signals.

    Deterministic and $0. Biased toward explanation over cleverness: every
    ``suspect`` verdict carries reasons and, for the extension signal, the
    offending phrase(s) with counts.
    """
    result = CorroborationResult()
    # No stored text → UNKNOWN, never suspect (probe philosophy: report what
    # the evidence shows; absence of evidence is a coverage gap, not a verdict).
    if not text.strip():
        return result
    stripped = strip_corporate_suffix(company_name)
    name_tokens = [t.lower() for t in _WORD_RE.findall(stripped)]
    if not name_tokens:
        return result
    # ``own_tokens`` lets a caller evaluating a name VARIANT (head token,
    # squashed) keep the ORIGINAL full name's tokens: "Yuga Labs" following
    # the head-variant "Yuga" is the company's own name, not another entity
    # (second prod probe triage: yuga-labs flagged on its own suffix).
    own_full_tokens = {t.lower() for t in _WORD_RE.findall(company_name)}
    if own_tokens:
        own_full_tokens |= {t.lower() for t in own_tokens}
    # The company's own-identity vocabulary: description + whatever the
    # caller supplies (website host, slug). A neighbor word from this set is
    # the company's own FORMAL name, not another entity — "Impulse Space"
    # for the impulse whose description says "space logistics" (third prod
    # triage: a company's fuller corporate name flagged as an extension).
    # Squashed view of the identity context (description + website + slug):
    # a neighbor is the company's own FORMAL name only when the WHOLE
    # extended phrase appears contiguously — "impulsespace.com" owns
    # "Impulse Space"; a description that merely mentions "capital" or
    # "group" somewhere must NOT clear "Drip Capital"/"Amber Group"
    # (review catch: a word-level check here shadowed this and undid the
    # deliberate _NEUTRAL_FOLLOWERS exclusion of entity-deciding nouns).
    own_identity_squashed = re.sub(
        r"[^a-z0-9]", "", f"{description or ''} {own_context or ''}".lower()
    )

    def _is_own_phrase(phrase: str) -> bool:
        squashed = re.sub(r"[^a-z0-9]", "", phrase.lower())
        return bool(squashed) and squashed in own_identity_squashed

    pattern = _name_token_pattern(name_tokens)
    extended_phrases: Counter[str] = Counter()

    for m in pattern.finditer(text):
        result.occurrences += 1
        matched_words = _WORD_RE.findall(m.group(0))
        # Proper-noun occurrence: every token of the match is capitalized.
        # (All-caps headlines pass; a lowercase common-word usage does not.)
        if not all(_is_proper(w) for w in matched_words):
            continue
        result.proper_occurrences += 1

        extended_via: list[str] = []
        # Walk OUTWARD past the company's own tokens before judging the
        # neighbor: the head-variant "Samba" must look through its own
        # "Probe" to see "Samba Probe Dynamics" (and "Yuga" through "Labs"
        # to see a plain verb). Then:
        # - leftward ("Primary Wave"): excluded are neutral verbs/roles,
        #   Title-Case category nouns ("Fusion Startup Helion"), and
        #   POSSESSIVES — "Fei-Fei Li's World Labs" / "India's Zepto"
        #   attribute ownership/origin, they don't name another entity;
        # - rightward ("Impulse Dynamics" / "TerraFirma Inc"-class):
        #   excluded are the neutral corporate/role/verb words.
        chain_left: list[str] = []
        word, pos = _prev_word(text, m.start())
        while word is not None and word.lower() in own_full_tokens:
            chain_left.insert(0, word)
            word, pos = _prev_word(text, pos)
        if (
            word is not None
            and _is_proper(word)
            and word.lower() not in _NEUTRAL_FOLLOWERS
            and word.lower() not in _NEUTRAL_PRECEDERS
            and not re.search(r"['']s?$", word)
            and not _ATTRIBUTIVE_PREFIX_RE.search(word)
        ):
            phrase = " ".join((word, *chain_left, m.group(0)))
            if not _is_own_phrase(phrase):
                extended_via.append(phrase)
        chain_right: list[str] = []
        word, pos = _next_word(text, m.end())
        while word is not None and word.lower() in own_full_tokens:
            chain_right.append(word)
            word, pos = _next_word(text, pos)
        if (
            word is not None
            and _is_proper(word)
            and word.lower() not in _NEUTRAL_FOLLOWERS
        ):
            phrase = " ".join((m.group(0), *chain_right, word))
            if not _is_own_phrase(phrase):
                extended_via.append(phrase)
        if extended_via:
            result.extended_occurrences += 1
            for phrase in extended_via:
                extended_phrases[phrase] += 1

    # Description-context overlap FIRST — it gates the lowercase verdict.
    context_available = False
    if description:
        candidates = {
            w.lower()
            for w in _WORD_RE.findall(description)
            if len(w) > 3
            and w.lower() not in _GENERIC_CONTEXT
            and w.lower() not in own_full_tokens
        }
        result.context_candidates = len(candidates)
        if len(candidates) >= _MIN_CONTEXT_CANDIDATES:
            context_available = True
            text_lower = text.lower()
            result.context_overlap = sum(
                1
                for w in candidates
                if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text_lower)
            )

    if result.occurrences and result.proper_occurrences == 0:
        result.lowercase_only = True
        # A distinctive all-lowercase brand ("n8n", "claroty") is not a
        # common-word usage — its article shares profile vocabulary. Only
        # condemn lowercase-only when the context ALSO fails to corroborate
        # (third prod triage: n8n/claroty/fal-ai false-flagged). The bar is
        # >= 2 overlapping words: one shared generic-ish word is coincidence
        # (review catch), while a right-company article shares several.
        if not context_available or result.context_overlap < 2:
            result.suspect = True
            result.reasons.append(
                "name occurs only as a lowercase common word, never as a "
                "proper noun"
            )

    if result.proper_occurrences:
        # Evidence is always surfaced (consumers apply their own text-kind
        # calibration — a headline-only text can't repeat a phrase).
        result.evidence = [
            f"{p} ({n}x)" for p, n in extended_phrases.most_common(3)
        ]
        # A real other-entity name REPEATS ("Primary Wave" x4); a stray
        # capitalized neighbor does not. Require a phrase seen >=2 times AND
        # the consistent phrases to cover most proper occurrences — one
        # Title-Case artifact in an otherwise-bare-mention article stays quiet.
        consistent = [
            (p, n) for p, n in extended_phrases.most_common(3) if n >= 2
        ]
        covered = sum(n for _, n in consistent)
        if consistent and covered / result.proper_occurrences >= 0.6:
            result.suspect = True
            result.reasons.append(
                "name consistently embedded in a longer entity phrase"
            )

    # The overlap alone is a WEAK signal: it only flags when the article
    # also failed to use the bare name more than once — a well-covered
    # right-company article nearly always shares profile vocabulary.
    if (
        context_available
        and result.context_overlap == 0
        and result.proper_occurrences <= 1
        and not result.suspect
    ):
        result.suspect = True
        result.reasons.append(
            "zero description-context overlap and at most one bare "
            "proper-noun mention"
        )

    return result
