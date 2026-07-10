"""Generic metric primitives for the golden-set harness.

Three families:

- **Slot precision/recall/F1** (:class:`SlotTally`) for nullable extraction
  fields: expected-null + got-null contributes nothing; a wrong or invented
  value is a false positive; a missed value is a false negative; a value
  mismatch counts as both (the model asserted something wrong AND missed the
  truth).
- **Set precision/recall/F1** for list-valued fields (investors, tags,
  people), computed over normalized elements.
- **Free-text structural checks** for description fields: length/paragraph
  bounds plus a *grounding* proxy for fabrication — proper-noun-ish tokens
  and numbers appearing in the output must also appear in the input text.
  Grounding is a proxy, not a hallucination oracle: it catches invented
  names and figures, not invented claims built from common words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel


class SlotTally(BaseModel):
    """Running tp/fp/fn counts for slot-style (nullable field) extraction."""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, *, expected_present: bool, got_present: bool, match: bool) -> None:
        """Record one slot observation.

        ``match`` is only consulted when both sides are present.
        """
        if expected_present and got_present:
            if match:
                self.tp += 1
            else:
                # Asserted a wrong value (fp) and missed the true one (fn).
                self.fp += 1
                self.fn += 1
        elif got_present:
            self.fp += 1
        elif expected_present:
            self.fn += 1

    def add_sets(self, expected: set[str], got: set[str]) -> None:
        """Fold a set comparison into the tally (micro-aggregation)."""
        self.tp += len(expected & got)
        self.fp += len(got - expected)
        self.fn += len(expected - got)

    @property
    def precision(self) -> float:
        """tp / (tp + fp); 1.0 when the model asserted nothing (vacuous)."""
        denom = self.tp + self.fp
        return self.tp / denom if denom else 1.0

    @property
    def recall(self) -> float:
        """tp / (tp + fn); 1.0 when there was nothing to find (vacuous)."""
        denom = self.tp + self.fn
        return self.tp / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


class Accuracy(BaseModel):
    """Running exact-match accuracy counter."""

    correct: int = 0
    total: int = 0

    def add(self, match: bool) -> None:
        self.total += 1
        if match:
            self.correct += 1

    @property
    def value(self) -> float:
        return self.correct / self.total if self.total else 1.0


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 1.0


# ---------------------------------------------------------------------------
# Free-text structural checks
# ---------------------------------------------------------------------------

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")


def paragraph_count(text: str) -> int:
    """Number of non-empty blank-line-separated blocks."""
    return len([p for p in _PARAGRAPH_SPLIT.split(text) if p.strip()])


# Word-ish tokens; keeps &/./- so "R&D", "Y.C.", "co-founder" stay whole.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9&.\-']*")
# Digit runs with optional thousands separators / decimals ("45", "1.2", "12,000").
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Sentence/fragment boundaries: after ./!/?/: or on any newline. The first
# word of each fragment is skipped by the proper-noun scan — English
# capitalizes sentence starts, so only mid-sentence capitals are treated as
# proper-noun evidence.
_FRAGMENT_SPLIT = re.compile(r"(?<=[.!?:])\s+|\n+")

# Generic capitalized tokens that are not proper-noun evidence: common
# acronyms/initialisms and title-cased generic words that legitimately show
# up in analyst prose without appearing verbatim in the source text.
_GROUNDING_STOPWORDS: frozenset[str] = frozenset(
    {
        "ai", "api", "apis", "arr", "aws", "b2b", "b2c", "ceo", "cfo", "cio",
        "ciso", "cmo", "coo", "cpo", "cro", "cto", "eu", "gdpr", "gtm", "hq",
        "hr", "ipo", "it", "llm", "llms", "ml", "r&d", "saas", "sdk", "seo",
        "smb", "smbs", "soc", "sql", "uk", "us", "usd", "vc", "vcs",
        "i", "markdown", "json",
    }
)


def _proper_noun_tokens(text: str) -> list[str]:
    """Capitalized tokens in non-sentence-initial position (>= 3 chars)."""
    tokens: list[str] = []
    for fragment in _FRAGMENT_SPLIT.split(text):
        words = _WORD_RE.findall(fragment)
        # Skip the fragment's first word: sentence-initial capitalization is
        # not proper-noun evidence. Also skip markdown emphasis/heading noise
        # by stripping leading non-letters (the regex already does).
        for word in words[1:]:
            if len(word) < 3 or not word[0].isupper():
                continue
            if word.lower().strip(".-'") in _GROUNDING_STOPWORDS:
                continue
            tokens.append(word)
    return tokens


def grounding_fraction(text: str, source: str) -> float:
    """Fraction of proper-noun/number tokens in ``text`` found in ``source``.

    The no-fabrication proxy: a description that names entities or figures
    absent from the input is presumed to be inventing them. Returns 1.0 when
    the text asserts no checkable tokens.
    """
    source_lower = source.lower()
    source_digits = re.sub(r"[,\s]", "", source_lower)
    checked = 0
    found = 0
    for token in _proper_noun_tokens(text):
        checked += 1
        if token.lower().strip(".-'") in source_lower:
            found += 1
    for number in _NUM_RE.findall(text):
        checked += 1
        plain = number.replace(",", "")
        if number in source or plain in source_digits:
            found += 1
    return found / checked if checked else 1.0
