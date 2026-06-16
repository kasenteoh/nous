"""Decide whether a company name is the *dominant subject* of a page title /
heading, vs. merely one brand *listed among others*.

Why this exists
---------------
``resolve_homepage`` accepts a candidate page when the company name appears in a
"strong position" — the ``<title>`` or an ``<h1>``.  That guard rejects pure
body-text mentions (directory listings), but it is fooled by a *different*
company's homepage that lists the target as one of several brands:

- **Kalshi** (a prediction market) was attached to **FrenFlow**'s site, whose
  page reads "multi-venue prediction-market platform … copy-trade across
  Polymarket, Kalshi, Predict.fun, Hyperliquid".  "Kalshi" appears in an ``<h1>``
  — a strong position — but FrenFlow, not Kalshi, is the subject.
- **AgentMail** was attached to a site whose subject is "Series V".

The fix: require the company name to be the *dominant subject* of the strong
text, not one entry in a list of other brands, and reject when the strong text's
*leading brand* is unmistakably a different single brand.

Design stance (mirrors ``parked.py``): a false *accept* attaches a wrong-company
description to a real company (the exact production bug — expensive, user-facing,
and it poisons enrichment).  A false *reject* merely leaves a company
website-less for another resolve attempt (cheap, self-healing).  So when the
signal is ambiguous we lean toward rejecting — BUT only on *clear* competing-brand
evidence, so ordinary single-subject homepages (whose title is "Acme — tagline",
"Acme | Pricing", "Welcome to Acme", a bare "Acme", or even "Acme vs Bar")
continue to pass.

Pure functions, no I/O — unit-testable in isolation and shared by the resolver
(accept-time) and the repair pass (detection-time).
"""

from __future__ import annotations

import re

from nous.util.slugify import normalize_name

# Separators that split a title into "<brand> <sep> <tagline/section>".
# A real homepage title is overwhelmingly "<Brand> — <what we do>" or
# "<Brand> | <Section>".  The leading segment before the FIRST such separator is
# the page's brand claim.
_TITLE_SEPARATORS: tuple[str, ...] = (
    "—",   # em dash
    "–",   # en dash
    "|",   # pipe
    "·",   # middle dot
    "::",  # double colon
    ":",   # colon
    " - ",  # spaced hyphen (NOT a bare hyphen — "at-bay", "co-op" keep theirs)
    " • ",  # bullet
    " › ",  # breadcrumb chevrons
    " » ",
    " / ",
)

# List separators *inside* one segment: when the leading brand segment is itself
# a list ("Polymarket, Kalshi, Predict.fun"), the page enumerates venues/brands
# rather than naming a single subject.
_LIST_SPLIT_RE: re.Pattern[str] = re.compile(
    r"\s*(?:,|&|\+|/| and | vs\.? | versus )\s*",
    re.IGNORECASE,
)

# Leading boilerplate stripped before reading the brand: "Welcome to Acme",
# "Home | Acme", "Home - Acme" should read as brand "Acme", not "Welcome"/"Home".
_LEADING_BOILERPLATE_RE: re.Pattern[str] = re.compile(
    r"^(?:welcome\s+to|home|homepage|official\s+(?:site|website)|the\s+official)\b[\s:–—-]*",
    re.IGNORECASE,
)


def _leading_segment(text: str) -> str:
    """Return the part of *text* before the first title separator.

    "FrenFlow — Multi-Venue Prediction Market" → "FrenFlow"
    "Acme | Pricing"                            → "Acme"
    "Kalshi"                                    → "Kalshi"
    """
    earliest = len(text)
    for sep in _TITLE_SEPARATORS:
        idx = text.find(sep)
        if idx != -1 and idx < earliest:
            earliest = idx
    return text[:earliest].strip()


def _strip_leading_boilerplate(segment: str) -> str:
    """Drop a leading "Welcome to" / "Home" / "Official site" preamble."""
    return _LEADING_BOILERPLATE_RE.sub("", segment).strip()


def _norm_contains(haystack: str, needle_norm: str) -> bool:
    """True when *needle_norm* (already normalized) is a token-run of *haystack*.

    Both sides are reduced with :func:`normalize_name` (lowercased, corporate
    suffix stripped, non-alphanumerics removed) and compared as concatenated
    token streams.  Working on the suffix-stripped, punctuation-free form makes
    "Predict.fun" vs "predictfun" and "Acme, Inc." vs "acme" line up, and avoids
    a bare-substring hit like "ai" inside "email" (we compare token *runs*, but
    via the alnum-collapsed key the match is still substring-on-the-key — which
    is acceptable here because the caller only uses this as a *supporting* check,
    never as the sole accept signal).
    """
    if not needle_norm:
        return False
    return needle_norm in normalize_name(haystack)


def _brand_tokens(segment: str) -> list[str]:
    """Split a leading segment into its list-member brands, normalized.

    "Polymarket, Kalshi, Predict.fun" → ["polymarket", "kalshi", "predictfun"]
    "FrenFlow"                        → ["frenflow"]
    Empty / punctuation-only members are dropped.
    """
    raw_members = _LIST_SPLIT_RE.split(segment)
    out: list[str] = []
    for member in raw_members:
        key = normalize_name(member)
        if key:
            out.append(key)
    return out


def name_is_dominant_subject(strong_text: str, company_name: str) -> bool:
    """True when *company_name* is the dominant subject of *strong_text*.

    *strong_text* is a single ``<title>`` or ``<h1>`` string.  The company name
    is "dominant" when EITHER:

    1. it is contained in the **leading brand segment** (the part before the
       first title separator) AND that segment is not a multi-brand list in
       which the company is merely one of several distinct brands; or
    2. the strong text has no separator at all and is *short* (a bare brand
       heading like "Kalshi" or "Kalshi Inc"), and contains the name.

    It is NOT dominant — i.e. this returns ``False`` — when the leading segment
    is clearly a *different single brand* (FrenFlow's "FrenFlow — …" for company
    "Kalshi"), or when the company is only one entry in a brand list
    ("Polymarket, Kalshi, Predict.fun").

    Conservative by construction: only a *competing leading brand* or a
    *multi-brand list* defeats dominance.  Ordinary single-subject titles
    ("Acme — tagline", "Welcome to Acme", "Acme vs Bar", a bare "Acme") still
    return True.
    """
    name_norm = normalize_name(company_name)
    if not name_norm:
        return False

    text = strong_text.strip()
    if not text:
        return False

    segment = _strip_leading_boilerplate(_leading_segment(text))
    segment_key = normalize_name(segment)

    # The company name is the whole leading brand (or the brand carries a
    # corporate suffix that normalize_name drops): unambiguous subject.
    if segment_key and segment_key == name_norm:
        return True

    members = _brand_tokens(segment)

    # Leading segment is a single brand token.
    if len(members) <= 1:
        # Single-brand leading segment that does NOT contain the company name
        # ⇒ the page's subject is a different brand (FrenFlow vs Kalshi, or a
        # "Series V" page for AgentMail). Reject.
        #
        # Exception: a long descriptive leading segment ("Acme is the platform
        # that helps teams ship") legitimately contains the brand mid-sentence;
        # we still accept those via the containment check below. We only treat a
        # *short* (brand-shaped) leading segment as an exclusive brand claim.
        if (
            segment_key
            and not _norm_contains(segment, name_norm)
            and _is_brandlike(segment)
        ):
            return False
        # Otherwise fall through to containment.
        return _norm_contains(text, name_norm)

    # Leading segment is a *list* of ≥2 brands. The company must be the FIRST
    # listed brand to count as dominant; appearing 2nd/3rd/… means it is one of
    # several enumerated venues (the Kalshi-in-"Polymarket, Kalshi, …" case).
    return members[0] == name_norm


# Heading shaped like a brand: few words, no sentence punctuation. Used to
# decide whether a separator-less leading segment is an *exclusive brand claim*
# (so a different one defeats dominance) vs. a descriptive sentence that merely
# opens with something else.
_SENTENCE_PUNCT_RE: re.Pattern[str] = re.compile(r"[.!?;]")


def _is_brandlike(segment: str) -> bool:
    """True when *segment* looks like a bare brand/title, not a sentence.

    Brand-shaped: at most 6 whitespace-delimited words and no sentence
    punctuation. "FrenFlow", "Series V", "Acme Robotics" → True.
    "Acme is the platform that helps teams ship faster" → False (a sentence, so
    it can legitimately contain the brand later and should not be treated as an
    exclusive competing-brand claim).
    """
    if _SENTENCE_PUNCT_RE.search(segment):
        return False
    return len(segment.split()) <= 6


# ── Repair-side helper: does a description OPEN by naming a different company? ─

# Leading "<Name> <copula> …" — the opening of an LLM company blurb, e.g.
# "FrenFlow is a multi-venue prediction-market platform …",
# "Series V provides …", "Ramp offers …". The copula list is the set of verbs an
# enrichment summary uses right after the subject; requiring one keeps this from
# firing on a description that merely starts with a capitalized common word.
_DESC_OPENER_RE: re.Pattern[str] = re.compile(
    r"""
    ^\s*
    (?:the\s+)?                       # optional leading article
    (?P<name>
        [A-Z0-9][\w.\-&']*            # first brand token (Caps/digit-led)
        (?:\s+[A-Z0-9][\w.\-&']*){0,4}  # up to 4 more Caps/digit-led tokens
    )
    \s+
    (?:is|was|provides|offers|builds|makes|develops|operates|powers|
       enables|delivers|helps|lets|allows|creates|connects|gives|brings|runs)
    \b
    """,
    re.VERBOSE,
)


def description_opening_subject(description: str) -> str | None:
    """Return the subject a description *opens by naming*, or None.

    Matches the leading ``"<Name> <verb> …"`` shape of an enrichment blurb and
    returns ``<Name>`` (raw, un-normalized).  Returns None when the text does not
    open with a recognizable "subject + company-verb" pattern — in which case the
    caller must NOT infer a mismatch (absence of signal ≠ evidence of a wrong
    company).

    Examples:
        "FrenFlow is a multi-venue prediction-market platform …" → "FrenFlow"
        "Series V provides capital to founders."                 → "Series V"
        "Ramp is an all-in-one spend management platform."        → "Ramp"
        "An AI platform for teams."                               → None
        "We help engineering teams ship faster."                  → None
    """
    match = _DESC_OPENER_RE.match(description.strip())
    if match is None:
        return None
    name = match.group("name").strip()
    return name or None


def description_subject_mismatches(description: str, company_name: str) -> bool:
    """True when the description OPENS by naming a company that is clearly NOT
    *company_name*.

    The conservative repair signal: returns True only when BOTH
    (a) the description opens with a recognizable "<Subject> <company-verb> …"
        pattern (so we actually extracted a named subject), AND
    (b) that subject does not match *company_name* under
        :func:`names_refer_to_same` (normalized equality / containment).

    Returns False whenever the opener is unrecognized (no extractable subject) or
    the subject matches the company — both of which mean "no clear mismatch".
    This asymmetry is the precision guard: a correctly-matched company
    ("Ramp is an all-in-one spend management platform …") is never flagged
    because its subject *is* the company.
    """
    subject = description_opening_subject(description)
    if subject is None:
        return False
    return not names_refer_to_same(subject, company_name)


def names_refer_to_same(a: str, b: str) -> bool:
    """True when two company names plausibly denote the same entity.

    Compares the :func:`normalize_name` keys (lowercased, corporate-suffix
    stripped, alphanumerics only) for equality or *whole-key* containment.
    Containment (one key fully inside the other) absorbs the common stylistic
    gaps — "Ramp" vs "Ramp Financial", "Kalshi" vs "Kalshi Inc" — without the
    fuzzy machinery the repo reserves for pg_trgm.  Substring containment is
    safe here because both operands are already brand-length tokens (the desc
    opener captures ≤5 tokens), so accidental overlaps like "on" ⊂ "monday" do
    not arise from realistic company names.

    Empty-on-either-side returns False (we cannot assert sameness without a key).
    """
    ka = normalize_name(a)
    kb = normalize_name(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    # Whole-key containment: the shorter key appears intact within the longer
    # ("Ramp" ⊂ "Ramp Financial", "Kalshi" ⊂ "Kalshi Inc"). Require the shorter
    # key to be ≥3 chars so a 1–2 char token ("ai", "x") can't spuriously match
    # inside an unrelated longer brand.
    shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(shorter) < 3:
        return False
    return shorter in longer
