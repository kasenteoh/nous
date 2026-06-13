"""null unevidenced US country

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-13 00:00:00.000000

Root cause: ``auto_create_company`` previously set ``hq_country = 'US'`` on
every new company row.  ``Company.hq_country`` also carried a Python-level
``default="US"``.  As a result, every company discovered from a VC portfolio
page or news article read as US — Fresha (London), Meesho (India), NOTHS (UK),
etc. — and the ``non_us`` exclusion filter never fired.

This migration nulls ``hq_country`` (and resets ``eligibility_checked_at``) for
rows that received only that unevidenced default.

**Conservative WHERE — we null FEWER rows when in doubt:**

    hq_country = 'US'
    AND eligibility_checked_at IS NULL          -- not yet judged: might be real US
    AND exclusion_reason IS NULL                -- already excluded rows stay as-is
    AND hq_state IS NULL                        -- US state would be hard US evidence
    AND hq_city IS NULL                         -- a US city name is at least soft evidence

Rows that pass the filter have ``hq_country = 'US'`` with NO corroborating US
signal (no state, no city) AND have never been eligibility-judged.  These are
essentially the un-enriched discoveries that got the 'US' default on insert and
nothing more.

We do NOT touch:
- Rows with ``eligibility_checked_at IS NOT NULL`` — an LLM has already looked
  at this company and confirmed or re-set the country; we must not overwrite that.
- Rows with ``hq_state`` or ``hq_city`` set — a US city/state is evidence the
  company is US; we leave those rows alone.
- Rows with any ``exclusion_reason`` — already processed, no point resetting.

``eligibility_checked_at`` is reset to NULL on the nulled rows so the
judge-eligibility backfill re-evaluates them (otherwise they would sit in
the backlog forever with a NULL country but a non-NULL stamp blocking the
backfill query).

downgrade(): data migration — we cannot know which 'US' values came from the
default vs. a real LLM or human signal, so restoring is unsafe.  The downgrade
is intentionally a no-op with a comment.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Null hq_country (and reset the eligibility stamp) for rows that have the
    # unevidenced US default.  Exact WHERE clause — DO NOT widen without
    # reviewing against the spec note above:
    #
    #   hq_country = 'US'
    #   AND eligibility_checked_at IS NULL
    #   AND exclusion_reason IS NULL
    #   AND hq_state IS NULL
    #   AND hq_city IS NULL
    op.execute(
        """
        UPDATE companies
        SET
            hq_country            = NULL,
            eligibility_checked_at = NULL
        WHERE
            hq_country             = 'US'
            AND eligibility_checked_at IS NULL
            AND exclusion_reason   IS NULL
            AND hq_state           IS NULL
            AND hq_city            IS NULL
        """
    )


def downgrade() -> None:
    # Intentional no-op: this is a data-quality migration.  We cannot safely
    # restore 'US' to rows that had only the unevidenced default — we cannot
    # distinguish those from rows that gained a real US signal after this
    # migration ran.  Restoring blindly would re-introduce the bug.
    #
    # To restore the pre-migration state, take a DB snapshot BEFORE running
    # this upgrade and restore from that if needed.
    pass
