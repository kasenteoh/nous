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


def extract_visible_text(html: str) -> str:
    """Return human-readable text from HTML, with structural noise removed.

    Strips tags in STRIP_TAGS, collapses whitespace, preserves paragraph
    breaks as a single newline.
    """
    tree = HTMLParser(html)

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

    return "\n".join(result_lines).strip()


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
