"""Unit tests for nous.util.slugify.

No database or network access — pure string transformation logic.
"""

from __future__ import annotations

from nous.util.slugify import normalize_name, slug_with_disambiguator, slugify


class TestSlugify:
    """Tests for the slugify() function."""

    # -- Suffix stripping --

    def test_strip_inc_period(self) -> None:
        assert slugify("Acme, Inc.") == "acme"

    def test_strip_inc_no_period(self) -> None:
        assert slugify("Acme Inc") == "acme"

    def test_strip_llc(self) -> None:
        assert slugify("Foo Bar LLC") == "foo-bar"

    def test_strip_llc_period(self) -> None:
        assert slugify("Foo Bar, LLC.") == "foo-bar"

    def test_strip_corp(self) -> None:
        # "Big Corp" — Corp is stripped from the suffix, leaving "Big".
        assert slugify("Big Corp") == "big"

    def test_strip_co_period(self) -> None:
        assert slugify("Widget Co.") == "widget"

    def test_strip_ltd(self) -> None:
        assert slugify("Global Ltd") == "global"

    def test_strip_holdings(self) -> None:
        assert slugify("Acme Holdings") == "acme"

    def test_strip_corporation(self) -> None:
        assert slugify("Acme Corporation") == "acme"

    def test_strip_lp(self) -> None:
        assert slugify("Venture LP") == "venture"

    def test_strip_llp(self) -> None:
        assert slugify("Firm LLP") == "firm"

    def test_strip_multiple_words(self) -> None:
        assert slugify("Foo Bar Baz, Inc.") == "foo-bar-baz"

    # -- Hyphenation --

    def test_spaces_become_hyphens(self) -> None:
        assert slugify("Foo Bar") == "foo-bar"

    def test_consecutive_spaces_one_hyphen(self) -> None:
        assert slugify("Foo   Bar") == "foo-bar"

    def test_punctuation_becomes_hyphens(self) -> None:
        assert slugify("Foo & Bar") == "foo-bar"

    # -- Unicode normalization --

    def test_cafe(self) -> None:
        assert slugify("Café Co.") == "cafe"

    def test_accented_chars(self) -> None:
        assert slugify("Rüber AG") == "ruber-ag"

    def test_ligature_ae(self) -> None:
        # Æ decomposes to A + combining mark, drops mark → a
        result = slugify("Ærø Tech")
        assert result.startswith("r")  # depends on NFKD for Æ

    # -- Edge cases --

    def test_empty_string(self) -> None:
        assert slugify("") == ""

    def test_whitespace_only(self) -> None:
        assert slugify("   ") == ""

    def test_suffix_only(self) -> None:
        # "Inc." alone → strip suffix → empty → empty slug
        assert slugify("Inc.") == ""

    def test_already_lowercase(self) -> None:
        assert slugify("foobar") == "foobar"

    def test_numbers_preserved(self) -> None:
        assert slugify("Startup 42 Inc") == "startup-42"

    def test_no_leading_trailing_hyphens(self) -> None:
        result = slugify("  --Foo--  ")
        assert not result.startswith("-")
        assert not result.endswith("-")


class TestNormalizeName:
    """Tests for normalize_name()."""

    def test_strip_inc(self) -> None:
        assert normalize_name("Acme, Inc.") == "acme"

    def test_spaces_collapsed(self) -> None:
        assert normalize_name("Foo Bar LLC") == "foobar"

    def test_unicode(self) -> None:
        assert normalize_name("Café Co.") == "cafe"

    def test_empty(self) -> None:
        assert normalize_name("") == ""

    def test_whitespace_only(self) -> None:
        assert normalize_name("   ") == ""

    def test_collapse_whitespace(self) -> None:
        assert normalize_name("Foo   Bar  Baz") == "foobarbaz"

    def test_stylization_variants_collide(self) -> None:
        # "OpenAI", "Open AI", and "Open AI, Inc." must produce the same key
        # so cross-source dedup catches them as the same company.
        assert normalize_name("OpenAI") == "openai"
        assert normalize_name("Open AI") == "openai"
        assert normalize_name("Open AI, Inc.") == "openai"
        assert normalize_name("OPENAI, INC.") == "openai"


class TestSlugWithDisambiguator:
    """Tests for slug_with_disambiguator()."""

    def test_format(self) -> None:
        result = slug_with_disambiguator("acme", "0001234567")
        # Should be "acme-" followed by exactly 6 hex chars.
        assert result.startswith("acme-")
        suffix = result[len("acme-"):]
        assert len(suffix) == 6
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_deterministic_with_cik(self) -> None:
        cik = "0001234567"
        result1 = slug_with_disambiguator("acme", cik)
        result2 = slug_with_disambiguator("acme", cik)
        assert result1 == result2

    def test_different_ciks_different_suffixes(self) -> None:
        r1 = slug_with_disambiguator("acme", "0001111111")
        r2 = slug_with_disambiguator("acme", "0002222222")
        assert r1 != r2

    def test_none_cik_non_deterministic(self) -> None:
        # With no CIK, the function uses os.urandom — two calls should
        # almost always differ (probability of collision is 1/16^6 ≈ 0).
        r1 = slug_with_disambiguator("acme", None)
        r2 = slug_with_disambiguator("acme", None)
        # We just check the format; non-determinism is acceptable.
        for r in (r1, r2):
            assert r.startswith("acme-")
            suffix = r[len("acme-"):]
            assert len(suffix) == 6

    def test_empty_cik_uses_random(self) -> None:
        # Empty string is falsy → random suffix.
        result = slug_with_disambiguator("widget", "")
        assert result.startswith("widget-")
        assert len(result) == len("widget-") + 6

    def test_suffix_length_always_6(self) -> None:
        for cik in ["0000000001", "9999999999", "0001858523"]:
            result = slug_with_disambiguator("co", cik)
            suffix = result.split("-")[-1]
            assert len(suffix) == 6
