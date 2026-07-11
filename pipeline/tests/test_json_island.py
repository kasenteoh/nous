"""Tests for nous.sources.vc_portfolios._json_island.

The shared balanced-delimiter walker replaced three per-adapter copies
(a16z array walker, Founders Fund object walker, Felicis payload walker).
These tests pin the tricky cases: nesting, quoted delimiters, escapes,
truncation, and the anchor-regex convention of ``find_balanced``.
"""

from __future__ import annotations

import re

from nous.sources.vc_portfolios._json_island import extract_balanced, find_balanced


def test_extracts_flat_object() -> None:
    text = 'prefix {"a": 1} suffix'
    assert extract_balanced(text, 7) == '{"a": 1}'


def test_extracts_nested_object_and_array() -> None:
    text = '{"a": {"b": [1, 2, {"c": 3}]}}'
    assert extract_balanced(text, 0) == text


def test_extracts_array_with_nested_objects() -> None:
    text = '[{"a": 1}, {"b": [2, 3]}] trailing'
    assert extract_balanced(text, 0) == '[{"a": 1}, {"b": [2, 3]}]'


def test_delimiters_inside_strings_are_ignored() -> None:
    text = '{"tag": "close}brace", "arr": "]bracket["}'
    assert extract_balanced(text, 0) == text


def test_escaped_quote_does_not_end_string() -> None:
    text = '{"q": "she said \\"}\\" loudly"}'
    assert extract_balanced(text, 0) == text


def test_unterminated_literal_returns_none() -> None:
    assert extract_balanced('{"a": {"b": 1}', 0) is None


def test_start_not_on_delimiter_returns_none() -> None:
    assert extract_balanced('x{"a": 1}', 0) is None


def test_start_out_of_range_returns_none() -> None:
    assert extract_balanced("{}", 5) is None
    assert extract_balanced("{}", -1) is None


def test_find_balanced_uses_anchor_ending_at_opener() -> None:
    text = 'var data = {"companies": [{"name": "Acme"}]};'
    anchor = re.compile(r"data\s*=\s*(\{)")
    assert find_balanced(text, anchor) == '{"companies": [{"name": "Acme"}]}'


def test_find_balanced_array_anchor() -> None:
    text = 'window.portfolio_companies = ["a", "b [x]", ["c"]]; more'
    anchor = re.compile(r"portfolio_companies\s*=\s*(\[)")
    assert find_balanced(text, anchor) == '["a", "b [x]", ["c"]]'


def test_find_balanced_no_anchor_match_returns_none() -> None:
    assert find_balanced("nothing here", re.compile(r"data\s*=\s*(\{)")) is None
