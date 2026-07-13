"""Unit tests for news-outbound-link mining (no network).

Covers link extraction from article HTML and the precision selection: a
domain-label match wins even with unhelpful anchor text, an anchor-name match is
the fallback, and the publisher's own domain / aggregators / already-rejected
domains / too-short-name substrings are never picked.
"""

from __future__ import annotations

from nous.sources.article_links import extract_outbound_links, select_company_link


def test_extract_resolves_and_filters_hrefs() -> None:
    html = """
    <article>
      <a href="https://perplexity.ai">Perplexity</a>
      <a href="/section/tech">Tech</a>
      <a href="mailto:tips@news.com">email</a>
      <a href="#top">top</a>
      <a href="javascript:void(0)">x</a>
      <a href="ftp://files.example.com/x">ftp</a>
      <a href="https://twitter.com/reporter">@reporter</a>
    </article>
    """
    links = extract_outbound_links(html, "https://news.com/story")
    urls = [u for u, _ in links]
    assert "https://perplexity.ai" in urls
    # relative resolved against base
    assert "https://news.com/section/tech" in urls
    assert "https://twitter.com/reporter" in urls
    # non-http and empty-ish schemes dropped
    assert not any(u.startswith(("mailto:", "javascript:", "ftp:")) for u in urls)
    assert not any(u.endswith("#top") for u in urls)


def test_domain_label_match_wins_over_weak_anchor() -> None:
    links = [("https://perplexity.ai/", "read more")]
    got = select_company_link(
        links, "Perplexity", publisher_host="techcrunch.com"
    )
    assert got == "https://perplexity.ai/"


def test_anchor_match_is_the_fallback() -> None:
    """Domain doesn't spell the name, but the anchor text does."""
    links = [("https://productsite.io/home", "Acme")]
    got = select_company_link(links, "Acme", publisher_host="techcrunch.com")
    assert got == "https://productsite.io/"


def test_publisher_self_link_excluded() -> None:
    links = [
        ("https://techcrunch.com/tag/acme", "Acme"),
        ("https://blog.techcrunch.com/x", "Acme"),
    ]
    assert (
        select_company_link(links, "Acme", publisher_host="techcrunch.com") is None
    )


def test_aggregator_excluded() -> None:
    links = [("https://www.linkedin.com/company/acme", "Acme")]
    assert (
        select_company_link(links, "Acme", publisher_host="techcrunch.com") is None
    )


def test_rejected_domain_excluded() -> None:
    links = [("https://acme.com/", "Acme")]
    got = select_company_link(
        links,
        "Acme",
        publisher_host="techcrunch.com",
        rejected_domains=frozenset({"acme.com"}),
    )
    assert got is None


def test_short_name_does_not_domain_match_substring() -> None:
    """A 3-char company name must not match "box" inside "dropbox.com"."""
    links = [("https://dropbox.com/", "cloud storage")]
    assert select_company_link(links, "Box", publisher_host="news.com") is None


def test_no_candidate_returns_none() -> None:
    links = [("https://unrelated.com/", "some other company")]
    assert select_company_link(links, "Acme", publisher_host="news.com") is None
