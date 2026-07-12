"""Unit tests for embed-companies helpers — no DB, no model download.

The stage's DB behavior (selection, idempotence, per-row commit) lives in
test_embed_companies_db.py; this file pins the pure functions the SQL side
must stay in lockstep with, the deterministic fake-embedder contract used
across both files, and the CLI registration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from click.testing import CliRunner

from nous.cli import cli
from nous.pipeline.embed_companies import (
    EMBEDDING_DIM,
    build_embedding_text,
    embedding_text_hash,
)


class FakeEmbedder:
    """Deterministic embedder: the vector is a pure function of the text.

    Derives EMBEDDING_DIM floats in [0, 1) from repeated sha256 over the
    text — stable across runs and platforms, so tests can assert that
    re-embedding the same text yields the same vector while a changed text
    yields a different one. Also counts calls so idempotence tests can assert
    the model seam was never touched.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        out: list[float] = []
        counter = 0
        while len(out) < EMBEDDING_DIM:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            out.extend(b / 255.0 for b in digest)
            counter += 1
        return out[:EMBEDDING_DIM]


def test_build_embedding_text_joins_name_and_descriptions() -> None:
    assert (
        build_embedding_text("Acme", "Short desc.", "Long desc.")
        == "Acme\nShort desc.\nLong desc."
    )


def test_build_embedding_text_absent_descriptions_are_empty() -> None:
    # None and "" must hash identically — the SQL twin uses coalesce(col, '').
    assert build_embedding_text("Acme", None, None) == "Acme\n\n"
    assert build_embedding_text("Acme", None, None) == build_embedding_text(
        "Acme", "", ""
    )


def test_embedding_text_hash_is_sha256_hex_of_utf8() -> None:
    text = "Acme\nBüilds ünïcode—things\n🚀 long"
    assert (
        embedding_text_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    )
    # 64 lowercase hex chars — what the TEXT column stores and SQL's
    # encode(sha256(...), 'hex') produces.
    digest = embedding_text_hash(text)
    assert len(digest) == 64
    assert digest == digest.lower()


def test_fake_embedder_is_deterministic_and_text_sensitive() -> None:
    embedder = FakeEmbedder()
    [a1] = embedder.embed(["alpha"])
    [a2] = embedder.embed(["alpha"])
    [b] = embedder.embed(["beta"])
    assert a1 == a2
    assert a1 != b
    assert len(a1) == EMBEDDING_DIM


def test_embed_companies_cli_is_registered_with_bounded_default() -> None:
    result = CliRunner().invoke(cli, ["embed-companies", "--help"])
    assert result.exit_code == 0
    assert "--limit" in result.output
    assert "200" in result.output  # the pipeline.yml-documented default
