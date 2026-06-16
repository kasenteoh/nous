"""Integration tests for the infer-hq-country stage.

`complete_json` is monkeypatched; the fetch client is a duck-typed fake.
Requires DATABASE_URL (same gating as the other DB suites). Uses
`committed_session_factory` so the stage's per-company sessions and the
verification reads run as the CLI does (separate sessions over one connection).
"""

from __future__ import annotations

import os
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nous.db.models import Company, RawPage
from nous.llm.client import LLMRateLimitError
from nous.llm.prompts.hq_country import HqCountryJudgment
from nous.pipeline.infer_hq_country import run_infer_hq_country
from nous.sources.homepage import FetchResult, RobotsBlockedError

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)

Factory = async_sessionmaker[AsyncSession]


class FakeClient:
    """Duck-typed HomepageClient: returns canned FetchResults / raises per URL.
    Any URL not in the map raises a 404 (the common 'page does not exist')."""

    def __init__(self, responses: dict[str, FetchResult | Exception]) -> None:
        self._responses = responses

    async def fetch(self, url: str) -> FetchResult:
        r = self._responses.get(url)
        if r is None:
            raise httpx.HTTPStatusError(
                "404 Not Found",
                request=httpx.Request("GET", url),
                response=httpx.Response(404, request=httpx.Request("GET", url)),
            )
        if isinstance(r, Exception):
            raise r
        return r


def _html(url: str, body: str) -> FetchResult:
    return FetchResult(
        url=url, status_code=200,
        content=f"<html><body><p>{body}</p></body></html>",
        content_type="text/html",
    )


def _shown_null_country_company(name: str, slug_prefix: str, website: str) -> Company:
    return Company(
        name=name,
        slug=f"{slug_prefix}-{os.urandom(4).hex()}",
        normalized_name=name.lower(),
        website=website,
        description_short="Does things.",
        hq_country=None,
    )


async def test_non_us_excluded_with_source(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = "https://acme-de.example/"
    async with committed_session_factory() as s1:
        co = _shown_null_country_company("Acme DE", "infer-de", site)
        s1.add(co)
        await s1.commit()
        co_id: UUID = co.id

    client = FakeClient({
        f"{site}contact": _html(
            f"{site}contact",
            "Contact us at hello@acme-de.example. Acme GmbH, "
            "Musterstrasse 1, 10115 Berlin, Germany. All rights reserved.",
        ),
    })
    monkeypatch.setattr(
        "nous.pipeline.infer_hq_country.complete_json",
        _amock(HqCountryJudgment(hq_country="DE", evidence_quote="Berlin, Germany")),
    )

    summary = await run_infer_hq_country(committed_session_factory, client)
    assert summary.companies_checked == 1
    assert summary.excluded_non_us == 1

    async with committed_session_factory() as s3:
        row = await s3.get(Company, co_id)
    assert row is not None
    assert row.hq_country == "DE"
    assert row.exclusion_reason == "non_us"
    assert f"{site}contact" in (row.exclusion_detail or "")
    assert row.hq_country_checked_at is not None

    # Idempotent: the stamp makes a second run a no-op.
    summary2 = await run_infer_hq_country(committed_session_factory, client)
    assert summary2.companies_checked == 0


async def test_silent_site_left_unknown_and_stamped(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = "https://silent.example/"
    async with committed_session_factory() as s1:
        co = _shown_null_country_company("Silent Co", "infer-silent", site)
        s1.add(co)
        await s1.flush()
        co_id = co.id
        s1.add(RawPage(company_id=co_id, url=site,
                       content="We make great software. No address here." * 5))
        await s1.commit()

    client = FakeClient({})  # every candidate path 404s
    monkeypatch.setattr(
        "nous.pipeline.infer_hq_country.complete_json",
        _amock(HqCountryJudgment()),  # unknown
    )

    summary = await run_infer_hq_country(committed_session_factory, client)
    assert summary.left_unknown == 1
    assert summary.fetch_failures == 1

    async with committed_session_factory() as s3:
        row = await s3.get(Company, co_id)
    assert row is not None
    assert row.hq_country is None
    assert row.exclusion_reason is None
    assert row.hq_country_checked_at is not None  # stamped → not retried


async def test_unsupported_evidence_does_not_exclude(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = "https://noproof.example/"
    async with committed_session_factory() as s1:
        co = _shown_null_country_company("No Proof Co", "infer-noproof", site)
        s1.add(co)
        await s1.commit()
        co_id = co.id

    client = FakeClient({
        f"{site}about": _html(
            f"{site}about",
            "We build great software for teams around the world. "
            "Learn more about our platform on this site.",
        ),
    })
    # Model claims DE but the quote is NOT in the fetched text → must not exclude.
    monkeypatch.setattr(
        "nous.pipeline.infer_hq_country.complete_json",
        _amock(HqCountryJudgment(hq_country="DE", evidence_quote="Hamburg, Germany")),
    )

    summary = await run_infer_hq_country(committed_session_factory, client)
    assert summary.excluded_non_us == 0
    assert summary.left_unknown == 1
    async with committed_session_factory() as s3:
        row = await s3.get(Company, co_id)
    assert row is not None and row.exclusion_reason is None and row.hq_country is None


async def test_falls_back_to_stored_text_and_sources_it(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = "https://uk-stored.example/"
    stored_url = f"{site}"
    async with committed_session_factory() as s1:
        co = _shown_null_country_company("UK Stored Co", "infer-uk", site)
        s1.add(co)
        await s1.flush()
        co_id = co.id
        s1.add(RawPage(company_id=co_id, url=stored_url,
                       content="Acme Ltd is based in Manchester, United Kingdom."))
        await s1.commit()

    # All candidate fetches fail (robots / network) → only stored text remains.
    client = FakeClient({
        f"{site}about": RobotsBlockedError("blocked"),
        f"{site}contact": httpx.RequestError("boom"),
    })
    monkeypatch.setattr(
        "nous.pipeline.infer_hq_country.complete_json",
        _amock(HqCountryJudgment(hq_country="GB",
                                 evidence_quote="Manchester, United Kingdom")),
    )

    summary = await run_infer_hq_country(committed_session_factory, client)
    assert summary.excluded_non_us == 1
    assert summary.fetch_failures == 1
    async with committed_session_factory() as s3:
        row = await s3.get(Company, co_id)
    assert row is not None
    assert row.hq_country == "GB"
    assert stored_url in (row.exclusion_detail or "")  # sourced to the stored page


async def test_dry_run_writes_nothing(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = "https://dry.example/"
    async with committed_session_factory() as s1:
        co = _shown_null_country_company("Dry Co", "infer-dry", site)
        s1.add(co)
        await s1.commit()
        co_id = co.id

    client = FakeClient({
        f"{site}contact": _html(
            f"{site}contact",
            "Say hello at hello@dry.example. Dry ApS, "
            "Langebrogade 5, 1411 Copenhagen, Denmark.",
        ),
    })
    monkeypatch.setattr(
        "nous.pipeline.infer_hq_country.complete_json",
        _amock(HqCountryJudgment(hq_country="DK", evidence_quote="Copenhagen, Denmark")),
    )

    summary = await run_infer_hq_country(committed_session_factory, client, dry_run=True)
    assert summary.excluded_non_us == 1  # intent counted
    async with committed_session_factory() as s3:
        row = await s3.get(Company, co_id)
    assert row is not None
    assert row.hq_country is None
    assert row.exclusion_reason is None
    assert row.hq_country_checked_at is None  # nothing written


async def test_rate_limit_stops_the_loop(
    committed_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock
    site = "https://rl.example/"
    async with committed_session_factory() as s1:
        first = _shown_null_country_company("Aaa RL", "infer-rl", site)
        tripped = _shown_null_country_company("Bbb RL", "infer-rl", site)
        never = _shown_null_country_company("Ccc RL", "infer-rl", site)
        s1.add_all([first, tripped, never])
        await s1.commit()
        first_id, tripped_id, never_id = first.id, tripped.id, never.id

    client = FakeClient({})  # all 404 → stored-less, unknown
    mock = AsyncMock(side_effect=[
        HqCountryJudgment(),                    # Aaa: processed (unknown)
        LLMRateLimitError("429 daily quota"),   # Bbb: break
    ])
    monkeypatch.setattr("nous.pipeline.infer_hq_country.complete_json", mock)

    summary = await run_infer_hq_country(committed_session_factory, client)
    assert summary.companies_checked == 1
    assert summary.skipped_rate_limited == 1
    assert mock.await_count == 2  # Ccc never reached

    async with committed_session_factory() as s3:
        a, b, c = (await s3.get(Company, first_id),
                   await s3.get(Company, tripped_id),
                   await s3.get(Company, never_id))
    assert a is not None and a.hq_country_checked_at is not None
    assert b is not None and b.hq_country_checked_at is None
    assert c is not None and c.hq_country_checked_at is None


def _amock(return_value: object):
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=return_value)
