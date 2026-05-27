# Milestone 4 — Competitor Analysis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the M4 competitor-analysis surface — a monthly pipeline stage that uses Gemini to rank up to 6 competitors per enriched company, plus a competitors section on the company detail page.

**Architecture:** New `analyze-competitors` Click command runs a pipeline stage that selects companies with `description_long` AND `industry_group` populated and either no competitors row yet or competitors older than 25 days, calls Gemini with the target + a peer list of up to 50 same-industry companies, and replaces the company's rows in a new `competitors` table. The cron lives in the existing monthly workflow (renamed). The web page gains a `<Competitors />` server component that renders cards, linking when the competitor resolves to an indexed company.

**Tech Stack:** Python 3.11, SQLAlchemy 2.x async, Alembic, Pydantic v2, Click, pytest, Postgres 15, Next.js 16 App Router server components, TypeScript strict, Tailwind v4, Supabase JS client (server-only).

**Spec reference:** [`docs/superpowers/specs/2026-05-26-milestone-4-competitor-analysis-design.md`](../specs/2026-05-26-milestone-4-competitor-analysis-design.md)

---

## Parallel-agent execution map

Tasks listed in `[brackets]` after a task heading list its dependencies. Tasks with no deps can start immediately.

```
Independent (run in parallel):
  Task 1  (DB layer)         []
  Task 2  (LLM prompt)       []
  Task 9  (Web types/query)  []   ← only depends on the DB column names already specified

Serial after Task 1 + Task 2:
  Task 3 → Task 4 → Task 5 → Task 6 → Task 7   (stage internals + tests)

Serial after Task 6:
  Task 8  (CLI)
  Task 12 (CI workflow rename + new step)

Serial after Task 9:
  Task 10 (Competitors component)
  Task 11 (page.tsx integration)

Final gate:
  Task 13 (E2E smoke + verification)   [all above]
```

When dispatching subagents, run Task 1, Task 2, and Task 9 in a single parallel batch. Then Task 3. Then Task 4. And so on.

---

## Working agreements

- Every task ends with a commit on a feature branch named `m4/<short-slug>`. Open one PR per task for incremental review.
- Tests gate on `DATABASE_URL` via the existing `pytestmark` pattern (see `pipeline/tests/conftest.py` and the top of `pipeline/tests/test_extract_funding.py`). The `db` fixture from `conftest.py` is reused everywhere.
- Mock `complete_json` via `monkeypatch.setattr("nous.pipeline.analyze_competitors.complete_json", _fake)` — mirrors `test_extract_funding.py`.
- After **every** Python task, run from `pipeline/`: `uv run ruff check . && uv run mypy src && uv run pytest -q`. After every web task, run from `web/`: `npm run build`.
- Never commit secrets. The new stage uses the existing `LLM_PROVIDER` / `GEMINI_API_KEY` plumbing — no new env vars.

---

## Task 1 — Database layer: Competitor model + migration

**Branch:** `m4/db-competitors-table`

**Files:**
- Modify: `pipeline/src/nous/db/models.py` (append `Competitor` after `FundingRoundInvestor` around line 220+)
- Create: `pipeline/alembic/versions/0004_m4_competitors.py`
- Create: `pipeline/tests/test_competitors_schema.py`

### Step 1.1 — Add the `Competitor` SQLAlchemy model

- [ ] Append to `pipeline/src/nous/db/models.py` (after the existing `class FundingRoundInvestor(Base):` block):

```python
class Competitor(Base):
    """Ranked competitor entry for a company, produced by the M4 analyze-competitors stage.

    Replace-style writes: each monthly run for a company DELETEs existing rows
    for that company_id then INSERTs the new ranked set inside one transaction.
    """

    __tablename__ = "competitors"

    company_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: many competitors won't match a row in our DB. Resolved via
    # exact normalized_name lookup in the stage.
    competitor_company_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    competitor_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(String, nullable=True)
    rank: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "rank", name="uq_competitors_company_rank"),
    )
```

### Step 1.2 — Generate the Alembic migration

- [ ] Run from `pipeline/`:

```bash
uv run alembic revision --autogenerate -m "m4 competitors table"
```

- [ ] Open the newly-generated file under `pipeline/alembic/versions/`. **Rename it** to `0004_m4_competitors.py`. Inside the file, set `revision: str = "0004"` and `down_revision: str | None = "0003"`. Replace the autogenerated body with the deterministic version below — `--autogenerate` sometimes mis-orders index creation:

```python
"""M4 schema: competitors table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26 22:00:00.000000

Adds the M4 surface:
- competitors table (one row per (company, ranked competitor))
- UNIQUE (company_id, rank) so the replace-style write can't violate ordering
- indexes on company_id (primary access path) and competitor_company_id
  (future reverse-lookup view)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "competitors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "competitor_company_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("competitor_name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("reasoning", sa.String(), nullable=True),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
            name="fk_competitors_company_id",
        ),
        sa.ForeignKeyConstraint(
            ["competitor_company_id"],
            ["companies.id"],
            ondelete="SET NULL",
            name="fk_competitors_competitor_company_id",
        ),
        sa.UniqueConstraint(
            "company_id", "rank", name="uq_competitors_company_rank"
        ),
    )
    op.create_index("ix_competitors_company_id", "competitors", ["company_id"])
    op.create_index(
        "ix_competitors_competitor_company_id",
        "competitors",
        ["competitor_company_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_competitors_competitor_company_id", table_name="competitors")
    op.drop_index("ix_competitors_company_id", table_name="competitors")
    op.drop_table("competitors")
```

### Step 1.3 — Hand-review and apply

- [ ] Run from `pipeline/`:

```bash
uv run alembic upgrade head
```

Expected: no errors; `0004` applied.

### Step 1.4 — Write the round-trip test

- [ ] Create `pipeline/tests/test_competitors_schema.py`:

```python
"""Round-trip coverage for the M4 competitors table."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _make_company(name: str) -> Company:
    return Company(
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{os.urandom(3).hex()}",
        normalized_name=name.lower(),
        hq_country="US",
    )


async def test_competitor_row_with_resolved_link(db: AsyncSession) -> None:
    target = _make_company("Acme")
    rival = _make_company("Beta Co")
    db.add_all([target, rival])
    await db.flush()

    row = Competitor(
        company_id=target.id,
        competitor_company_id=rival.id,
        competitor_name="Beta Co",
        description="Direct rival in same market.",
        reasoning="Both target SMB ops teams.",
        rank=1,
    )
    db.add(row)
    await db.flush()

    fetched = await db.get(Competitor, row.id)
    assert fetched is not None
    assert fetched.company_id == target.id
    assert fetched.competitor_company_id == rival.id
    assert fetched.rank == 1


async def test_competitor_row_unlinked(db: AsyncSession) -> None:
    target = _make_company("Acme")
    db.add(target)
    await db.flush()

    row = Competitor(
        company_id=target.id,
        competitor_company_id=None,
        competitor_name="UnknownCo",
        description="Not in our DB.",
        reasoning="Mentioned by the LLM.",
        rank=2,
    )
    db.add(row)
    await db.flush()

    stmt = select(Competitor).where(Competitor.company_id == target.id)
    rows = (await db.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].competitor_company_id is None
    assert rows[0].competitor_name == "UnknownCo"


async def test_unique_company_rank_constraint(db: AsyncSession) -> None:
    target = _make_company("Acme")
    db.add(target)
    await db.flush()

    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="A",
            rank=1,
        )
    )
    await db.flush()

    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="B",
            rank=1,
        )
    )
    with pytest.raises(Exception):  # IntegrityError from unique constraint
        await db.flush()
```

### Step 1.5 — Run tests, confirm pass

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_competitors_schema.py -v
```

Expected: 3 passed.

### Step 1.6 — Lint, typecheck, full test sweep

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src && uv run pytest -q
```

Expected: all green.

### Step 1.7 — Commit

```bash
git checkout -b m4/db-competitors-table
git add pipeline/src/nous/db/models.py pipeline/alembic/versions/0004_m4_competitors.py pipeline/tests/test_competitors_schema.py
git commit -m "$(cat <<'EOF'
feat(m4,db): competitors table + Competitor model + round-trip tests

Implements spec §4.6 — one row per ranked competitor with FK to the
target company (CASCADE) and a nullable FK to the resolved competitor
company (SET NULL). Unique (company_id, rank) backs the replace-style
write the analyze-competitors stage will perform.
EOF
)"
git push -u origin m4/db-competitors-table
gh pr create --title "feat(m4,db): competitors table + model + round-trip tests" \
  --body "M4 Task 1. Adds the competitors table per spec §4.6 and the Competitor SQLAlchemy model. Round-trip coverage in pipeline/tests/test_competitors_schema.py."
```

---

## Task 2 — LLM prompt: schema + builder + unit tests

**Branch:** `m4/llm-competitor-prompt`

**Files:**
- Create: `pipeline/src/nous/llm/prompts/competitor_analysis.py`
- Create: `pipeline/tests/test_competitor_analysis_prompt.py`

### Step 2.1 — Write the failing tests

- [ ] Create `pipeline/tests/test_competitor_analysis_prompt.py`:

```python
"""Unit tests for the M4 competitor-analysis prompt module.

Pure unit tests — no DB, no LLM call. Validates the Pydantic schema and the
prompt builder's structural contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nous.llm.prompts.competitor_analysis import (
    MAX_PEERS,
    Competitor,
    CompetitorAnalysis,
    Peer,
    Target,
    build_prompt,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_empty_competitors_list_is_valid() -> None:
    assert CompetitorAnalysis(competitors=[]).competitors == []


def test_single_competitor_with_rank_1_is_valid() -> None:
    ca = CompetitorAnalysis(
        competitors=[
            Competitor(
                name="Beta",
                description="A rival.",
                reasoning="Same market.",
                rank=1,
            )
        ]
    )
    assert len(ca.competitors) == 1


def test_six_competitors_with_consecutive_ranks_is_valid() -> None:
    ca = CompetitorAnalysis(
        competitors=[
            Competitor(name=f"C{i}", description="d", reasoning="r", rank=i)
            for i in range(1, 7)
        ]
    )
    assert [c.rank for c in ca.competitors] == [1, 2, 3, 4, 5, 6]


def test_more_than_six_competitors_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name=f"C{i}", description="d", reasoning="r", rank=i)
                for i in range(1, 8)
            ]
        )


def test_duplicate_ranks_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name="A", description="d", reasoning="r", rank=1),
                Competitor(name="B", description="d", reasoning="r", rank=1),
            ]
        )


def test_gap_in_ranks_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name="A", description="d", reasoning="r", rank=1),
                Competitor(name="B", description="d", reasoning="r", rank=3),
            ]
        )


def test_rank_starting_above_one_rejected() -> None:
    with pytest.raises(ValidationError):
        CompetitorAnalysis(
            competitors=[
                Competitor(name="A", description="d", reasoning="r", rank=2),
            ]
        )


def test_empty_name_rejected() -> None:
    with pytest.raises(ValidationError):
        Competitor(name="", description="d", reasoning="r", rank=1)


def test_rank_above_six_rejected() -> None:
    with pytest.raises(ValidationError):
        Competitor(name="A", description="d", reasoning="r", rank=7)


def test_rank_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        Competitor(name="A", description="d", reasoning="r", rank=0)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _target() -> Target:
    return Target(
        name="Acme",
        description_short="Acme makes widgets.",
        description_long="Acme is a B2B SaaS for widget logistics.",
        industry_group="SaaS",
    )


def _peer(i: int) -> Peer:
    return Peer(name=f"Peer{i}", description_short=f"Does thing {i}.")


def test_build_prompt_includes_target_fields() -> None:
    prompt = build_prompt(target=_target(), peers=[])
    assert "Acme" in prompt
    assert "Acme makes widgets." in prompt
    assert "Acme is a B2B SaaS for widget logistics." in prompt
    assert "SaaS" in prompt


def test_build_prompt_includes_each_peer() -> None:
    peers = [_peer(i) for i in range(3)]
    prompt = build_prompt(target=_target(), peers=peers)
    for i in range(3):
        assert f"Peer{i}" in prompt
        assert f"Does thing {i}." in prompt


def test_build_prompt_empty_peer_list_renders_without_error() -> None:
    prompt = build_prompt(target=_target(), peers=[])
    assert isinstance(prompt, str)
    assert len(prompt) > 0


def test_build_prompt_caps_peers_at_max() -> None:
    too_many = [_peer(i) for i in range(MAX_PEERS + 20)]
    prompt = build_prompt(target=_target(), peers=too_many)
    # Last peer that should appear is index MAX_PEERS - 1; anything beyond drops.
    assert f"Peer{MAX_PEERS - 1}" in prompt
    assert f"Peer{MAX_PEERS}" not in prompt


def test_build_prompt_forbids_fabrication_language() -> None:
    """Per spec §11 / CLAUDE.md: prompts must instruct null/empty over fabrication."""
    prompt = build_prompt(target=_target(), peers=[])
    lowered = prompt.lower()
    assert "do not invent" in lowered or "do not fabricate" in lowered
    assert "empty list" in lowered or "return an empty" in lowered
```

### Step 2.2 — Run tests, confirm they fail

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_competitor_analysis_prompt.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nous.llm.prompts.competitor_analysis'`.

### Step 2.3 — Implement the prompt module

- [ ] Create `pipeline/src/nous/llm/prompts/competitor_analysis.py`:

```python
"""Competitor-analysis prompt per spec §6.3 (M4).

Input: a target company (name + descriptions + industry_group) and a peer list
of up to 50 same-industry companies (name + short description). Output: a
Pydantic model holding up to 6 ranked competitors with descriptions and
reasoning.

Per CLAUDE.md ("prompts must instruct the model to return null or empty rather
than fabricate"), the template tells Gemini to return an empty list rather
than invent competitors.

This module is a drop-in user of `nous.llm.client.complete_json`. The caller
(analyze-competitors stage) imports `build_prompt` and `CompetitorAnalysis`
and hands them to `complete_json`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

MAX_PEERS = 50
MAX_COMPETITORS = 6


class Target(BaseModel):
    name: str
    description_short: str
    description_long: str
    industry_group: str


class Peer(BaseModel):
    name: str
    description_short: str


class Competitor(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    reasoning: str = Field(..., min_length=1)
    rank: int = Field(..., ge=1, le=MAX_COMPETITORS)


class CompetitorAnalysis(BaseModel):
    competitors: list[Competitor] = Field(
        default_factory=list, max_length=MAX_COMPETITORS
    )

    @model_validator(mode="after")
    def _ranks_must_be_one_through_n(self) -> "CompetitorAnalysis":
        ranks = [c.rank for c in self.competitors]
        expected = list(range(1, len(ranks) + 1))
        if ranks != expected:
            raise ValueError(
                f"ranks must be 1..N with no gaps or duplicates; got {ranks}"
            )
        return self


PROMPT_TEMPLATE = """\
You are identifying competitors for a software company.

Target company:
- Name: {name}
- Industry: {industry_group}
- Short description: {description_short}
- Long description:
{description_long}

Peer list (other companies indexed in our database in the same industry):
{peer_block}

Task:
- Identify up to {max_competitors} companies that compete with the target.
- Prefer companies from the peer list when reasonable.
- You may also name well-known competitors that are not in the peer list.
- Do not invent fictional companies. If you have no high-confidence competitors,
  return an empty list rather than fabricate.
- Rank them 1..N, where 1 is the most direct competitor. Ranks must be
  consecutive integers starting at 1 with no gaps or duplicates.
- For each competitor, write a 1–2 sentence description and a short
  reasoning explaining why they compete with the target.

Return JSON matching the schema.
"""


def _render_peer_block(peers: list[Peer]) -> str:
    if not peers:
        return "(no peers available in our database)"
    lines = [f"- {p.name}: {p.description_short}" for p in peers]
    return "\n".join(lines)


def build_prompt(*, target: Target, peers: list[Peer]) -> str:
    """Render the competitor-analysis prompt with the given target and peer list.

    The peer list is truncated to MAX_PEERS to keep token cost predictable.
    """
    capped_peers = peers[:MAX_PEERS]
    return PROMPT_TEMPLATE.format(
        name=target.name,
        industry_group=target.industry_group,
        description_short=target.description_short,
        description_long=target.description_long,
        peer_block=_render_peer_block(capped_peers),
        max_competitors=MAX_COMPETITORS,
    )
```

### Step 2.4 — Run tests, confirm pass

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_competitor_analysis_prompt.py -v
```

Expected: 15 passed.

### Step 2.5 — Lint, typecheck

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src
```

Expected: all green.

### Step 2.6 — Commit

```bash
git checkout -b m4/llm-competitor-prompt
git add pipeline/src/nous/llm/prompts/competitor_analysis.py pipeline/tests/test_competitor_analysis_prompt.py
git commit -m "$(cat <<'EOF'
feat(m4,llm): competitor-analysis prompt + Pydantic schema

Implements spec §6.3. CompetitorAnalysis validates ranks 1..N with no
gaps/duplicates and caps at MAX_COMPETITORS (6). Prompt template
instructs Gemini to prefer peers in our DB, never invent fictional
companies, and return an empty list rather than fabricate.
EOF
)"
git push -u origin m4/llm-competitor-prompt
gh pr create --title "feat(m4,llm): competitor-analysis prompt + schema" \
  --body "M4 Task 2. Pure unit tests cover schema validation and prompt structure."
```

---

## Task 3 — Stage scaffolding: module skeleton + summary + eligibility query

**Branch:** `m4/stage-scaffold`
**Depends on:** Task 1, Task 2

**Files:**
- Create: `pipeline/src/nous/pipeline/analyze_competitors.py`
- Create: `pipeline/tests/test_analyze_competitors_stage.py`

### Step 3.1 — Write the failing tests for the eligibility query

- [ ] Create `pipeline/tests/test_analyze_competitors_stage.py`:

```python
"""Tests for the analyze-competitors stage.

DB-gated integration tests covering:
- Eligibility query (description_long + industry_group required; TTL gate).
- Peer-list query (50-cap, same industry_group, target excluded, recency order).
- Competitor name resolution (exact normalized_name match; otherwise null).
- Replace-style write inside one transaction.
- Main loop happy path with a mocked LLM.
- Rate-limit, parse-error, TTL-gate, dry-run behaviors.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor
from nous.llm.client import LLMParseError, LLMRateLimitError
from nous.llm.prompts.competitor_analysis import (
    CompetitorAnalysis,
    Competitor as CompetitorOut,
)
from nous.pipeline.analyze_competitors import (
    AnalyzeCompetitorsSummary,
    fetch_eligible_companies,
    fetch_peers,
    resolve_competitor_company_id,
    run_analyze_competitors,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping DB integration tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(
    name: str,
    *,
    description_long: str | None = "Long desc",
    industry_group: str | None = "SaaS",
) -> Company:
    return Company(
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{os.urandom(3).hex()}",
        normalized_name=name.lower(),
        description_short=f"{name} short.",
        description_long=description_long,
        industry_group=industry_group,
        hq_country="US",
    )


# ---------------------------------------------------------------------------
# Eligibility query
# ---------------------------------------------------------------------------


async def test_eligible_requires_description_long(db: AsyncSession) -> None:
    yes = _make_company("Yes")
    no = _make_company("No", description_long=None)
    db.add_all([yes, no])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=10, ttl_days=25)
    ids = {c.id for c in eligible}
    assert yes.id in ids
    assert no.id not in ids


async def test_eligible_requires_industry_group(db: AsyncSession) -> None:
    yes = _make_company("Yes")
    no = _make_company("No", industry_group=None)
    db.add_all([yes, no])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=10, ttl_days=25)
    ids = {c.id for c in eligible}
    assert yes.id in ids
    assert no.id not in ids


async def test_eligible_skips_recently_analyzed(db: AsyncSession) -> None:
    fresh = _make_company("Fresh")
    stale = _make_company("Stale")
    db.add_all([fresh, stale])
    await db.flush()

    # Fresh has a competitor updated 5 days ago — gated out by 25-day TTL.
    fresh_recent = Competitor(
        company_id=fresh.id,
        competitor_name="X",
        rank=1,
        updated_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    # Stale has a competitor updated 40 days ago — eligible again.
    stale_old = Competitor(
        company_id=stale.id,
        competitor_name="Y",
        rank=1,
        updated_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    db.add_all([fresh_recent, stale_old])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=10, ttl_days=25)
    ids = {c.id for c in eligible}
    assert stale.id in ids
    assert fresh.id not in ids


async def test_eligible_respects_limit(db: AsyncSession) -> None:
    db.add_all([_make_company(f"Co{i}") for i in range(5)])
    await db.flush()

    eligible = await fetch_eligible_companies(db, limit=2, ttl_days=25)
    assert len(eligible) == 2
```

### Step 3.2 — Run tests, confirm they fail

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py::test_eligible_requires_description_long -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nous.pipeline.analyze_competitors'`.

### Step 3.3 — Create the stage module scaffolding

- [ ] Create `pipeline/src/nous/pipeline/analyze_competitors.py`:

```python
"""analyze-competitors pipeline stage (M4).

For each enriched, industry-classified company with no recent competitors
analysis, call Gemini with the target description + a peer list of up to 50
same-industry companies, and write the ranked competitor set to the
`competitors` table.

Idempotency:
- Replace-style writes: each run for a company DELETEs existing rows for that
  company_id then INSERTs the new ranked set in one transaction.
- TTL gate (default 25 days): a company is re-analyzed only when no rows exist
  or when MAX(updated_at) is older than the TTL.

Quota discipline (spec §11):
- Hard cap on companies processed per run (default 500 = monthly Gemini budget;
  Gemini 2.5 Flash free tier = 1500 RPD).
- On LLMRateLimitError, stop the loop immediately — same pattern as
  extract-funding.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, Competitor
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.competitor_analysis import (
    MAX_PEERS,
    CompetitorAnalysis,
    Peer,
    Target,
    build_prompt,
)

logger = logging.getLogger(__name__)


class AnalyzeCompetitorsSummary(BaseModel):
    companies_analyzed: int = 0
    competitors_written: int = 0
    competitors_linked: int = 0
    competitors_unlinked: int = 0
    llm_failures: int = 0
    skipped_rate_limited: int = 0


# ---------------------------------------------------------------------------
# Eligibility query
# ---------------------------------------------------------------------------


async def fetch_eligible_companies(
    session: AsyncSession,
    *,
    limit: int,
    ttl_days: int,
) -> list[Company]:
    """Return companies eligible for competitor analysis.

    A company is eligible when:
    - description_long IS NOT NULL
    - industry_group IS NOT NULL
    - No competitors row exists for it, OR MAX(competitors.updated_at) is older
      than `ttl_days` days ago.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

    # Subquery: most-recent competitors.updated_at per company_id.
    last_analyzed = (
        select(
            Competitor.company_id,
            func.max(Competitor.updated_at).label("last_analyzed_at"),
        )
        .group_by(Competitor.company_id)
        .subquery()
    )

    stmt = (
        select(Company)
        .outerjoin(last_analyzed, Company.id == last_analyzed.c.company_id)
        .where(Company.description_long.is_not(None))
        .where(Company.industry_group.is_not(None))
        .where(
            (last_analyzed.c.last_analyzed_at.is_(None))
            | (last_analyzed.c.last_analyzed_at < cutoff)
        )
        .order_by(
            last_analyzed.c.last_analyzed_at.asc().nullsfirst(),
            Company.name.asc(),
        )
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


# ---------------------------------------------------------------------------
# Peer-list query (placeholder — implemented in Task 4)
# ---------------------------------------------------------------------------


async def fetch_peers(
    session: AsyncSession, *, target: Company, max_peers: int = MAX_PEERS
) -> list[Peer]:
    raise NotImplementedError("Implemented in Task 4")


# ---------------------------------------------------------------------------
# Resolution (placeholder — implemented in Task 5)
# ---------------------------------------------------------------------------


async def resolve_competitor_company_id(
    session: AsyncSession, *, name: str
) -> UUID | None:
    raise NotImplementedError("Implemented in Task 5")


# ---------------------------------------------------------------------------
# Main loop (placeholder — implemented in Task 6)
# ---------------------------------------------------------------------------


async def run_analyze_competitors(
    session: AsyncSession,
    *,
    limit: int = 500,
    ttl_days: int = 25,
    dry_run: bool = False,
) -> AnalyzeCompetitorsSummary:
    raise NotImplementedError("Implemented in Task 6")
```

### Step 3.4 — Run tests, confirm eligibility tests pass

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -k eligible -v
```

Expected: 4 passed.

### Step 3.5 — Lint, typecheck

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src
```

Expected: all green.

### Step 3.6 — Commit

```bash
git checkout -b m4/stage-scaffold
git add pipeline/src/nous/pipeline/analyze_competitors.py pipeline/tests/test_analyze_competitors_stage.py
git commit -m "$(cat <<'EOF'
feat(m4,stage): scaffold analyze-competitors module + eligibility query

Skeleton for the analyze-competitors stage: AnalyzeCompetitorsSummary
model, fetch_eligible_companies (enforces description_long + industry_group
gates and the 25-day TTL), and NotImplementedError placeholders for the
peer-list query, resolution, and main loop (filled in subsequent tasks).
EOF
)"
git push -u origin m4/stage-scaffold
gh pr create --title "feat(m4,stage): scaffold analyze-competitors + eligibility query" \
  --body "M4 Task 3. Eligibility query covered by 4 integration tests."
```

---

## Task 4 — Stage: peer-list query

**Branch:** `m4/stage-peer-list`
**Depends on:** Task 3

**Files:**
- Modify: `pipeline/src/nous/pipeline/analyze_competitors.py`
- Modify: `pipeline/tests/test_analyze_competitors_stage.py`

### Step 4.1 — Append failing tests to the test file

- [ ] Append to `pipeline/tests/test_analyze_competitors_stage.py` (after the eligibility tests):

```python
# ---------------------------------------------------------------------------
# Peer-list query
# ---------------------------------------------------------------------------


async def test_peers_same_industry_only(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    same = _make_company("Same", industry_group="SaaS")
    other = _make_company("Other", industry_group="Hardware")
    db.add_all([target, same, other])
    await db.flush()

    peers = await fetch_peers(db, target=target)
    names = {p.name for p in peers}
    assert "Same" in names
    assert "Other" not in names


async def test_peers_exclude_self(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    peer = _make_company("Peer", industry_group="SaaS")
    db.add_all([target, peer])
    await db.flush()

    peers = await fetch_peers(db, target=target)
    names = {p.name for p in peers}
    assert "Target" not in names
    assert "Peer" in names


async def test_peers_capped_at_max(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    db.add_all(
        [
            _make_company(f"Peer{i:03d}", industry_group="SaaS")
            for i in range(60)
        ]
    )
    await db.flush()

    peers = await fetch_peers(db, target=target, max_peers=50)
    assert len(peers) == 50


async def test_peers_carry_short_description(db: AsyncSession) -> None:
    target = _make_company("Target", industry_group="SaaS")
    peer = _make_company("Peer", industry_group="SaaS")
    db.add_all([target, peer])
    await db.flush()

    peers = await fetch_peers(db, target=target)
    assert peers[0].description_short == "Peer short."
```

### Step 4.2 — Run tests, confirm they fail

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -k peers -v
```

Expected: 4 FAIL with `NotImplementedError: Implemented in Task 4`.

### Step 4.3 — Implement `fetch_peers`

- [ ] Replace the `fetch_peers` placeholder in `pipeline/src/nous/pipeline/analyze_competitors.py` with:

```python
async def fetch_peers(
    session: AsyncSession, *, target: Company, max_peers: int = MAX_PEERS
) -> list[Peer]:
    """Return up to `max_peers` companies in the same industry_group as `target`,
    excluding the target itself. Ordered by name for deterministic output."""
    stmt = (
        select(Company.name, Company.description_short)
        .where(Company.industry_group == target.industry_group)
        .where(Company.id != target.id)
        .where(Company.description_short.is_not(None))
        .order_by(Company.name.asc())
        .limit(max_peers)
    )
    rows = (await session.execute(stmt)).all()
    return [
        Peer(name=row.name, description_short=row.description_short or "")
        for row in rows
    ]
```

### Step 4.4 — Run tests, confirm pass

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -k peers -v
```

Expected: 4 passed.

### Step 4.5 — Lint, typecheck

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src
```

Expected: all green.

### Step 4.6 — Commit

```bash
git checkout -b m4/stage-peer-list
git add pipeline/src/nous/pipeline/analyze_competitors.py pipeline/tests/test_analyze_competitors_stage.py
git commit -m "$(cat <<'EOF'
feat(m4,stage): peer-list query for competitor-analysis prompt

fetch_peers returns up to 50 same-industry companies (target excluded)
with their short descriptions, ordered by name for determinism. Drives
the peer block of the spec §6.3 prompt.
EOF
)"
git push -u origin m4/stage-peer-list
gh pr create --title "feat(m4,stage): peer-list query" --body "M4 Task 4."
```

---

## Task 5 — Stage: competitor name resolution

**Branch:** `m4/stage-resolution`
**Depends on:** Task 4

**Files:**
- Modify: `pipeline/src/nous/pipeline/analyze_competitors.py`
- Modify: `pipeline/tests/test_analyze_competitors_stage.py`

### Step 5.1 — Append failing tests

- [ ] Append to `pipeline/tests/test_analyze_competitors_stage.py`:

```python
# ---------------------------------------------------------------------------
# Competitor resolution
# ---------------------------------------------------------------------------


async def test_resolve_exact_normalized_match(db: AsyncSession) -> None:
    rival = _make_company("Beta Co")
    db.add(rival)
    await db.flush()

    resolved = await resolve_competitor_company_id(db, name="Beta Co")
    assert resolved == rival.id


async def test_resolve_normalizes_case(db: AsyncSession) -> None:
    rival = _make_company("Beta Co")
    db.add(rival)
    await db.flush()

    resolved = await resolve_competitor_company_id(db, name="BETA CO")
    assert resolved == rival.id


async def test_resolve_returns_none_for_unknown(db: AsyncSession) -> None:
    resolved = await resolve_competitor_company_id(db, name="NeverSeen")
    assert resolved is None
```

### Step 5.2 — Run tests, confirm they fail

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -k resolve -v
```

Expected: 3 FAIL with `NotImplementedError: Implemented in Task 5`.

### Step 5.3 — Implement `resolve_competitor_company_id`

- [ ] Replace the `resolve_competitor_company_id` placeholder in `pipeline/src/nous/pipeline/analyze_competitors.py` with:

```python
async def resolve_competitor_company_id(
    session: AsyncSession, *, name: str
) -> UUID | None:
    """Look up an indexed company by exact normalized_name match.

    The Company.normalized_name column is populated by M1/M3 upsert paths
    using the same lowercase strategy applied here. Fuzzy match is
    deliberately deferred — spec §10 lists it as out-of-scope for M4.
    """
    normalized = name.strip().lower()
    if not normalized:
        return None
    stmt = select(Company.id).where(Company.normalized_name == normalized).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()
```

### Step 5.4 — Run tests, confirm pass

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -k resolve -v
```

Expected: 3 passed.

### Step 5.5 — Lint, typecheck

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src
```

Expected: all green.

### Step 5.6 — Commit

```bash
git checkout -b m4/stage-resolution
git add pipeline/src/nous/pipeline/analyze_competitors.py pipeline/tests/test_analyze_competitors_stage.py
git commit -m "$(cat <<'EOF'
feat(m4,stage): exact normalized_name resolution for competitors

resolve_competitor_company_id looks up an indexed company by exact
lowercased name match. Unmatched competitors are stored text-only
(competitor_company_id=NULL) per spec §2.
EOF
)"
git push -u origin m4/stage-resolution
gh pr create --title "feat(m4,stage): competitor resolution" --body "M4 Task 5."
```

---

## Task 6 — Stage: main loop with replace-style write

**Branch:** `m4/stage-main-loop`
**Depends on:** Task 5

**Files:**
- Modify: `pipeline/src/nous/pipeline/analyze_competitors.py`
- Modify: `pipeline/tests/test_analyze_competitors_stage.py`

### Step 6.1 — Append happy-path test

- [ ] Append to `pipeline/tests/test_analyze_competitors_stage.py`:

```python
# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _fixture_extraction(names: list[str]) -> CompetitorAnalysis:
    return CompetitorAnalysis(
        competitors=[
            CompetitorOut(
                name=n,
                description=f"{n} description.",
                reasoning=f"{n} reasoning.",
                rank=i,
            )
            for i, n in enumerate(names, start=1)
        ]
    )


async def test_happy_path_writes_competitors_and_resolves_links(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    rival = _make_company("RivalCo", industry_group="SaaS")
    db.add_all([target, rival])
    await db.flush()

    extraction = _fixture_extraction(["RivalCo", "UnindexedCo"])

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        assert schema is CompetitorAnalysis
        return extraction

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)

    assert summary.companies_analyzed == 1
    assert summary.competitors_written == 2
    assert summary.competitors_linked == 1
    assert summary.competitors_unlinked == 1

    rows = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == target.id).order_by(Competitor.rank)
        )
    ).scalars().all()
    assert [r.competitor_name for r in rows] == ["RivalCo", "UnindexedCo"]
    assert rows[0].competitor_company_id == rival.id
    assert rows[1].competitor_company_id is None


async def test_rerun_replaces_existing_rows(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    # Pre-existing competitor row that's older than the TTL so the run picks it up.
    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="OldRival",
            rank=1,
            updated_at=datetime.now(timezone.utc) - timedelta(days=40),
        )
    )
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        return _fixture_extraction(["NewRival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    await run_analyze_competitors(db, limit=10, ttl_days=25)

    rows = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == target.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].competitor_name == "NewRival"


async def test_dry_run_writes_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        return _fixture_extraction(["Rival"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25, dry_run=True)
    assert summary.companies_analyzed == 1

    rows = (
        await db.execute(select(Competitor).where(Competitor.company_id == target.id))
    ).scalars().all()
    assert rows == []
```

### Step 6.2 — Run tests, confirm they fail

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -k "happy_path or rerun_replaces or dry_run" -v
```

Expected: 3 FAIL with `NotImplementedError: Implemented in Task 6`.

### Step 6.3 — Implement the main loop

- [ ] Replace the `run_analyze_competitors` placeholder in `pipeline/src/nous/pipeline/analyze_competitors.py` with:

```python
async def run_analyze_competitors(
    session: AsyncSession,
    *,
    limit: int = 500,
    ttl_days: int = 25,
    dry_run: bool = False,
) -> AnalyzeCompetitorsSummary:
    summary = AnalyzeCompetitorsSummary()

    companies = await fetch_eligible_companies(
        session, limit=limit, ttl_days=ttl_days
    )

    for company in companies:
        peers = await fetch_peers(session, target=company)
        target = Target(
            name=company.name,
            description_short=company.description_short or "",
            description_long=company.description_long or "",
            industry_group=company.industry_group or "",
        )
        prompt = build_prompt(target=target, peers=peers)

        try:
            analysis: CompetitorAnalysis = await complete_json(
                prompt, CompetitorAnalysis
            )
        except LLMRateLimitError:
            logger.warning(
                "Gemini rate limit hit while analyzing competitors for %s — "
                "stopping loop to avoid further quota exhaustion.",
                company.name,
            )
            summary.skipped_rate_limited += 1
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning(
                "LLM error analyzing competitors for %s: %s", company.name, exc
            )
            summary.llm_failures += 1
            continue

        summary.companies_analyzed += 1

        # Resolve each competitor name to a company_id (None if unmatched).
        resolved: list[tuple[UUID | None, str, str, str, int]] = []
        for c in analysis.competitors:
            cid = await resolve_competitor_company_id(session, name=c.name)
            resolved.append((cid, c.name, c.description, c.reasoning, c.rank))

        if dry_run:
            continue

        # Replace-style write: delete then insert in one transaction. The outer
        # session manages the transaction; we use a SAVEPOINT via begin_nested()
        # so the eligibility loop's prior writes stay intact if this one fails.
        async with session.begin_nested():
            await session.execute(
                delete(Competitor).where(Competitor.company_id == company.id)
            )
            now = datetime.now(timezone.utc)
            for cid, name, desc, reasoning, rank in resolved:
                session.add(
                    Competitor(
                        company_id=company.id,
                        competitor_company_id=cid,
                        competitor_name=name,
                        description=desc,
                        reasoning=reasoning,
                        rank=rank,
                        updated_at=now,
                    )
                )
                summary.competitors_written += 1
                if cid is not None:
                    summary.competitors_linked += 1
                else:
                    summary.competitors_unlinked += 1
        await session.flush()

    return summary
```

### Step 6.4 — Run tests, confirm happy path + replace + dry-run pass

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -v
```

Expected: all passing (4 eligibility + 4 peers + 3 resolve + 3 main-loop = 14 passed).

### Step 6.5 — Lint, typecheck

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src
```

Expected: all green.

### Step 6.6 — Commit

```bash
git checkout -b m4/stage-main-loop
git add pipeline/src/nous/pipeline/analyze_competitors.py pipeline/tests/test_analyze_competitors_stage.py
git commit -m "$(cat <<'EOF'
feat(m4,stage): main loop with replace-style write

run_analyze_competitors orchestrates eligibility → peer list → LLM call
→ resolution → replace-style write. The write happens inside a SAVEPOINT
so a failure on company N doesn't roll back companies 0..N-1. Honors
dry_run by skipping the write phase.
EOF
)"
git push -u origin m4/stage-main-loop
gh pr create --title "feat(m4,stage): main loop with replace-style write" --body "M4 Task 6."
```

---

## Task 7 — Stage: error-path coverage

**Branch:** `m4/stage-error-paths`
**Depends on:** Task 6

**Files:**
- Modify: `pipeline/tests/test_analyze_competitors_stage.py` (tests only — implementation already handles these paths)

### Step 7.1 — Append failure-mode tests

- [ ] Append to `pipeline/tests/test_analyze_competitors_stage.py`:

```python
async def test_rate_limit_halts_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _make_company("First", industry_group="SaaS")
    second = _make_company("Second", industry_group="SaaS")
    db.add_all([first, second])
    await db.flush()

    call_count = {"n": 0}

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _fixture_extraction(["X"])
        raise LLMRateLimitError("429")

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert summary.companies_analyzed == 1
    assert summary.skipped_rate_limited == 1

    # First company's row should be persisted; loop broke before second got written.
    rows_first = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == first.id)
        )
    ).scalars().all()
    rows_second = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == second.id)
        )
    ).scalars().all()
    assert len(rows_first) == 1
    assert rows_second == []


async def test_parse_error_skips_company_and_continues(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _make_company("Bad", industry_group="SaaS")
    good = _make_company("Good", industry_group="SaaS")
    db.add_all([bad, good])
    await db.flush()

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        # First call (sorted by name: "Bad" before "Good" alphabetically) raises.
        if "Bad" in prompt:
            raise LLMParseError("schema mismatch")
        return _fixture_extraction(["X"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert summary.llm_failures == 1
    assert summary.companies_analyzed == 1

    rows_good = (
        await db.execute(
            select(Competitor).where(Competitor.company_id == good.id)
        )
    ).scalars().all()
    assert len(rows_good) == 1


async def test_ttl_gate_skips_recently_analyzed(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _make_company("Target", industry_group="SaaS")
    db.add(target)
    await db.flush()

    db.add(
        Competitor(
            company_id=target.id,
            competitor_name="Existing",
            rank=1,
            updated_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
    )
    await db.flush()

    called = {"n": 0}

    async def _fake_complete_json(prompt: str, schema: type) -> CompetitorAnalysis:
        called["n"] += 1
        return _fixture_extraction(["Anything"])

    monkeypatch.setattr(
        "nous.pipeline.analyze_competitors.complete_json", _fake_complete_json
    )

    summary = await run_analyze_competitors(db, limit=10, ttl_days=25)
    assert called["n"] == 0
    assert summary.companies_analyzed == 0
```

### Step 7.2 — Run tests, confirm all pass

- [ ] Run from `pipeline/`:

```bash
uv run pytest tests/test_analyze_competitors_stage.py -v
```

Expected: all 17 passed.

### Step 7.3 — Lint, typecheck, full sweep

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src && uv run pytest -q
```

Expected: all green.

### Step 7.4 — Commit

```bash
git checkout -b m4/stage-error-paths
git add pipeline/tests/test_analyze_competitors_stage.py
git commit -m "$(cat <<'EOF'
test(m4,stage): rate-limit, parse-error, and TTL-gate coverage

Locks in the three failure-mode contracts of run_analyze_competitors:
- LLMRateLimitError breaks the loop, preserving prior writes
- LLMParseError increments llm_failures and continues
- TTL gate prevents re-analyzing a company within ttl_days
EOF
)"
git push -u origin m4/stage-error-paths
gh pr create --title "test(m4,stage): error-path coverage" --body "M4 Task 7."
```

---

## Task 8 — CLI: replace the `analyze-competitors` stub

**Branch:** `m4/cli-analyze-competitors`
**Depends on:** Task 6

**Files:**
- Modify: `pipeline/src/nous/cli.py` (lines 339–341 — the existing stub)

### Step 8.1 — Replace the stub

- [ ] In `pipeline/src/nous/cli.py`, replace the existing block:

```python
@cli.command("analyze-competitors")
def analyze_competitors() -> None:
    _stub("analyze-competitors")
```

with:

```python
@cli.command("analyze-competitors")
@click.option(
    "--limit",
    type=int,
    default=500,
    show_default=True,
    help="Maximum number of companies to analyze per run (monthly Gemini budget).",
)
@click.option(
    "--ttl-days",
    type=int,
    default=25,
    show_default=True,
    help="Skip companies whose competitors were updated within this many days.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run eligibility + LLM calls but skip the DB write.",
)
def analyze_competitors(limit: int, ttl_days: int, dry_run: bool) -> None:
    """Run the competitor-analysis LLM over eligible companies."""
    import asyncio

    from nous.db.session import AsyncSessionLocal
    from nous.pipeline.analyze_competitors import run_analyze_competitors

    async def _run() -> None:
        async with AsyncSessionLocal() as session:
            summary = await run_analyze_competitors(
                session,
                limit=limit,
                ttl_days=ttl_days,
                dry_run=dry_run,
            )
            click.echo(summary.model_dump_json(indent=2))

    asyncio.run(_run())
```

### Step 8.2 — Sanity check via `--help`

- [ ] Run from `pipeline/`:

```bash
uv run nous analyze-competitors --help
```

Expected output includes the three flags and the "Run the competitor-analysis LLM…" docstring.

### Step 8.3 — Lint, typecheck, full sweep

- [ ] Run from `pipeline/`:

```bash
uv run ruff check . && uv run mypy src && uv run pytest -q
```

Expected: all green.

### Step 8.4 — Commit

```bash
git checkout -b m4/cli-analyze-competitors
git add pipeline/src/nous/cli.py
git commit -m "$(cat <<'EOF'
feat(m4,cli): wire analyze-competitors Click command

Replaces the stub with a real Click command exposing --limit, --ttl-days,
and --dry-run. Mirrors the extract-funding pattern: async runner inside
AsyncSessionLocal, summary echoed as indented JSON.
EOF
)"
git push -u origin m4/cli-analyze-competitors
gh pr create --title "feat(m4,cli): analyze-competitors command" --body "M4 Task 8."
```

---

## Task 9 — Web: types + queries.ts extension

**Branch:** `m4/web-types-and-query`
**Depends on:** Task 1 (DB column names — already locked in the spec)

**Files:**
- Modify: `web/lib/types.ts`
- Modify: `web/lib/queries.ts`

### Step 9.1 — Extend `web/lib/types.ts`

- [ ] Append to `web/lib/types.ts` (after `FundingRoundWithInvestors`):

```typescript
// ─── M4: competitors ──────────────────────────────────────────────────────────

/**
 * Row from the `competitors` table. `competitor_company_id` is non-null when
 * the LLM-named competitor resolves to an indexed company via exact
 * normalized_name match; otherwise the competitor is stored text-only.
 */
export interface CompetitorRow {
  id: string;
  company_id: string;
  competitor_company_id: string | null;
  competitor_name: string;
  description: string | null;
  reasoning: string | null;
  rank: number;
  created_at: string;
  updated_at: string;
}

/**
 * A competitor joined with the resolved company's slug + name, when present.
 * Built in `getCompanyBySlug` from the nested-select.
 */
export interface CompetitorWithResolved extends CompetitorRow {
  resolved: { slug: string; name: string } | null;
}
```

- [ ] Modify the `CompanyDetail` interface in the same file — add the `competitors` field:

```typescript
/** Full company detail assembled from four DB queries. */
export interface CompanyDetail {
  company: CompanyRow;
  filings: FilingRow[]; // sorted by filing_date desc
  relatedPersons: RelatedPersonRow[]; // most recent filing's people first
  fundingRounds: FundingRoundWithInvestors[]; // sorted by announced_date desc (nulls last)
  competitors: CompetitorWithResolved[]; // sorted by rank ascending
}
```

### Step 9.2 — Extend `web/lib/queries.ts`

- [ ] In `web/lib/queries.ts`, add to the top-of-file imports:

```typescript
import type {
  CompanyDetail,
  CompanyListRow,
  CompanyRow,
  CompetitorRow,
  CompetitorWithResolved,
  FilingRow,
  FundingRound,
  FundingRoundWithInvestors,
  RelatedPersonRow,
} from "@/lib/types";
```

- [ ] Add a narrow type for the nested PostgREST shape (place just below the existing `NestedFundingRoundInvestor` interface):

```typescript
interface NestedResolvedCompany {
  slug: string | null;
  name: string | null;
}

type CompetitorJoin = CompetitorRow & {
  competitor_company: NestedResolvedCompany | NestedResolvedCompany[] | null;
};
```

- [ ] Modify the `Promise.all` block in `getCompanyBySlug` from a 3-fetch tuple to a 4-fetch tuple by adding the competitors query:

```typescript
  const [filingsResult, personsResult, roundsResult, competitorsResult] =
    await Promise.all([
      supabase
        .from("filings")
        .select("*")
        .eq("company_id", companyId)
        .order("filing_date", { ascending: false }),

      supabase
        .from("related_persons")
        .select("*")
        .eq("company_id", companyId),

      supabase
        .from("funding_rounds")
        .select("*, funding_round_investors(is_lead, investors(name))")
        .eq("company_id", companyId),

      supabase
        .from("competitors")
        .select("*, competitor_company:companies!competitor_company_id(slug, name)")
        .eq("company_id", companyId)
        .order("rank", { ascending: true }),
    ]);
```

- [ ] Add error logging for the new query, immediately after the existing `roundsResult.error` block:

```typescript
  if (competitorsResult.error) {
    console.error(
      "[getCompanyBySlug] competitors query failed:",
      competitorsResult.error.message,
    );
  }
```

- [ ] Build the `competitors` payload just before the existing `return { company, filings, relatedPersons, fundingRounds }` and update the return:

```typescript
  const rawCompetitors = (competitorsResult.data ?? []) as CompetitorJoin[];
  const competitors: CompetitorWithResolved[] = rawCompetitors.map((row) => {
    const nested = Array.isArray(row.competitor_company)
      ? row.competitor_company[0]
      : row.competitor_company;
    const resolved =
      nested && nested.slug && nested.name
        ? { slug: nested.slug, name: nested.name }
        : null;
    const { competitor_company: _competitor_company, ...rest } = row;
    void _competitor_company;
    return { ...rest, resolved };
  });

  return {
    company: company as unknown as CompanyRow,
    filings,
    relatedPersons,
    fundingRounds,
    competitors,
  };
```

### Step 9.3 — Run web build

- [ ] Run from `web/`:

```bash
npm run build
```

Expected: build succeeds (the `[listCompanies] Supabase not configured` warning during static prerender is expected and pre-existing).

### Step 9.4 — Commit

```bash
git checkout -b m4/web-types-and-query
git add web/lib/types.ts web/lib/queries.ts
git commit -m "$(cat <<'EOF'
feat(m4,web): CompetitorRow types + competitors fetch in getCompanyBySlug

Adds the fourth parallel fetch to getCompanyBySlug — pulls the competitors
list with a PostgREST nested select that resolves each competitor to the
indexed company's slug + name in one round-trip. CompanyDetail now carries
a competitors: CompetitorWithResolved[] field sorted by rank ascending.
EOF
)"
git push -u origin m4/web-types-and-query
gh pr create --title "feat(m4,web): types + competitors query" --body "M4 Task 9."
```

---

## Task 10 — Web: Competitors component

**Branch:** `m4/web-competitors-component`
**Depends on:** Task 9

**Files:**
- Create: `web/components/Competitors.tsx`

### Step 10.1 — Implement the component

- [ ] Create `web/components/Competitors.tsx`:

```typescript
// Server component — renders the M4 competitors section on /c/[slug].
// No "use client": read-only display, all data flows in via props. Cards
// link internally when the competitor resolved to an indexed company.

import Link from "next/link";
import type { CompetitorWithResolved } from "@/lib/types";

interface Props {
  competitors: CompetitorWithResolved[];
}

export function Competitors({ competitors }: Props) {
  if (competitors.length === 0) {
    // Section omitted entirely when there is nothing to show — same convention
    // as the FundingHistory empty state and spec §11 (unknown = hidden).
    return null;
  }

  return (
    <section className="mb-12">
      <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100 mb-4">
        Competitors
      </h2>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {competitors.map((c) => {
          const NameTag = c.resolved ? Link : "span";
          const nameProps = c.resolved
            ? {
                href: `/c/${c.resolved.slug}`,
                className:
                  "font-semibold text-zinc-900 dark:text-zinc-100 hover:underline underline-offset-2",
              }
            : {
                className: "font-semibold text-zinc-900 dark:text-zinc-100",
              };

          return (
            <article
              key={c.id}
              className="rounded-lg border border-zinc-200 dark:border-zinc-800 p-4"
            >
              <header className="flex items-baseline gap-2">
                {/* @ts-expect-error — NameTag is a discriminated union of `Link` | "span" */}
                <NameTag {...nameProps}>{c.competitor_name}</NameTag>
                <span className="ml-auto text-xs text-zinc-400 dark:text-zinc-500">
                  #{c.rank}
                </span>
              </header>

              {c.description && (
                <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-300 leading-snug">
                  {c.description}
                </p>
              )}

              {c.reasoning && (
                <p className="mt-2 text-xs text-zinc-400 dark:text-zinc-500 leading-snug">
                  <span className="font-medium">Why they compete: </span>
                  {c.reasoning}
                </p>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
```

### Step 10.2 — Run web build

- [ ] Run from `web/`:

```bash
npm run build
```

Expected: build succeeds.

### Step 10.3 — Commit

```bash
git checkout -b m4/web-competitors-component
git add web/components/Competitors.tsx
git commit -m "$(cat <<'EOF'
feat(m4,web): Competitors server component

Grid of cards rendered server-side. Name wraps in <Link> when the
competitor resolves to an indexed company; plain span otherwise. Section
is omitted entirely when there are no competitor rows.
EOF
)"
git push -u origin m4/web-competitors-component
gh pr create --title "feat(m4,web): Competitors component" --body "M4 Task 10."
```

---

## Task 11 — Web: page integration

**Branch:** `m4/web-page-integration`
**Depends on:** Task 10

**Files:**
- Modify: `web/app/c/[slug]/page.tsx`

### Step 11.1 — Update destructure + insert the section

- [ ] In `web/app/c/[slug]/page.tsx`, add the import near the existing component imports:

```typescript
import { Competitors } from "@/components/Competitors";
```

- [ ] Update the destructure of `detail` to include the new field. Find:

```typescript
  const { company, filings, relatedPersons, fundingRounds } = detail;
```

Replace with:

```typescript
  const { company, filings, relatedPersons, fundingRounds, competitors } = detail;
```

- [ ] Insert the new section between the `<FundingHistory rounds={fundingRounds} />` line and the Filings table section. Find:

```tsx
      {/* ── Funding history (M3) ───────────────────────────────────────── */}
      <FundingHistory rounds={fundingRounds} />

      {/* ── Filings table ──────────────────────────────────────────────── */}
```

Replace with:

```tsx
      {/* ── Funding history (M3) ───────────────────────────────────────── */}
      <FundingHistory rounds={fundingRounds} />

      {/* ── Competitors (M4) ───────────────────────────────────────────── */}
      <Competitors competitors={competitors} />

      {/* ── Filings table ──────────────────────────────────────────────── */}
```

### Step 11.2 — Run web build

- [ ] Run from `web/`:

```bash
npm run build
```

Expected: build succeeds.

### Step 11.3 — Commit

```bash
git checkout -b m4/web-page-integration
git add web/app/c/[slug]/page.tsx
git commit -m "$(cat <<'EOF'
feat(m4,web): mount Competitors section on company detail page

Inserts the Competitors section between funding history and the filings
table on /c/[slug], matching spec §7.3 ordering.
EOF
)"
git push -u origin m4/web-page-integration
gh pr create --title "feat(m4,web): mount Competitors on /c/[slug]" --body "M4 Task 11."
```

---

## Task 12 — CI: rename monthly workflow and add analyze-competitors step

**Branch:** `m4/ci-monthly-refresh`
**Depends on:** Task 8

**Files:**
- Move (git): `.github/workflows/monthly-vc-refresh.yml` → `.github/workflows/monthly-refresh.yml`
- Modify: the renamed file

### Step 12.1 — Rename and update

- [ ] Rename and replace contents:

```bash
git mv .github/workflows/monthly-vc-refresh.yml .github/workflows/monthly-refresh.yml
```

- [ ] Replace the renamed file's contents with:

```yaml
name: monthly-refresh

on:
  schedule:
    # 09:00 UTC on the 1st of each month. Both VC portfolios and competitor
    # analysis move slowly; one monthly job keeps the weekly pipeline lean
    # and respects external sites + Gemini quota.
    - cron: "0 9 1 * *"
  workflow_dispatch:
    inputs:
      firms:
        description: "Optional comma-separated firm slugs (e.g. 'yc,a16z'). Default: all 7."
        required: false
        type: string
      similarity_threshold:
        description: "Optional pg_trgm threshold for fuzzy match. Default: Settings.COMPANY_FUZZY_MATCH_THRESHOLD (0.85)."
        required: false
        type: string
      competitor_limit:
        description: "Max companies analyze-competitors will process this run. Default: 500."
        required: false
        type: string

jobs:
  monthly-refresh:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: pipeline
    env:
      DATABASE_URL: ${{ secrets.DATABASE_URL }}
      SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}
      GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
      DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
      LLM_PROVIDER: ${{ secrets.LLM_PROVIDER }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install uv
        uses: astral-sh/setup-uv@v3

      - name: Install pipeline dependencies
        run: uv sync

      - name: Apply migrations
        run: uv run alembic upgrade head

      - name: Refresh VC portfolios
        run: |
          ARGS=()
          if [ -n "${{ inputs.firms }}" ]; then
            IFS=',' read -ra FIRMS <<< "${{ inputs.firms }}"
            for firm in "${FIRMS[@]}"; do
              ARGS+=(--firm "$firm")
            done
          fi
          if [ -n "${{ inputs.similarity_threshold }}" ]; then
            ARGS+=(--similarity-threshold "${{ inputs.similarity_threshold }}")
          fi
          uv run nous refresh-vc-portfolios "${ARGS[@]}"

      - name: Analyze competitors
        run: |
          LIMIT="${{ inputs.competitor_limit }}"
          if [ -z "$LIMIT" ]; then LIMIT=500; fi
          uv run nous analyze-competitors --limit "$LIMIT"
```

### Step 12.2 — Validate the YAML locally

- [ ] Run:

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/monthly-refresh.yml'))"
```

Expected: no output (valid YAML).

### Step 12.3 — Commit

```bash
git checkout -b m4/ci-monthly-refresh
git add .github/workflows/monthly-refresh.yml
git commit -m "$(cat <<'EOF'
ops(m4,ci): merge analyze-competitors into the monthly refresh workflow

Renames monthly-vc-refresh.yml → monthly-refresh.yml and adds an
analyze-competitors step after the existing refresh-vc-portfolios step.
Same monthly cron (09:00 UTC, 1st of month). New optional workflow_dispatch
input competitor_limit for ad-hoc runs.
EOF
)"
git push -u origin m4/ci-monthly-refresh
gh pr create --title "ops(m4,ci): monthly refresh adds analyze-competitors" --body "M4 Task 12."
```

---

## Task 13 — End-to-end smoke + verification

**Branch:** none (verification only — no new commit unless an issue surfaces)
**Depends on:** all previous tasks merged to `main`

### Step 13.1 — Local stage smoke (dry-run)

- [ ] Ensure local Postgres is reachable via `DATABASE_URL` (the same Supabase instance the deployed site reads from will do — read-only enough for a dry run). Run from `pipeline/`:

```bash
uv run nous analyze-competitors --limit 3 --dry-run
```

Expected: JSON summary with `companies_analyzed >= 0`, `competitors_written == 0` (dry-run), no exceptions.

### Step 13.2 — Local stage real run (small)

- [ ] Run from `pipeline/`:

```bash
uv run nous analyze-competitors --limit 3
```

Expected: JSON summary with `competitors_written > 0` (assuming there are eligible companies). `competitors_linked + competitors_unlinked == competitors_written`.

### Step 13.3 — Verify the rows landed

- [ ] Run from `pipeline/`:

```bash
uv run python -c "
import asyncio
from sqlalchemy import select, func
from nous.db.session import AsyncSessionLocal
from nous.db.models import Competitor

async def main():
    async with AsyncSessionLocal() as s:
        total = (await s.execute(select(func.count()).select_from(Competitor))).scalar_one()
        linked = (await s.execute(
            select(func.count()).select_from(Competitor).where(Competitor.competitor_company_id.is_not(None))
        )).scalar_one()
        print(f'rows={total} linked={linked} unlinked={total - linked}')

asyncio.run(main())
"
```

Expected: `rows > 0`, sane linked vs unlinked split.

### Step 13.4 — Web verification on Vercel preview

- [ ] Push the docs-only verification branch (or open a stub PR if needed to trigger a preview). Visit `/c/<slug>` for a company that was analyzed. Confirm:
  - Competitors section renders between funding history and the filings table.
  - Each card shows name, description, reasoning, rank badge.
  - Names that resolved to an indexed company are clickable links to `/c/<their-slug>`.
  - Names that didn't resolve are plain text.
  - For a company with no competitors row, the section is **absent** (not "no competitors found" placeholder).

### Step 13.5 — Confirm CI is green on `main`

- [ ] Visit `https://github.com/kasenteoh/nous/actions` and confirm the most recent push of `main` shows both the `pipeline` and `web` jobs green.

### Step 13.6 — Mark M4 done in the spec

- [ ] No code change. Open a checklist comment on the milestone or update `nous-technical-spec.md` Milestone 4 with a "✓ shipped <date>" annotation if the convention has emerged by then.

---

## Self-review notes (author)

**Spec coverage check** — every spec section has at least one task:
- §3 Database (table + migration) → Task 1
- §4 LLM prompt + schema → Task 2
- §5 Pipeline stage (eligibility, peer list, resolution, main loop, error paths, summary) → Tasks 3–7
- §6 CLI → Task 8
- §7.1 Types → Task 9
- §7.2 Query → Task 9
- §7.3 Component → Task 10
- §7.4 Page integration → Task 11
- §8 Ops/CI → Task 12
- §9 Tests → integrated into Tasks 1–7
- §12 Verification → Task 13

**Type consistency check** — all symbols referenced across tasks are defined in the task that introduces them:
- `Competitor` model — defined Task 1, referenced Tasks 3, 4, 6, 7
- `CompetitorAnalysis`, `Competitor` (Pydantic), `Peer`, `Target`, `build_prompt`, `MAX_PEERS`, `MAX_COMPETITORS` — defined Task 2, referenced Tasks 3, 6
- `AnalyzeCompetitorsSummary`, `fetch_eligible_companies`, `fetch_peers`, `resolve_competitor_company_id`, `run_analyze_competitors` — defined Task 3 (scaffold) and progressively implemented in Tasks 4–6
- `CompetitorRow`, `CompetitorWithResolved` — defined Task 9, referenced Task 10
- `Competitors` component — defined Task 10, referenced Task 11
