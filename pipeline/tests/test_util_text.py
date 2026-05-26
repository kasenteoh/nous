"""Unit tests for nous.util.text — no database required."""

from __future__ import annotations

import pytest

from nous.util.text import STRIP_TAGS, extract_visible_text, truncate_to_chars

# ---------------------------------------------------------------------------
# extract_visible_text
# ---------------------------------------------------------------------------


def test_strips_script_tags() -> None:
    html = "<html><body><p>Hello</p><script>alert('x')</script></body></html>"
    result = extract_visible_text(html)
    assert "alert" not in result
    assert "Hello" in result


def test_strips_style_tags() -> None:
    html = "<html><body><p>World</p><style>.foo { color: red; }</style></body></html>"
    result = extract_visible_text(html)
    assert "color" not in result
    assert "World" in result


@pytest.mark.parametrize("tag", STRIP_TAGS)
def test_strips_all_strip_tags(tag: str) -> None:
    html = f"<html><body><p>Visible</p><{tag}>Hidden content</{tag}></body></html>"
    result = extract_visible_text(html)
    assert "Hidden content" not in result
    assert "Visible" in result


def test_collapses_whitespace_within_line() -> None:
    html = "<html><body><p>Foo    bar   baz</p></body></html>"
    result = extract_visible_text(html)
    assert "Foo bar baz" in result


def test_preserves_paragraph_breaks_as_single_newline() -> None:
    html = "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
    result = extract_visible_text(html)
    # Should have a newline between the paragraphs
    assert "First paragraph." in result
    assert "Second paragraph." in result
    assert "\n" in result


def test_no_excessive_blank_lines() -> None:
    html = "<html><body><p>A</p>\n\n\n\n\n<p>B</p></body></html>"
    result = extract_visible_text(html)
    # Should not have more than 2 consecutive newlines
    assert "\n\n\n" not in result


def test_empty_html() -> None:
    result = extract_visible_text("")
    assert result == ""


def test_nav_stripped() -> None:
    html = (
        "<html><body><nav>Menu Link 1 Link 2</nav>"
        "<main><p>Content here.</p></main></body></html>"
    )
    result = extract_visible_text(html)
    assert "Menu Link 1" not in result
    assert "Content here" in result


def test_returns_stripped_result() -> None:
    html = "<html><body>   <p>   text   </p>   </body></html>"
    result = extract_visible_text(html)
    # Result should not start or end with whitespace
    assert result == result.strip()


# ---------------------------------------------------------------------------
# truncate_to_chars
# ---------------------------------------------------------------------------


def test_short_text_unchanged() -> None:
    text = "Hello world"
    assert truncate_to_chars(text, max_chars=100) == text


def test_exact_length_unchanged() -> None:
    text = "a" * 32_000
    assert truncate_to_chars(text) == text


def test_truncates_long_text() -> None:
    text = "a" * 40_000
    result = truncate_to_chars(text, max_chars=32_000)
    assert len(result) <= 32_000


def test_cuts_on_word_boundary() -> None:
    # Build a text where the 32_000-char cut falls in the middle of a word,
    # but there is a space just before it.
    word = "longlonglongword"
    # Fill up to near the boundary with complete words, then add a long word
    # that crosses the boundary.
    text = ("hello " * 5333) + word * 10  # ~32_000 chars + overflow
    result = truncate_to_chars(text, max_chars=32_000)
    assert len(result) <= 32_000
    # Result should end at a word boundary (space was the last char before cut)
    assert not result.endswith(" ")
    # The long overflow word at the boundary should be cut — result should not
    # start with it or contain odd partial words in the last position.


def test_word_boundary_near_limit() -> None:
    # Space at position 31_999, long word from 32_000 onward.
    prefix = "word " * 6399  # 6399 * 5 = 31_995 chars
    suffix = "toolongword" * 100  # definitely overflows
    text = prefix + suffix
    result = truncate_to_chars(text, max_chars=32_000)
    assert len(result) <= 32_000
    # Should have cut at the space, not in the middle of "toolongword"
    assert "toolongword" not in result


def test_default_max_chars() -> None:
    long_text = "x " * 20_000  # 40_000 chars
    result = truncate_to_chars(long_text)
    assert len(result) <= 32_000


def test_hard_fallback_when_no_word_boundary_near_limit() -> None:
    # A string with no spaces at all — should hard-truncate.
    text = "a" * 40_000
    result = truncate_to_chars(text, max_chars=32_000)
    assert len(result) == 32_000
