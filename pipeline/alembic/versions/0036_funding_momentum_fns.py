"""funding_by_quarter + industry_funding_momentum RPCs (industry pages / trends).

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-12 00:00:00.000000

Read-time aggregates for the industry landing pages (/industry/[group]) and the
macro /trends dashboard. These are catalog-wide GROUP BY rollups that PostgREST
cannot express through filter params AND that exceed its silent 1000-row select
cap on the largest industries — so, like similar_companies (0033) and
semantic_companies (0035), the aggregation lives in a STABLE SQL function the
web calls via .rpc(), never in an unbounded flat select.

Both apply the same "shown" catalog bar the web uses everywhere
(exclusion_reason IS NULL AND (description_short IS NOT NULL OR
funding_round_count > 0); see queries.ts CATALOG_BAR_OR) so an industry page's
numbers can never include companies the browse grid hides.

funding_by_quarter(p_quarters, p_industry_group DEFAULT NULL): total raised +
round count per calendar quarter over the last p_quarters quarters (INCLUDING
the in-progress one — it renders as the latest, partial bar on the chart).
p_industry_group scopes it to one bucket for the per-industry chart, or all
catalog companies when NULL for the /trends chart.

industry_funding_momentum(): per-industry trailing-2-complete-quarter raised
(recent) vs the 2 quarters before (prior) — the SAME window math as
compute_themes.funding_windows (recent = [now_quarter - 6mo, now_quarter);
prior = [now_quarter - 12mo, now_quarter - 6mo); the in-progress quarter is
excluded so a mid-quarter run never compares a partial window against full
ones). The web computes growth = (recent - prior) / prior itself (NULL when
prior is 0) and ranks the "hottest industries". company_count is intentionally
left to the web's existing per-industry head-count query rather than folded in
here (it is not a funding aggregate).

SECURITY INVOKER (the default) on purpose, same as 0033/0035: the web calls as
service_role; an anon PostgREST role hitting these yields zero rows via the
companies RLS (0027), not a leak.

No schema change — models.py untouched. Hand-written per the 0015+ convention
(autogenerate cannot emit CREATE FUNCTION).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FUNDING_BY_QUARTER_FN = """
CREATE FUNCTION funding_by_quarter(
    p_quarters integer,
    p_industry_group text DEFAULT NULL
)
RETURNS TABLE (
    quarter_start date,
    total_usd numeric,
    round_count integer
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT
        date_trunc('quarter', fr.announced_date)::date AS quarter_start,
        sum(fr.amount_raised) AS total_usd,
        count(*)::integer AS round_count
    FROM funding_rounds fr
    JOIN companies c ON c.id = fr.company_id
    WHERE c.exclusion_reason IS NULL
      AND (c.description_short IS NOT NULL OR c.funding_round_count > 0)
      AND fr.announced_date IS NOT NULL
      AND fr.amount_raised IS NOT NULL
      AND fr.announced_date >= (
          date_trunc('quarter', CURRENT_DATE)
          - make_interval(months => 3 * (p_quarters - 1))
      )
      AND (p_industry_group IS NULL OR c.industry_group = p_industry_group)
    GROUP BY 1
    ORDER BY 1
$$;
"""

_INDUSTRY_FUNDING_MOMENTUM_FN = """
CREATE FUNCTION industry_funding_momentum()
RETURNS TABLE (
    industry_group text,
    recent_usd numeric,
    prior_usd numeric,
    round_count integer
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    WITH bounds AS (
        SELECT
            date_trunc('quarter', CURRENT_DATE)::date AS recent_end,
            (date_trunc('quarter', CURRENT_DATE) - interval '6 months')::date
                AS recent_start,
            (date_trunc('quarter', CURRENT_DATE) - interval '12 months')::date
                AS prior_start
    )
    SELECT
        c.industry_group::text AS industry_group,
        sum(fr.amount_raised) FILTER (
            WHERE fr.announced_date >= b.recent_start
              AND fr.announced_date < b.recent_end
        ) AS recent_usd,
        sum(fr.amount_raised) FILTER (
            WHERE fr.announced_date >= b.prior_start
              AND fr.announced_date < b.recent_start
        ) AS prior_usd,
        count(*) FILTER (
            WHERE fr.announced_date >= b.recent_start
              AND fr.announced_date < b.recent_end
        )::integer AS round_count
    FROM funding_rounds fr
    JOIN companies c ON c.id = fr.company_id
    CROSS JOIN bounds b
    WHERE c.exclusion_reason IS NULL
      AND (c.description_short IS NOT NULL OR c.funding_round_count > 0)
      AND c.industry_group IS NOT NULL
      AND fr.announced_date IS NOT NULL
      AND fr.amount_raised IS NOT NULL
      AND fr.announced_date >= b.prior_start
      AND fr.announced_date < b.recent_end
    GROUP BY c.industry_group
$$;
"""


def upgrade() -> None:
    op.execute(_FUNDING_BY_QUARTER_FN)
    op.execute(_INDUSTRY_FUNDING_MOMENTUM_FN)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS industry_funding_momentum();")
    op.execute("DROP FUNCTION IF EXISTS funding_by_quarter(integer, text);")
