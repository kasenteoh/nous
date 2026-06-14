"""denormalize latest_round_* on companies for sorting/filtering

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-14 00:00:00.000000

Adds three denormalized "most recent funding round" columns to ``companies``:
``latest_round_amount`` (Numeric, indexed), ``latest_round_date`` (Date,
indexed), and ``latest_round_type`` (text, indexed).

The web browse page needs to sort by biggest raise / most-recent raise and
filter by funding stage / funded-since, but PostgREST cannot ORDER BY (or
paginate) an aggregate over the one-to-many ``funding_rounds`` embed. These
columns flatten the single most-recent round onto the company row so each of
those becomes a plain indexed WHERE/ORDER BY. They are kept fresh at runtime by
the ``refresh-latest-round`` stage (called at the end of the extract-funding
paths), whose recompute mirrors the backfill below.

"Most recent" is the round with the greatest ``announced_date`` (NULLS LAST):
a dated round always wins over an undated one; a company whose only round is
undated still gets its type/amount, with ``latest_round_date`` left NULL.

All three columns are indexed: amount/date back the funding_desc /
recently_funded sorts and the raise-range / funded-since filters;
latest_round_type backs the stage (=) filter — every column used in a WHERE is
indexed per the project conventions.

Hand-written per the 0015+ convention (autogenerate emits spurious DROPs for
hand-created objects such as the trigram GIN index it cannot model).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("latest_round_amount", sa.Numeric(20, 2), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("latest_round_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("latest_round_type", sa.String(), nullable=True),
    )

    op.create_index(
        "ix_companies_latest_round_amount", "companies", ["latest_round_amount"]
    )
    op.create_index(
        "ix_companies_latest_round_date", "companies", ["latest_round_date"]
    )
    op.create_index(
        "ix_companies_latest_round_type", "companies", ["latest_round_type"]
    )

    # Backfill: for each company, pick its most-recent round via DISTINCT ON,
    # ordered by announced_date DESC NULLS LAST (a dated round beats an undated
    # one; the secondary id ordering makes the pick deterministic when two
    # rounds share a date). Companies with no rounds are simply absent from the
    # subquery and keep NULL columns. This mirrors refresh_latest_round exactly
    # so the columns are correct the moment the migration runs.
    op.execute(
        """
        UPDATE companies c
        SET latest_round_amount = sub.amount_raised,
            latest_round_date   = sub.announced_date,
            latest_round_type   = sub.round_type
        FROM (
            SELECT DISTINCT ON (fr.company_id)
                   fr.company_id,
                   fr.amount_raised,
                   fr.announced_date,
                   fr.round_type
            FROM funding_rounds fr
            ORDER BY fr.company_id,
                     fr.announced_date DESC NULLS LAST,
                     fr.id DESC
        ) sub
        WHERE c.id = sub.company_id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_companies_latest_round_type", table_name="companies")
    op.drop_index("ix_companies_latest_round_date", table_name="companies")
    op.drop_index("ix_companies_latest_round_amount", table_name="companies")
    op.drop_column("companies", "latest_round_type")
    op.drop_column("companies", "latest_round_date")
    op.drop_column("companies", "latest_round_amount")
