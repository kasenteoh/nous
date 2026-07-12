"""compute-themes: cluster company embeddings into named market themes (E-3).

Per ``industry_group`` with at least :data:`MIN_EMBEDDED_COMPANIES` shown +
embedded companies, KMeans-clusters the description embeddings (migration
0033), names each coherent cluster with one DeepSeek call, and materializes
the result into ``themes`` + ``company_themes`` (migration 0034) for the
/themes pages.

Clustering: KMeans over unit-normalized vectors (so euclidean ≈ cosine) with
``k = clamp(round(sqrt(n/2)), 2, 10)`` — the classic rule-of-thumb, capped
low so themes stay coarse, recognizable market segments rather than
micro-clusters (and so LLM spend stays bounded). KMeans over HDBSCAN on
purpose: at per-industry sizes of 8–300, HDBSCAN routinely labels most
points noise (no theme membership for half the catalog), while KMeans covers
every company and is deterministic with a fixed seed — which the
slug-stability contract below depends on. scikit-learn lives in the optional
``embeddings`` uv dependency group next to fastembed; the stage depends only
on the :class:`Clusterer` Protocol and tests inject a deterministic fake.

Slug stability (the ≥0.9 tolerance): each new cluster centroid is matched
greedily one-to-one against the industry's previous theme centroids at
cosine ≥ 0.9. A matched cluster KEEPS the previous theme's row — slug, name,
description, prompt_version — and only refreshes centroid, members, counts,
and funding metrics, with zero LLM cost. Unchanged embeddings re-cluster
identically (fixed seed, deterministic member order), match at cosine 1.0,
and converge: same clusters → same slugs → no rewrites beyond the metric
refresh. Below 0.9 the cluster is genuinely different content, so it gets a
new name + slug and the stale theme row is deleted (replace-per-industry).
The tolerance means slugs survive incremental catalog drift (new members
nudging a centroid) but not a real reshuffle — accepted for a monthly
aggregate surface whose inbound links are sitemap-driven.

Funding metrics (computed here, stored on the theme row, all derived from
stored + sourced ``funding_rounds`` — never a new unattributed number):
trailing-2-complete-calendar-quarter sum of member companies'
``amount_raised`` vs the 2 quarters before, by ``announced_date``.
``funding_growth = (recent − prior) / prior``; NULL when prior is 0.

Cost discipline: at most ``--limit`` clusters are LLM-named per run (default
100 ⇒ ~100 calls × ~1k tokens ≈ well under $0.05 on DeepSeek — pennies).
When the remaining budget can't cover an industry's new clusters, that whole
industry is deferred to the next run rather than half-replaced. A 429 stops
the run immediately (analyze-competitors pattern); industries already
committed stay.

TTL gate (monthly cadence without a monthly workflow): the stage runs from
weekly discovery.yml but exits immediately when ``MAX(themes.updated_at)``
is younger than ``ttl_days`` (default 25) — the same TTL idea as
analyze-competitors, lifted to stage level because themes are rebuilt as a
set, not per company. While the table is empty the gate never trips, so the
first successful build happens on the first weekly run with enough
embeddings, then settles into ~monthly.

Idempotency: re-running with unchanged embeddings (``--force`` to bypass the
TTL) converges — every cluster centroid-matches its own previous row, no LLM
calls are made, no rows are created or deleted, and only metrics/updated_at
refresh.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, CompanyTheme, FundingRound, Theme
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.theme_naming import (
    PROMPT_VERSION as THEME_PROMPT_VERSION,
)
from nous.llm.prompts.theme_naming import (
    ThemeNaming,
    build_prompt,
    format_members_block,
)

logger = logging.getLogger(__name__)

# An industry needs at least this many shown + embedded companies before
# clustering it. Rationale: k is at least 2 and a nameable theme needs
# MIN_CLUSTER_SIZE (3) members, so 8 is the smallest population where two
# coherent themes can plausibly coexist (2 clusters × ~4 members); below it
# the industry page itself is the better surface.
MIN_EMBEDDED_COMPANIES: int = 8

# Clusters smaller than this are dropped before naming: a 1–2 company
# "theme" is a thin page and statistically indistinguishable from noise.
# Matches the web's ≥3-member sitemap threshold.
MIN_CLUSTER_SIZE: int = 3

# k = clamp(round(sqrt(n/2)), 2, MAX_K) — see module docstring.
MAX_K: int = 10

# Cosine floor for matching a new cluster centroid to a previous theme's
# centroid (slug stability). See module docstring for the tolerance rationale.
CENTROID_MATCH_THRESHOLD: float = 0.9

DEFAULT_MAX_LLM_CLUSTERS: int = 100
DEFAULT_TTL_DAYS: int = 25


class Clusterer(Protocol):
    """The clustering seam: partition vectors into k groups.

    The stage depends only on this Protocol; the real scikit-learn adapter
    (:class:`KMeansClusterer`) is constructed in the CLI, and tests inject a
    deterministic fake so scikit-learn (optional ``embeddings`` group) is
    never required to run them.
    """

    def cluster(self, vectors: list[list[float]], k: int) -> list[int]:
        """Return one 0-based cluster label per input vector."""
        ...


class KMeansClusterer:
    """Real adapter over scikit-learn's KMeans.

    Deterministic on purpose (fixed ``random_state``, ``n_init=10``): the
    slug-stability contract needs unchanged inputs to produce identical
    clusters. Imports scikit-learn lazily so the module stays importable
    without the optional ``embeddings`` dependency group (lint CI, default
    local ``uv sync``).
    """

    def __init__(self) -> None:
        # Import check up front: the CLI constructs this eagerly so a missing
        # optional dependency group fails loudly at startup, not mid-run.
        import sklearn.cluster  # noqa: F401  # optional dep — see class docstring

    def cluster(self, vectors: list[list[float]], k: int) -> list[int]:
        from sklearn.cluster import KMeans

        model = KMeans(n_clusters=k, random_state=0, n_init=10)
        labels = model.fit_predict(vectors)
        return [int(label) for label in labels]


class ComputeThemesSummary(BaseModel):
    """Result of one compute-themes run."""

    skipped_ttl: bool = False  # gate tripped — nothing was touched
    industries_seen: int = 0  # industries meeting MIN_EMBEDDED_COMPANIES
    industries_processed: int = 0  # industries whose writes committed
    industries_deferred_cap: int = 0  # skipped whole: LLM budget exhausted
    clusters_found: int = 0  # clusters meeting MIN_CLUSTER_SIZE
    clusters_small_dropped: int = 0  # below MIN_CLUSTER_SIZE
    clusters_incoherent_dropped: int = 0  # LLM returned null (no fabrication)
    themes_matched: int = 0  # kept slug via centroid match (no LLM)
    themes_created: int = 0  # newly named + inserted
    themes_deleted: int = 0  # previous themes no new cluster matched
    memberships_written: int = 0  # company_themes rows inserted
    llm_calls: int = 0  # naming calls issued
    llm_failures: int = 0  # parse/other LLM errors (cluster skipped)
    skipped_rate_limited: int = 0  # 1 when a 429 stopped the run


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a DB)
# ---------------------------------------------------------------------------


def choose_k(n: int) -> int:
    """Cluster count for n companies: clamp(round(sqrt(n/2)), 2, MAX_K)."""
    return max(2, min(MAX_K, round(math.sqrt(n / 2))))


def unit_normalize(vector: list[float]) -> list[float]:
    """Scale to unit length (zero vectors pass through unchanged)."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def centroid_of(unit_vectors: list[list[float]]) -> list[float]:
    """Unit-normalized mean of already-unit vectors (the theme centroid)."""
    dim = len(unit_vectors[0])
    mean = [
        sum(vec[i] for vec in unit_vectors) / len(unit_vectors) for i in range(dim)
    ]
    return unit_normalize(mean)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity (inputs need not be unit length)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def theme_slug(name: str) -> str:
    """URL slug for a theme name: NFKD → ascii lowercase → hyphenated.

    Deliberately NOT ``util.slugify.slugify``: that helper strips corporate
    suffixes ("Co", "Inc") which are legitimate words in theme names
    ("Payroll Co-pilots" must not become "payroll"). Falls back to "theme"
    for a name with no alphanumerics so the slug is never empty.
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = _SLUG_STRIP.sub("-", ascii_name.lower()).strip("-")
    return slug or "theme"


def quarter_start(day: date) -> date:
    """First day of the calendar quarter containing ``day``."""
    quarter_month = 3 * ((day.month - 1) // 3) + 1
    return date(day.year, quarter_month, 1)


def _shift_quarters(q_start: date, quarters: int) -> date:
    """A quarter-start date moved ``quarters`` quarters back."""
    total_months = q_start.year * 12 + (q_start.month - 1) - 3 * quarters
    return date(total_months // 12, total_months % 12 + 1, 1)


def funding_windows(today: date) -> tuple[date, date, date]:
    """(prior_start, recent_start, recent_end) for the growth metric.

    ``recent`` = the 2 most recent COMPLETE calendar quarters —
    [recent_start, recent_end) where recent_end is the current quarter's
    first day (the in-progress quarter is excluded so mid-quarter runs don't
    compare a partial window against full ones). ``prior`` = the 2 quarters
    before — [prior_start, recent_start).
    """
    recent_end = quarter_start(today)
    recent_start = _shift_quarters(recent_end, 2)
    prior_start = _shift_quarters(recent_end, 4)
    return prior_start, recent_start, recent_end


def compute_funding_metrics(
    rounds: list[tuple[date | None, Decimal | None]],
    *,
    today: date,
) -> tuple[Decimal, Decimal, Decimal | None]:
    """(recent_sum, prior_sum, growth) over (announced_date, amount) rounds.

    Rounds without a date or amount contribute nothing (they cannot be
    placed in a window — unknown stays unknown, never guessed into one).
    ``growth = (recent − prior) / prior``, quantized to 4 dp; None when
    prior is 0 (undefined growth over a zero base — the web derives a "new
    funding" label from the sums instead of a fabricated infinity).
    """
    prior_start, recent_start, recent_end = funding_windows(today)
    recent = Decimal("0")
    prior = Decimal("0")
    for announced, amount in rounds:
        if announced is None or amount is None:
            continue
        if recent_start <= announced < recent_end:
            recent += amount
        elif prior_start <= announced < recent_start:
            prior += amount
    if prior == 0:
        return recent, prior, None
    growth = ((recent - prior) / prior).quantize(Decimal("0.0001"))
    return recent, prior, growth


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _ttl_gate_passed(
    session: AsyncSession, *, ttl_days: int, now: datetime
) -> bool:
    """True when the stage should run: no theme was built within the TTL."""
    last_built = (
        await session.execute(select(func.max(Theme.updated_at)))
    ).scalar_one_or_none()
    if last_built is None:
        return True
    return last_built < now - timedelta(days=ttl_days)


async def _eligible_industries(session: AsyncSession) -> list[str]:
    """industry_groups with ≥ MIN_EMBEDDED_COMPANIES shown+embedded companies,
    alphabetical for deterministic run order."""
    stmt = (
        select(Company.industry_group)
        .where(
            Company.exclusion_reason.is_(None),
            Company.embedding.is_not(None),
            Company.industry_group.is_not(None),
        )
        .group_by(Company.industry_group)
        .having(func.count(Company.id) >= MIN_EMBEDDED_COMPANIES)
        .order_by(Company.industry_group)
    )
    return [row for row in (await session.execute(stmt)).scalars() if row]


async def _fetch_members(session: AsyncSession, industry: str) -> list[Company]:
    """Shown + embedded companies of one industry, id-ordered (deterministic
    input order is part of the KMeans reproducibility contract)."""
    stmt = (
        select(Company)
        .where(
            Company.exclusion_reason.is_(None),
            Company.embedding.is_not(None),
            Company.industry_group == industry,
        )
        .order_by(Company.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _fetch_member_rounds(
    session: AsyncSession, company_ids: list[UUID], since: date
) -> list[tuple[date | None, Decimal | None]]:
    """(announced_date, amount_raised) for the member companies' rounds in the
    metric horizon. ``since`` bounds the scan to the 4-quarter window."""
    if not company_ids:
        return []
    stmt = select(FundingRound.announced_date, FundingRound.amount_raised).where(
        FundingRound.company_id.in_(company_ids),
        FundingRound.announced_date >= since,
    )
    rows = (await session.execute(stmt)).all()
    return [(row.announced_date, row.amount_raised) for row in rows]


async def _existing_slugs(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(Theme.slug))).scalars().all())


def _unique_slug(base: str, taken: set[str]) -> str:
    """``base``, or ``base-2``/``base-3``/… when already taken. Mutates
    ``taken`` so successive calls within one run can't collide either."""
    slug = base
    counter = 2
    while slug in taken:
        slug = f"{base}-{counter}"
        counter += 1
    taken.add(slug)
    return slug


# ---------------------------------------------------------------------------
# Clustering + matching (pure given fetched data)
# ---------------------------------------------------------------------------


class _Cluster:
    """One ≥MIN_CLUSTER_SIZE cluster: members + centroid, pre-persistence."""

    __slots__ = ("members", "centroid", "similarities")

    def __init__(
        self,
        members: list[Company],
        centroid: list[float],
        similarities: list[float],
    ) -> None:
        self.members = members
        self.centroid = centroid
        self.similarities = similarities  # member-to-centroid, aligned


def _embedding_list(company: Company) -> list[float]:
    """Coerce the pgvector value (numpy array at runtime) to list[float].

    Deliberately `is not None` rather than truthiness: an ndarray's __bool__
    raises on >1 element, so `embedding or []` would crash.
    """
    emb = company.embedding
    return [float(x) for x in emb] if emb is not None else []


def _build_clusters(
    companies: list[Company], unit_vectors: list[list[float]], labels: list[int]
) -> tuple[list[_Cluster], int]:
    """Group companies by label into _Clusters; returns (clusters, dropped).

    Members are ordered most-representative-first (similarity to centroid,
    descending, id as tiebreak) — the order the prompt and the member table
    both want.
    """
    by_label: dict[int, list[tuple[Company, list[float]]]] = {}
    for company, vec, label in zip(companies, unit_vectors, labels, strict=True):
        by_label.setdefault(label, []).append((company, vec))

    clusters: list[_Cluster] = []
    dropped = 0
    for label in sorted(by_label):
        group = by_label[label]
        if len(group) < MIN_CLUSTER_SIZE:
            dropped += 1
            continue
        centroid = centroid_of([vec for _, vec in group])
        scored = sorted(
            group,
            key=lambda item: (-cosine_similarity(item[1], centroid), item[0].id),
        )
        clusters.append(
            _Cluster(
                members=[company for company, _ in scored],
                centroid=centroid,
                similarities=[
                    cosine_similarity(vec, centroid) for _, vec in scored
                ],
            )
        )
    return clusters, dropped


def match_clusters_to_themes(
    centroids: list[list[float]],
    previous: list[tuple[UUID, list[float]]],
    *,
    threshold: float = CENTROID_MATCH_THRESHOLD,
) -> dict[int, UUID]:
    """Greedy one-to-one matching: cluster index → previous theme id.

    All (cluster, theme) pairs at cosine ≥ threshold are considered in
    descending-similarity order; each cluster and each theme is used at most
    once. Greedy is exact enough here — matches at play are ≥0.9 and real
    collisions (two clusters both ≥0.9 to one theme) mean the content truly
    reshuffled, where either assignment is defensible.
    """
    scored: list[tuple[float, int, UUID]] = []
    for idx, centroid in enumerate(centroids):
        for theme_id, prev_centroid in previous:
            sim = cosine_similarity(centroid, [float(x) for x in prev_centroid])
            if sim >= threshold:
                scored.append((sim, idx, theme_id))
    scored.sort(key=lambda item: -item[0])

    assigned: dict[int, UUID] = {}
    used_themes: set[UUID] = set()
    for _, idx, theme_id in scored:
        if idx in assigned or theme_id in used_themes:
            continue
        assigned[idx] = theme_id
        used_themes.add(theme_id)
    return assigned


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _name_cluster(cluster: _Cluster, industry: str) -> ThemeNaming:
    """One DeepSeek naming call for a cluster (raises LLM errors upward)."""
    members_block = format_members_block(
        [(c.name, c.description_short) for c in cluster.members]
    )
    prompt = build_prompt(industry_group=industry, members_block=members_block)
    return await complete_json(prompt, ThemeNaming)


async def run_compute_themes(
    session: AsyncSession,
    clusterer: Clusterer,
    *,
    max_llm_clusters: int = DEFAULT_MAX_LLM_CLUSTERS,
    ttl_days: int = DEFAULT_TTL_DAYS,
    force: bool = False,
    today: date | None = None,
) -> ComputeThemesSummary:
    """Build/refresh the themes tables. See the module docstring for the
    full contract; per-industry writes commit incrementally (a 429 or crash
    keeps every industry committed so far, and the TTL gate then holds the
    partial result until the next monthly window)."""
    summary = ComputeThemesSummary()
    now = datetime.now(UTC)
    metric_day = today or now.date()

    if not force and not await _ttl_gate_passed(session, ttl_days=ttl_days, now=now):
        summary.skipped_ttl = True
        logger.info(
            "compute-themes: themes built less than %d days ago — skipping "
            "(monthly cadence via weekly workflow).",
            ttl_days,
        )
        return summary

    industries = await _eligible_industries(session)
    summary.industries_seen = len(industries)
    if not industries:
        return summary

    taken_slugs = await _existing_slugs(session)
    llm_budget = max(0, max_llm_clusters)
    prior_start, _, _ = funding_windows(metric_day)

    for industry in industries:
        companies = await _fetch_members(session, industry)
        vectors = [unit_normalize(_embedding_list(c)) for c in companies]
        k = choose_k(len(companies))
        labels = clusterer.cluster(vectors, k)
        clusters, dropped = _build_clusters(companies, vectors, labels)
        summary.clusters_small_dropped += dropped
        summary.clusters_found += len(clusters)
        if not clusters:
            continue

        previous = list(
            (
                await session.execute(
                    select(Theme).where(Theme.industry_group == industry)
                )
            )
            .scalars()
            .all()
        )
        matched = match_clusters_to_themes(
            [cluster.centroid for cluster in clusters],
            [(theme.id, [float(x) for x in theme.centroid]) for theme in previous],
        )

        unmatched_count = len(clusters) - len(matched)
        if unmatched_count > llm_budget:
            # Deferring the WHOLE industry keeps replace-per-industry atomic:
            # half-naming its clusters would delete stale themes without
            # writing their successors. The next (post-TTL) run picks it up.
            summary.industries_deferred_cap += 1
            logger.info(
                "compute-themes: deferring %s (%d new clusters > %d LLM budget "
                "left).",
                industry,
                unmatched_count,
                llm_budget,
            )
            continue

        # --- Name the unmatched clusters (LLM), before any DB writes for
        # this industry so a failure leaves it untouched.
        named: dict[int, ThemeNaming] = {}
        rate_limited = False
        for idx, cluster in enumerate(clusters):
            if idx in matched:
                continue
            try:
                naming = await _name_cluster(cluster, industry)
                summary.llm_calls += 1
                llm_budget -= 1
            except LLMRateLimitError:
                logger.warning(
                    "compute-themes: LLM rate limit while naming a %s cluster "
                    "— stopping run (industries committed so far stay).",
                    industry,
                )
                rate_limited = True
                break
            except (LLMParseError, LLMError) as exc:
                logger.warning(
                    "compute-themes: LLM error naming a %s cluster — dropping "
                    "it this run: %s",
                    industry,
                    exc,
                )
                summary.llm_failures += 1
                continue
            if naming.name is None:
                # Incoherent cluster — dropped entirely, never a made-up
                # theme (null-over-fabricate).
                summary.clusters_incoherent_dropped += 1
                continue
            named[idx] = naming
        if rate_limited:
            summary.skipped_rate_limited = 1
            break

        # --- Persist this industry in one transaction (replace-style).
        theme_by_id = {theme.id: theme for theme in previous}
        matched_theme_ids = set(matched.values())
        async with session.begin_nested():
            for idx, cluster in enumerate(clusters):
                if idx in matched:
                    theme = theme_by_id[matched[idx]]
                elif idx in named:
                    naming = named[idx]
                    assert naming.name is not None  # filtered above
                    theme = Theme(
                        slug=_unique_slug(theme_slug(naming.name), taken_slugs),
                        name=naming.name,
                        industry_group=industry,
                        description=naming.description,
                        centroid=cluster.centroid,
                        company_count=0,  # set below
                        prompt_version=THEME_PROMPT_VERSION,
                    )
                    session.add(theme)
                    await session.flush()  # assign theme.id for memberships
                    summary.themes_created += 1
                else:
                    continue  # incoherent / LLM-failed cluster: no row

                member_ids = [c.id for c in cluster.members]
                rounds = await _fetch_member_rounds(
                    session, member_ids, prior_start
                )
                recent, prior_sum, growth = compute_funding_metrics(
                    rounds, today=metric_day
                )
                theme.centroid = cluster.centroid
                theme.company_count = len(cluster.members)
                theme.funding_recent_usd = recent
                theme.funding_prior_usd = prior_sum
                theme.funding_growth = growth
                # Explicit stamp (not just onupdate): the TTL gate reads
                # MAX(updated_at), and a metrics-identical refresh must still
                # advance it or the gate would re-run weekly forever.
                theme.updated_at = now
                if idx in matched:
                    summary.themes_matched += 1

                await session.execute(
                    delete(CompanyTheme).where(CompanyTheme.theme_id == theme.id)
                )
                for company_id, similarity in zip(
                    member_ids, cluster.similarities, strict=True
                ):
                    session.add(
                        CompanyTheme(
                            theme_id=theme.id,
                            company_id=company_id,
                            similarity=similarity,
                        )
                    )
                    summary.memberships_written += 1

            # Previous themes no new cluster matched are stale content —
            # delete (memberships cascade).
            for theme in previous:
                if theme.id not in matched_theme_ids:
                    await session.delete(theme)
                    summary.themes_deleted += 1
        await session.commit()
        summary.industries_processed += 1

    logger.info(
        "compute-themes: industries=%d/%d clusters=%d matched=%d created=%d "
        "deleted=%d incoherent=%d llm_calls=%d",
        summary.industries_processed,
        summary.industries_seen,
        summary.clusters_found,
        summary.themes_matched,
        summary.themes_created,
        summary.themes_deleted,
        summary.clusters_incoherent_dropped,
        summary.llm_calls,
    )
    return summary
