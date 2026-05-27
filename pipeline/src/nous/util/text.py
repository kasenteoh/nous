"""HTML → cleaned visible text + token-budget truncation."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

STRIP_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "noscript",
    "iframe",
    "form",
)

# Meta tags we lift, in priority order. JS-rendered SPAs (Next.js / React /
# Svelte / etc.) often ship empty body shells but populate these for
# OpenGraph / Twitter card / Slack-preview reasons. Falling back to them
# rescues the description signal for those sites without needing a real
# headless browser.
_META_SELECTORS: tuple[tuple[str, str], ...] = (
    # (selector, attribute) — selectolax CSS + which attribute to read
    ('meta[property="og:title"]', "content"),
    ('meta[name="twitter:title"]', "content"),
    ('meta[name="description"]', "content"),
    ('meta[property="og:description"]', "content"),
    ('meta[name="twitter:description"]', "content"),
    ('meta[property="og:site_name"]', "content"),
)


def _extract_meta_signals(tree: HTMLParser) -> list[str]:
    """Pull title + description-shaped meta tags out of a parsed document.

    Returns deduped non-empty strings, preserving first-seen order.
    """
    parts: list[str] = []
    title_node = tree.css_first("title")
    if title_node:
        title_text = title_node.text(strip=True)
        if title_text:
            parts.append(title_text)
    for selector, attr in _META_SELECTORS:
        node = tree.css_first(selector)
        if node is None:
            continue
        value = (node.attributes.get(attr) or "").strip()
        if value:
            parts.append(value)
    # Dedupe while preserving order — title + og:title + twitter:title etc.
    # routinely duplicate each other.
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    return unique


def extract_visible_text(html: str) -> str:
    """Return human-readable text from HTML, with structural noise removed.

    Strips tags in STRIP_TAGS, collapses whitespace, preserves paragraph
    breaks as a single newline.

    Always prepends title + SEO meta tags (description / og:description /
    twitter:description / og:title) to the body text. They cost <500 chars
    on a normal page but rescue JS-only SPAs whose body is empty until
    hydration. The enrichment LLM then has *something* to work with even
    when the page has no statically-rendered content.
    """
    tree = HTMLParser(html)

    # Collect meta signals BEFORE we mutate the tree (STRIP_TAGS doesn't
    # remove meta, but the order keeps the code obviously safe).
    meta_parts = _extract_meta_signals(tree)

    # Remove noisy structural tags in-place.
    for tag in STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    # Extract visible text. selectolax's .text() collapses whitespace but
    # doesn't give us paragraph-aware output, so we iterate over block-level
    # elements manually.
    # Simpler approach: get all text, then normalise whitespace.
    if tree.body:
        raw = tree.body.text(strip=True, separator="\n")
    else:
        raw = tree.text(strip=True, separator="\n")

    # Collapse sequences of more than 2 newlines into 2 (preserve paragraph
    # breaks but drop excessive blank lines).
    collapsed = re.sub(r"\n{3,}", "\n\n", raw)

    # Collapse runs of spaces/tabs within a line.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in collapsed.splitlines()]

    # Drop fully-empty lines beyond the first consecutive empty one.
    result_lines: list[str] = []
    prev_empty = False
    for line in lines:
        if line == "":
            if not prev_empty:
                result_lines.append(line)
            prev_empty = True
        else:
            result_lines.append(line)
            prev_empty = False

    body_text = "\n".join(result_lines).strip()

    # Drop meta entries that already appear in the body (avoids dupes on
    # pages that render the title/tagline in the visible header).
    filtered_meta = [p for p in meta_parts if p not in body_text]

    sections: list[str] = []
    if filtered_meta:
        sections.append("\n".join(filtered_meta))
    if body_text:
        sections.append(body_text)
    return "\n\n".join(sections)


def truncate_to_chars(text: str, max_chars: int = 32_000) -> str:
    """Truncate to a rough ~8K-token budget for English prose.

    Cuts on a word boundary when possible.
    Tokenization is approximate (1 token ≈ 4 chars).
    """
    if len(text) <= max_chars:
        return text

    # Try to cut on a word boundary (space or newline) within the last 200
    # chars of the budget so we don't split a word.
    truncated = text[:max_chars]
    # Find the last whitespace character within the window.
    cut = max(truncated.rfind(" "), truncated.rfind("\n"))
    if cut > max_chars - 200:
        # Found a good word boundary close to the limit.
        return truncated[:cut].rstrip()

    # Fall back to hard truncation.
    return truncated.rstrip()
