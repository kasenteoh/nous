"""Embed shown companies' descriptions with a local CPU model — $0 LLM cost.

Wave 3 (E-1). Computes a 384-dim sentence embedding over ``name +
description_short + description_long`` for every shown company whose
description text changed since it was last embedded, and stamps it onto
``companies.embedding`` (migration 0033). The web's similar-companies module
ranks nearest neighbors over these vectors via the ``similar_companies`` SQL
function; rows without an embedding simply render no section (no fabrication).

Model: fastembed's ``BAAI/bge-small-en-v1.5`` (ONNX, CPU-only, ~130MB one-time
download cached in Actions). fastembed lives in the optional ``embeddings``
uv dependency group so the default ``uv sync`` stays light — only pipeline.yml
installs it (``uv sync --group embeddings``); lint/CI tests inject the
deterministic fake embedder instead and never download a model.

Idempotence: ``embedding_text_hash`` stores the sha256 of the exact text
embedded. Selection compares it against the hash recomputed IN SQL (Postgres'
built-in ``sha256`` over ``convert_to(text, 'UTF8')``), so ``--limit`` bounds
real work — unchanged rows are never selected, and a re-run after a full pass
selects nothing. The SQL hash and :func:`embedding_text_hash` must stay
byte-identical; ``test_embed_companies.py`` pins that parity (a drift would
only cause needless CPU re-embeds, never wrong data, but pin it anyway).

Mirrors estimate-employees: one commit per company so a mid-run crash leaves
consistent rows (hash + vector + embedded_at always travel together), and
``StaleDataError`` (concurrent dedup merge deleting a row mid-run) skips the
row rather than sinking the run.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import String, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql.elements import ColumnElement

from nous.db.models import Company

logger = logging.getLogger(__name__)

# bge-small-en-v1.5: best small English retrieval model on CPU; 384 dims must
# match companies.embedding vector(384) (migration 0033).
EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# Embed in batches so the ONNX runtime amortizes per-call overhead; writes
# remain per-row (see module docstring).
_BATCH_SIZE = 32


class Embedder(Protocol):
    """The model seam: anything that turns texts into fixed-dim vectors.

    The stage depends only on this Protocol; the real fastembed adapter
    (:class:`FastembedEmbedder`) is constructed in the CLI, and tests inject a
    deterministic fake so no model is ever downloaded in CI.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one ``EMBEDDING_DIM``-length vector per input text."""
        ...


class FastembedEmbedder:
    """Real adapter over fastembed's ONNX runtime (CPU).

    Imports fastembed lazily so the module stays importable in environments
    without the optional ``embeddings`` dependency group (lint CI, web-only
    work, the default local ``uv sync``).
    """

    def __init__(self, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding  # optional dep — see module docstring

        resolved = str(Path(cache_dir).expanduser()) if cache_dir else None
        self._model = TextEmbedding(model_name=EMBEDDING_MODEL_NAME, cache_dir=resolved)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self._model.embed(list(texts))]


class EmbedCompaniesSummary(BaseModel):
    """Result of one embed-companies run."""

    companies_seen: int = 0  # rows selected as needing (re-)embedding
    embedded: int = 0  # rows whose vector+hash+embedded_at were written
    errors: int = 0  # bad model output / concurrent-delete skips


def build_embedding_text(
    name: str, description_short: str | None, description_long: str | None
) -> str:
    """The exact text embedded and hashed for a company.

    Newline-joined with absent descriptions as empty strings. MUST stay in
    lockstep with ``_sql_embedding_text`` (the SQL twin used in selection) —
    the hash-parity test pins them together.
    """
    return f"{name}\n{description_short or ''}\n{description_long or ''}"


def embedding_text_hash(text: str) -> str:
    """sha256 hex digest of the embedding text — the idempotence key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sql_embedding_text() -> ColumnElement[str]:
    """SQL twin of :func:`build_embedding_text` over the companies columns."""
    return (
        Company.name
        + literal("\n")
        + func.coalesce(Company.description_short, literal(""))
        + literal("\n")
        + func.coalesce(Company.description_long, literal(""))
    )


def _sql_embedding_text_hash() -> ColumnElement[str]:
    """SQL twin of :func:`embedding_text_hash`: sha256 hex over UTF-8 bytes.

    ``sha256(bytea)`` is a Postgres builtin (PG 11+, no pgcrypto needed);
    ``convert_to(..., 'UTF8')`` mirrors Python's ``str.encode("utf-8")``.
    """
    return func.encode(
        func.sha256(func.convert_to(_sql_embedding_text(), literal("UTF8"))),
        literal("hex"),
    ).cast(String)


async def run_embed_companies(
    session: AsyncSession,
    embedder: Embedder,
    *,
    limit: int | None = None,
) -> EmbedCompaniesSummary:
    """Embed shown companies whose description text changed since last embed.

    Selection (all in SQL, so ``--limit`` bounds real work):
    - shown: ``exclusion_reason IS NULL`` — excluded companies are never
      embedded, so they can never surface as a neighbor even before the RPC's
      own filter;
    - has a description: ``description_short IS NOT NULL`` (name alone embeds
      to noise, and the catalog only shows described companies' profiles);
    - needs work: no embedding yet, or the stored hash differs from the hash
      of the current text (computed in SQL — see module docstring).

    Never-embedded rows first, then stalest embeddings, so a bounded run
    drains the backlog fairly across dispatches.
    """
    summary = EmbedCompaniesSummary()

    stmt = (
        select(Company)
        .where(
            Company.exclusion_reason.is_(None),
            Company.description_short.is_not(None),
            or_(
                Company.embedding.is_(None),
                Company.embedding_text_hash.is_(None),
                Company.embedding_text_hash != _sql_embedding_text_hash(),
            ),
        )
        .order_by(Company.embedded_at.asc().nulls_first(), Company.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    companies = (await session.execute(stmt)).scalars().all()
    summary.companies_seen = len(companies)

    for start in range(0, len(companies), _BATCH_SIZE):
        batch = companies[start : start + _BATCH_SIZE]
        texts = [
            build_embedding_text(c.name, c.description_short, c.description_long)
            for c in batch
        ]
        vectors = embedder.embed(texts)
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} texts"
            )

        for company, text, vector in zip(batch, texts, vectors, strict=True):
            if len(vector) != EMBEDDING_DIM:
                # Wrong model wired in — skip rather than corrupt the column
                # (a dimension mismatch would also fail the vector(384) cast).
                logger.error(
                    "embed-companies: got %d-dim vector for %s (want %d) — skipping",
                    len(vector),
                    company.slug,
                    EMBEDDING_DIM,
                )
                summary.errors += 1
                continue

            # Vector, hash, and timestamp always travel together (one commit),
            # so a crash can't leave a hash claiming an embedding that isn't
            # there — the hash is only trustworthy because of this.
            company.embedding = vector
            company.embedding_text_hash = embedding_text_hash(text)
            company.embedded_at = datetime.now(tz=UTC)
            session.add(company)
            try:
                await session.commit()
            except StaleDataError:
                # Row deleted mid-run — almost always a concurrent dedup merge.
                await session.rollback()
                logger.warning(
                    "Company %s disappeared mid-embed (likely a concurrent merge) "
                    "— skipping.",
                    company.id,
                )
                summary.errors += 1
                continue
            summary.embedded += 1

    logger.info(
        "embed-companies: seen=%d embedded=%d errors=%d",
        summary.companies_seen,
        summary.embedded,
        summary.errors,
    )
    return summary
