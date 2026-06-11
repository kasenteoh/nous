"""Unit tests for the competitor-candidate extraction prompt (pass 1).

Pure unit tests — no DB, no LLM call.
"""

from __future__ import annotations

import json

from nous.llm.prompts.competitor_candidates import (
    MAX_ARTICLE_CHARS,
    MAX_ARTICLES,
    CompetitorCandidates,
    TechCrunchArticle,
    build_candidates_prompt,
)


def _article(i: int, text: str = "body") -> TechCrunchArticle:
    return TechCrunchArticle(url=f"https://techcrunch.com/{i}", text=text)


def test_candidates_default_empty() -> None:
    assert CompetitorCandidates().candidates == []


def test_candidates_round_trip() -> None:
    payload = {
        "candidates": [
            {"name": "Globex", "article_url": "https://techcrunch.com/1"},
        ]
    }
    obj = CompetitorCandidates.model_validate_json(json.dumps(payload))
    assert obj.candidates[0].name == "Globex"
    assert obj.candidates[0].article_url == "https://techcrunch.com/1"


def test_build_prompt_includes_target_and_article_urls() -> None:
    prompt = build_candidates_prompt(
        target_name="Acme",
        articles=[_article(1, "Acme competes with Globex")],
    )
    assert "Acme" in prompt
    assert "https://techcrunch.com/1" in prompt
    assert "Acme competes with Globex" in prompt


def test_build_prompt_caps_articles() -> None:
    articles = [_article(i) for i in range(MAX_ARTICLES + 5)]
    prompt = build_candidates_prompt(target_name="Acme", articles=articles)
    assert f"https://techcrunch.com/{MAX_ARTICLES - 1}" in prompt
    assert f"https://techcrunch.com/{MAX_ARTICLES}" not in prompt


def test_build_prompt_truncates_article_text() -> None:
    prompt = build_candidates_prompt(
        target_name="Acme", articles=[_article(1, "z" * 20_000)]
    )
    assert "z" * MAX_ARTICLE_CHARS in prompt
    assert "z" * (MAX_ARTICLE_CHARS + 1) not in prompt
