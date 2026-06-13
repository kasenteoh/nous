"""investor portfolio_count denormalized column

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-13 00:00:00.000000

Adds a denormalized ``portfolio_count`` to the ``investors`` table — the count
of distinct non-excluded companies backed by that investor via EITHER the
company-level link (``company_investors``) OR a funding round
(``funding_round_investors`` → ``funding_rounds``).

Previously the investor index had no reliable sort key: round-only investors
showed "0 companies" (the company_investors count), while VC-portfolio
investors showed only the direct link count, ignoring their funding-round
appearances. This single integer fixes both problems and lets the web index
page order by portfolio_count DESC.

The backfill UPDATE uses the same UNION-count logic as the runtime
refresh-investor-counts stage so the column is immediately correct after the
migration runs, with no separate backfill step required.

Hand-written per the 0015+ convention (autogenerate emits spurious DROPs for
hand-created objects such as the trigram GIN index it cannot model).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investors",
        sa.Column(
            "portfolio_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_investors_portfolio_count", "investors", ["portfolio_count"]
    )

    # Backfill: count distinct non-excluded companies per investor across BOTH
    # link tables and set each investor's portfolio_count. The UNION (not
    # UNION ALL) deduplicates (investor_id, company_id) pairs so a company
    # backed via both tables is counted only once. Investors with no qualifying
    # companies stay at 0 (the server_default).
    op.execute(
        """
        UPDATE investors i
        SET portfolio_count = sub.n
        FROM (
            SELECT inv_id, COUNT(DISTINCT company_id) AS n
            FROM (
                SELECT ci.investor_id AS inv_id, ci.company_id
                FROM company_investors ci
                JOIN companies c ON c.id = ci.company_id
                WHERE c.exclusion_reason IS NULL
                UNION
                SELECT fri.investor_id AS inv_id, fr.company_id
                FROM funding_round_investors fri
                JOIN funding_rounds fr ON fr.id = fri.funding_round_id
                JOIN companies c ON c.id = fr.company_id
                WHERE c.exclusion_reason IS NULL
            ) u
            GROUP BY inv_id
        ) sub
        WHERE i.id = sub.inv_id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_investors_portfolio_count", table_name="investors")
    op.drop_column("investors", "portfolio_count")
