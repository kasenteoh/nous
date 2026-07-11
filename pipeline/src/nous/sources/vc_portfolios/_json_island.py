"""Balanced-delimiter extraction for JSON islands embedded in HTML/JS.

Several VC portfolio pages ship their company list as a JSON literal inside a
``<script>`` tag (a16z's ``window.a16z_portfolio_companies`` array, Founders
Fund's ``window.__data`` object) or scattered through a React Flight payload
(Felicis). A regex alone can't safely capture those literals — ``.*?`` stops
at the first closing delimiter, and greedy matching overshoots — so each
adapter historically carried its own copy of a "walk to the matching closing
brace/bracket" helper. This module is the single shared implementation.

The walker counts only the delimiter pair matching the opener (``{}`` or
``[]``). That is sufficient for well-formed JSON: delimiters always nest
properly, so an interleaving like ``[ { ] }`` never occurs and the other pair
can be ignored. String literals and backslash escapes are respected so braces
or brackets inside quoted values don't fool the counter.
"""

from __future__ import annotations

import re

__all__ = ["extract_balanced", "find_balanced"]

_CLOSERS: dict[str, str] = {"{": "}", "[": "]"}


def extract_balanced(text: str, start: int) -> str | None:
    """Return the balanced ``{...}`` or ``[...]`` literal beginning at ``text[start]``.

    ``text[start]`` must be ``{`` or ``[``; the matching closer is chosen
    automatically. Returns ``None`` when ``start`` is out of range, does not
    sit on an opening delimiter, or the literal is unterminated (truncated
    page). The caller decides whether ``None`` is a structural error.
    """
    if start < 0 or start >= len(text):
        return None
    opener = text[start]
    closer = _CLOSERS.get(opener)
    if closer is None:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_string:
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def find_balanced(text: str, anchor: re.Pattern[str]) -> str | None:
    """Search ``anchor`` in ``text`` and extract the literal it points at.

    Convention: the anchor's match must END at the opening delimiter — i.e.
    the last matched character is ``{`` or ``[`` (the existing adapters write
    this as a trailing ``(\\{)`` / ``(\\[)`` group). Returns ``None`` when the
    anchor doesn't match or the literal is malformed.
    """
    match = anchor.search(text)
    if not match:
        return None
    return extract_balanced(text, match.end() - 1)
