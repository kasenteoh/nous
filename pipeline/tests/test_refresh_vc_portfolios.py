"""Integration tests for the refresh-vc-portfolios pipeline stage.

Requires DATABASE_URL pointing at a Postgres with pg_trgm + the M3
migrations applied. Tests are skipped when DATABASE_URL is unset.

The real VC adapters are NEVER called — we monkey-patch
``nous.pipeline.refresh_vc_portfolios.ADAPTERS`` with a small registry of
fake adapter classes that return canned :class:`PortfolioEntry` lists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.pipeline import refresh_vc_portfolios as stage
from nous.pipeline.refresh_vc_portfolios import (
    PortfolioEntry,
    run_refresh_vc_portfolios,
)
from nous.sources.homepage import HomepageClient
from nous.util.slugify import normalize_name

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeAdapter:
    """Test-only stand-in for a PortfolioAdapter.

    Each instance returns a fixed entry list; setting ``raises`` to an
    exception swaps the fetch path for an unconditional raise so we can
    verify adapter-failure isolation.
    """

    firm: str
    entries: list[PortfolioEntry]
    raises: Exception | None = None

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        if self.raises is not None:
            raise self.raises
        return self.entries


class StubHomepageClient:
    """A no-op HomepageClient stand-in.

    The fake adapters never touch ``client`` so we don't need any of the
    real client's machinery. Typed as ``HomepageClient`` at the call site
    via a cast; the adapter Protocol only requires ``fetch`` accept *some*
    object and our fakes ignore it entirely.
    """

    async def __aenter__(self) -> StubHomepageClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _stub_client() -> HomepageClient:
    """Return a StubHomepageClient typed as HomepageClient for the call site.

    The adapter Protocol declares ``fetch(client: HomepageClient)`` but our
    fakes ignore the argument entirely, so a structural stub is safe.
    """
    return StubHomepageClient()  # type: ignore[return-value]


def _entry(
    firm: str, name: str, *, website: str | None = "https://example.com"
) -> PortfolioEntry:
    return PortfolioEntry(
        firm=firm,
        name=name,
        website=website,
        description=None,
        source_url=f"https://{firm}.example.com/portfolio",
    )


def _install_fake_adapters(
    monkeypatch: pytest.MonkeyPatch, adapters: dict[str, FakeAdapter]
) -> None:
    """Swap the module-level ADAPTERS registry for our fakes."""
    monkeypatch.setattr(stage, "ADAPTERS", adapters, raising=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_all_adapters_succeed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three healthy adapters → firms_run==3, entries summed, all created."""
    suffix = os.urandom(3).hex()
    fakes = {
        "fakeyc": FakeAdapter(
            firm="fakeyc",
            entries=[
                _entry("fakeyc", f"YC Newco One {suffix}"),
                _entry("fakeyc", f"YC Newco Two {suffix}"),
            ],
        ),
        "fakea16z": FakeAdapter(
            firm="fakea16z",
            entries=[
                _entry("fakea16z", f"A16z Newco {suffix}"),
            ],
        ),
        "fakesequoia": FakeAdapter(
            firm="fakesequoia",
            entries=[
                _entry("fakesequoia", f"Sequoia Newco One {suffix}"),
                _entry("fakesequoia", f"Sequoia Newco Two {suffix}"),
                _entry("fakesequoia", f"Sequoia Newco Three {suffix}"),
            ],
        ),
    }
    _install_fake_adapters(monkeypatch, fakes)

    summary = await run_refresh_vc_portfolios(db, _stub_client())

    assert summary.firms_run == 3
    assert summary.entries_seen == 6
    assert summary.companies_created == 6
    assert summary.companies_matched == 0
    assert summary.adapter_failures == {}


async def test_one_adapter_failure_does_not_block_others(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken adapter is recorded and skipped; healthy ones still run."""
    suffix = os.urandom(3).hex()
    fakes = {
        "broken": FakeAdapter(
            firm="broken",
            entries=[],
            raises=RuntimeError("portfolio page returned 500"),
        ),
        "healthy": FakeAdapter(
            firm="healthy",
            entries=[_entry("healthy", f"Healthy Co {suffix}")],
        ),
    }
    _install_fake_adapters(monkeypatch, fakes)

    summary = await run_refresh_vc_portfolios(db, _stub_client())

    # Both adapters were attempted, so firms_run includes the broken one.
    assert summary.firms_run == 2
    # Only the healthy adapter produced entries.
    assert summary.entries_seen == 1
    assert summary.companies_created == 1
    # The broken firm shows up in the failures map with the error repr.
    assert "broken" in summary.adapter_failures
    assert "500" in summary.adapter_failures["broken"]
    # The healthy firm is NOT in failures.
    assert "healthy" not in summary.adapter_failures


async def test_rerun_is_idempotent(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second run matches the first run's inserts; nothing new is created."""
    suffix = os.urandom(3).hex()
    # Use trigram-dissimilar base names. "Idempotent Co A" vs "...Co B" share
    # too many trigrams (>0.85) and the fuzzy-match path collapses them into
    # one row. Distinct base names defeat that.
    fakes = {
        "idemfirm": FakeAdapter(
            firm="idemfirm",
            entries=[
                _entry("idemfirm", f"Aurora Health {suffix}"),
                _entry("idemfirm", f"Pinecone Robotics {suffix}"),
            ],
        ),
    }
    _install_fake_adapters(monkeypatch, fakes)

    first = await run_refresh_vc_portfolios(db, _stub_client())
    assert first.companies_created == 2
    assert first.companies_matched == 0

    second = await run_refresh_vc_portfolios(db, _stub_client())
    # Same entries → everything matches existing rows; no new inserts.
    assert second.entries_seen == 2
    assert second.companies_created == 0
    assert second.companies_matched == 2


async def test_form_d_company_match_preserves_discovered_via(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A VC entry matching a Form-D row returns it WITHOUT changing
    discovered_via — first-discovery wins (per the M3 plan)."""
    suffix = os.urandom(3).hex()
    # Seed a pre-existing Form-D company.
    name = f"FormD Originated Co {suffix}"
    seeded = Company(
        name=name,
        slug=f"formd-originated-{suffix}",
        normalized_name=normalize_name(name),
        hq_country="US",
        discovered_via="form_d",
    )
    db.add(seeded)
    await db.flush()
    await db.commit()
    seeded_id = seeded.id

    fakes = {
        "vcfirm": FakeAdapter(
            firm="vcfirm",
            entries=[_entry("vcfirm", name, website="https://formdco.example/")],
        ),
    }
    _install_fake_adapters(monkeypatch, fakes)

    summary = await run_refresh_vc_portfolios(db, _stub_client())

    assert summary.entries_seen == 1
    assert summary.companies_matched == 1
    assert summary.companies_created == 0

    # Reload and confirm: same row, discovered_via untouched, website backfilled.
    result = await db.execute(select(Company).where(Company.id == seeded_id))
    row = result.scalar_one()
    assert row.discovered_via == "form_d"
    assert row.website == "https://formdco.example/"


async def test_firms_arg_filters_to_subset(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing firms=['a'] runs only adapter 'a' even if 'b' is registered."""
    suffix = os.urandom(3).hex()
    fakes = {
        "alpha": FakeAdapter(
            firm="alpha",
            entries=[_entry("alpha", f"Alpha Co {suffix}")],
        ),
        "beta": FakeAdapter(
            firm="beta",
            entries=[_entry("beta", f"Beta Co {suffix}")],
        ),
    }
    _install_fake_adapters(monkeypatch, fakes)

    summary = await run_refresh_vc_portfolios(db, _stub_client(), firms=["alpha"])

    assert summary.firms_run == 1
    assert summary.entries_seen == 1
    assert summary.companies_created == 1


async def test_unknown_firm_is_recorded_as_failure(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown firm slug is reported in adapter_failures, not crashed."""
    fakes: dict[str, FakeAdapter] = {}
    _install_fake_adapters(monkeypatch, fakes)

    summary = await run_refresh_vc_portfolios(db, _stub_client(), firms=["nope"])

    assert summary.firms_run == 0
    assert summary.adapter_failures.get("nope") == "unknown firm"
