"""discover-github-trending pipeline stage.

GitHub-trending → company discovery: devtools companies (the Supabase class)
trend on GitHub months before funding news covers them. The stage maps each
trending repo to its owning account, filters out what is clearly not a
company, and gates every remaining candidate through an LLM judgment before
it may enter the catalog via :func:`auto_create_company`.

Pipeline per owner (cheapest checks first, so re-runs cost ~nothing):

1. **Existing-company skip** (free): an owner whose login already matches a
   catalog row (exact normalized name or trigram) is skipped before any API
   or LLM spend — the steady-state weekly re-run re-judges only NEW owners.
2. **Profile fetch** (one REST call): ``/users/{login}`` supplies the
   org-vs-user distinction the trending HTML lacks. ``type == "User"`` is
   skipped (personal account — detectable case); a failed fetch degrades to
   judging from page signals alone rather than blocking.
3. **Profile-based skip** (free): the profile's display name / website
   domain may match an existing row under a different login — skip those too.
4. **LLM judgment gate** (paid, bounded): "is this plausibly a US software
   COMPANY with a product?" — null/false means no row, ever. Bounded at
   ``limit`` judgments per run (default :data:`MAX_LLM_JUDGMENTS_PER_RUN`,
   ~25 × ~700 tokens ≈ well under a cent per weekly run).
5. **Auto-create**: accepted candidates flow through the standard
   find-or-create (domain dedup + fuzzy name match), so re-runs and
   cross-source rediscovery never duplicate. ``discovered_via`` is the
   distinct ``github_trending`` slug (underscore per the existing
   ``vc_portfolio`` / ``crunchbase_news`` facet convention).

Commit cadence: one commit per accepted owner, mirroring
``refresh_vc_portfolios`` — a mid-run crash leaves the DB clean.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.upsert import (
    auto_create_company,
    find_company_by_domain,
    find_company_by_name,
)
from nous.llm.client import LLMError, complete_json
from nous.llm.prompts.github_trending_company import (
    PROMPT_VERSION,
    TrendingCompanyJudgment,
    build_prompt,
    format_repos_block,
)
from nous.sources.github_trending import (
    GitHubOwnerProfile,
    TrendingRepo,
    fetch_owner_profile,
    fetch_trending_repos,
)
from nous.sources.news import NewsClient
from nous.util.url import is_storable_website

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_LLM_JUDGMENTS_PER_RUN",
    "DiscoverGithubTrendingSummary",
    "run_discover_github_trending",
]

# discovered_via facet slug for companies this stage creates. Underscored for
# consistency with the existing facet values (vc_portfolio, crunchbase_news).
DISCOVERED_VIA_SLUG = "github_trending"

# Per-run ceiling on LLM judgments — the stage's entire DeepSeek spend bound.
# The trending page lists ~25 repos, so this is effectively "judge at most one
# page of NEW owners"; at ~700 tokens/judgment a full run costs well under one
# cent. CLI --limit overrides per run.
MAX_LLM_JUDGMENTS_PER_RUN = 25


class DiscoverGithubTrendingSummary(BaseModel):
    """Outcome of one ``discover-github-trending`` run."""

    repos_seen: int = 0
    owners_seen: int = 0
    owners_skipped_existing: int = 0
    """Owner already matched a catalog row (pre- or post-profile) — no LLM."""
    owners_skipped_personal: int = 0
    """Profile said type=User (personal account) — no LLM."""
    owners_judged: int = 0
    owners_accepted: int = 0
    owners_rejected: int = 0
    """LLM said is_company=false."""
    owners_uncertain: int = 0
    """LLM said null — treated exactly like a rejection (no row)."""
    llm_failures: int = 0
    companies_created: int = 0
    companies_matched: int = 0
    """Accepted candidates that auto-create matched to an existing row."""
    prompt_version: str = PROMPT_VERSION


def _group_repos_by_owner(
    repos: list[TrendingRepo],
) -> dict[str, list[TrendingRepo]]:
    """Group trending repos by owner login, preserving page order."""
    by_owner: dict[str, list[TrendingRepo]] = {}
    for repo in repos:
        by_owner.setdefault(repo.owner, []).append(repo)
    return by_owner


def _candidate_website(profile: GitHubOwnerProfile | None) -> str | None:
    """The org's self-declared website (profile ``blog``), when storable.

    Feeding it to auto-create makes domain dedup work across sources (a VC
    portfolio and this stage discovering the same company collapse to one
    row). resolve-homepages still verifies/canonicalizes it downstream.
    """
    if profile is None or not profile.blog:
        return None
    blog = profile.blog.strip()
    return blog if blog and is_storable_website(blog) else None


async def _judge_owner(
    owner: str,
    repos: list[TrendingRepo],
    profile: GitHubOwnerProfile | None,
) -> TrendingCompanyJudgment:
    """One LLM judgment for ``owner``. Raises LLMError upward."""
    prompt = build_prompt(
        owner_login=owner,
        account_type=profile.type if profile is not None else None,
        profile_name=profile.name if profile is not None else None,
        profile_website=profile.blog if profile is not None else None,
        profile_bio=profile.bio if profile is not None else None,
        repos_block=format_repos_block(
            [(r.name, r.description, r.language, r.stars) for r in repos]
        ),
    )
    return await complete_json(prompt, TrendingCompanyJudgment)


async def run_discover_github_trending(
    session: AsyncSession,
    client: NewsClient,
    *,
    github_token: str = "",
    limit: int = MAX_LLM_JUDGMENTS_PER_RUN,
    similarity_threshold: float = 0.85,
) -> DiscoverGithubTrendingSummary:
    """Fetch trending, judge new owners, auto-create accepted companies.

    Args:
        session: Open async session; the stage commits per accepted owner.
        client: An entered :class:`NewsClient` (robots + throttle + UA) used
            for both the trending page and the GitHub REST profile calls.
        github_token: Optional GitHub REST token (in CI the built-in Actions
            token). Empty is fine — 25 unauthenticated profile calls sit far
            under the 60 req/h anonymous ceiling.
        limit: Max LLM judgments this run (spend bound).
        similarity_threshold: pg_trgm threshold forwarded to the existing-
            company checks and :func:`auto_create_company`.
    """
    summary = DiscoverGithubTrendingSummary()

    repos = await fetch_trending_repos(client)
    summary.repos_seen = len(repos)
    if not repos:
        logger.warning("discover-github-trending: no trending repos parsed")
        return summary

    for owner, owner_repos in _group_repos_by_owner(repos).items():
        summary.owners_seen += 1

        try:
            # 1. Free skip: the login already names a catalog row.
            existing = await find_company_by_name(
                session, owner, similarity_threshold=similarity_threshold
            )
            if existing is not None:
                summary.owners_skipped_existing += 1
                continue

            if summary.owners_judged >= limit:
                # Spend bound reached; remaining owners wait for next week's
                # run (the page churns, so genuinely new orgs come back).
                continue

            # 2. Profile: org-vs-user + display name/website/bio for the gate.
            profile = await fetch_owner_profile(
                client, owner, github_token=github_token
            )
            if profile is not None and profile.type.lower() == "user":
                summary.owners_skipped_personal += 1
                continue

            # 3. Free skip on profile identity (name under different login,
            #    or an already-known website domain).
            if profile is not None:
                if profile.name:
                    existing = await find_company_by_name(
                        session,
                        profile.name,
                        similarity_threshold=similarity_threshold,
                    )
                if existing is None:
                    existing = await find_company_by_domain(
                        session, _candidate_website(profile)
                    )
                if existing is not None:
                    summary.owners_skipped_existing += 1
                    continue

            # 4. The LLM gate.
            summary.owners_judged += 1
            try:
                judgment = await _judge_owner(owner, owner_repos, profile)
            except LLMError as exc:
                summary.llm_failures += 1
                logger.warning(
                    "discover-github-trending: LLM judgment failed for %s: %s",
                    owner,
                    exc,
                )
                continue

            if judgment.is_company is not True:
                if judgment.is_company is False:
                    summary.owners_rejected += 1
                else:
                    summary.owners_uncertain += 1
                logger.info(
                    "discover-github-trending: %s not accepted (%s): %s",
                    owner,
                    "rejected" if judgment.is_company is False else "uncertain",
                    judgment.reason,
                )
                continue

            # 5. Accepted → the standard idempotent find-or-create.
            summary.owners_accepted += 1
            name = (judgment.company_name or "").strip()
            if not name:
                name = (profile.name or "").strip() if profile is not None else ""
            if not name:
                name = owner
            company, created = await auto_create_company(
                session,
                name=name,
                website=_candidate_website(profile),
                discovered_via=DISCOVERED_VIA_SLUG,
                similarity_threshold=similarity_threshold,
            )
            if created:
                summary.companies_created += 1
            else:
                summary.companies_matched += 1
            await session.commit()
            logger.info(
                "discover-github-trending: %s %s as %r (%s)",
                owner,
                "created" if created else "matched existing",
                name,
                company.slug,
            )
        except Exception:  # noqa: BLE001 — per-owner isolation, keep sweeping
            logger.exception(
                "discover-github-trending: owner %s failed; continuing", owner
            )
            await session.rollback()

    return summary
