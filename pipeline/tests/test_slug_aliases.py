"""Pure tests for the slug-alias recording guard (no DB required).

The DB-gated integration coverage (alias recording on merge, chain
convergence, upsert idempotency, shadow-alias cleanup) lives in
test_slug_aliases_db.py; this module pins the one pure invariant: a survivor's
own live slug is never recorded as an alias of itself.
"""

from __future__ import annotations

from nous.db.upsert import _should_record_slug_alias


def test_survivors_own_slug_is_never_aliased() -> None:
    """The guard refuses a loser slug equal to the survivor's live slug — a
    self-alias would (latently) redirect a page to itself."""
    assert _should_record_slug_alias("acme", "acme") is False


def test_distinct_loser_slug_is_recorded() -> None:
    assert _should_record_slug_alias("acme-inc", "acme") is True


def test_guard_is_case_sensitive_like_slugs() -> None:
    """Slugs are lowercase by construction (slugify), so comparison is plain
    string equality — no case folding that could conflate distinct slugs."""
    assert _should_record_slug_alias("Acme", "acme") is True
