"""refetch-article-text — heal thin / interstitial news_articles.raw_content.

News surfaced through Google News RSS carries ``news.google.com`` redirect URLs
whose stored ``raw_content`` is often an interstitial stub (or, on a resolution
miss at ingest time, just the headline + snippet). That thin text starves
describe-fallback evidence, the entity guard, and funding extraction. Ingest now
resolves NEW rows to their publisher body (``resolve_and_fetch_article_text``),
but the historical backlog — and any row whose resolution failed then — still
holds junk. This stage drains that backlog at the DATA layer.

Selection: a row is a candidate when its URL is a bare ``news.google.com`` link
OR its stored ``raw_content`` is shorter than ``MIN_BODY_CHARS`` (either signals
"probably not real article text"), AND it has never been attempted
(``text_refetched_at IS NULL``). Candidates are prominence-ordered by the owning
company (``latest_round_amount DESC NULLS LAST``) and capped at ``--limit`` so a
small run heals marquee coverage first.

Per row (apply): ``resolve_and_fetch_article_text`` resolves the redirect and
fetches the publisher page politely (robots.txt honored, contact-email UA, the
shared 1 req/s per-domain throttle, SSRF guard, thin-body cutoff — all inside
the shared helper). On healed text (``>= MIN_BODY_CHARS``) the row's
``raw_content`` is overwritten and stamped. The URL is NEVER touched — it is the
canonical-dedup identity. A thin / failed / robots-blocked fetch is stamped
anyway (we tried; don't re-bill a live fetch every run — a future need can clear
the stamp), so the backlog drains monotonically and re-runs at the same limit
select nothing (idempotent).

``--dry-run`` (default) reports the selection size + a sample of candidate URLs
and makes NO network calls and NO writes — a $-free size-the-work gate before an
apply run spends fetches. Etiquette: 1 req/s per domain (the shared throttle)
plus a ``--max-runtime-minutes`` wall-clock budget, since publisher domains vary
(the throttle rarely binds) and a bounded lever must stop cleanly.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, NewsArticle
from nous.sources.news import (
    _GOOGLE_NEWS_HOST,
    MIN_BODY_CHARS,
    NewsClient,
    resolve_and_fetch_article_text,
)

logger = logging.getLogger(__name__)

# How many candidate URLs to list in the dry-run summary (a spot-check sample,
# not the whole selection — the count carries the size signal).
_DRY_RUN_SAMPLE_SIZE = 20


class RefetchArticleTextSummary(BaseModel):
    """Stage summary — feeds record_pipeline_run and the yield table."""

    dry_run: bool
    selected: int = 0  # candidates processed (apply) / matched (dry-run)
    refetched: int = 0  # raw_content healed with real publisher text
    # Tried but no usable text — robots-block, 4xx/5xx, network/SSRF, a GN link
    # that never left the consent interstitial, or a body below MIN_BODY_CHARS.
    # The shared helper collapses those causes to one "no text" outcome, so they
    # share a bucket; the row is stamped regardless (never re-billed).
    failed_fetch: int = 0
    stopped_early: bool = False  # hit the wall-clock budget mid-run
    sample_urls: list[str] = Field(default_factory=list)  # dry-run only


def _candidate_stmt(limit: int) -> Select[tuple[NewsArticle]]:
    """Prominence-ordered candidate rows: a GN-host URL OR thin stored text, and
    never yet attempted. Joined to companies for the prominence ordering."""
    gn_host = NewsArticle.url.like(f"https://{_GOOGLE_NEWS_HOST}/%")
    thin = func.length(NewsArticle.raw_content) < MIN_BODY_CHARS
    return (
        select(NewsArticle)
        .join(Company, Company.id == NewsArticle.company_id)
        .where(
            or_(gn_host, thin),
            NewsArticle.text_refetched_at.is_(None),
        )
        .order_by(
            Company.latest_round_amount.desc().nulls_last(),
            NewsArticle.id,
        )
        .limit(limit)
    )


async def run_refetch_article_text(
    session: AsyncSession,
    *,
    user_agent: str = "",
    limit: int = 50,
    max_runtime_minutes: float | None = None,
    dry_run: bool = True,
) -> RefetchArticleTextSummary:
    """Heal thin / interstitial article text from the publisher page.

    ``dry_run`` (default) selects the candidate slice and reports its size + a
    URL sample, making no network calls and no writes. Apply
    (``dry_run=False``, requires a contact-email ``user_agent``) fetches each
    candidate through ``resolve_and_fetch_article_text``, overwrites
    ``raw_content`` on a healed body, stamps ``text_refetched_at`` on every
    attempt, and commits per row (a mid-run stop leaves a clean, resumable
    state). ``max_runtime_minutes`` stops cleanly at the next row boundary.
    """
    summary = RefetchArticleTextSummary(dry_run=dry_run)
    candidates = list(
        (await session.execute(_candidate_stmt(limit))).scalars().all()
    )
    summary.selected = len(candidates)

    if dry_run:
        summary.sample_urls = [a.url for a in candidates[:_DRY_RUN_SAMPLE_SIZE]]
        logger.info(
            "refetch-article-text: dry-run selected=%d (no fetches, no writes)",
            summary.selected,
        )
        return summary

    if not user_agent:
        raise ValueError("apply mode requires a contact-email user_agent")

    started = time.monotonic()
    deadline = (
        started + max_runtime_minutes * 60 if max_runtime_minutes is not None else None
    )

    async with NewsClient(user_agent) as client:
        for article in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                summary.stopped_early = True
                logger.info(
                    "refetch-article-text: %.0f-min budget reached — stopping "
                    "(%d of %d processed)",
                    max_runtime_minutes or 0,
                    summary.refetched + summary.failed_fetch,
                    summary.selected,
                )
                break

            _resolved_url, text = await resolve_and_fetch_article_text(
                client, article.url
            )
            now = datetime.now(tz=UTC)
            if text is not None:
                # Heal the body; leave the URL untouched (dedup identity).
                article.raw_content = text
                article.text_refetched_at = now
                summary.refetched += 1
            else:
                # Tried and got nothing usable — stamp so we don't re-fetch it
                # every run. Clearing the stamp later forces a retry.
                article.text_refetched_at = now
                summary.failed_fetch += 1
            session.add(article)
            await session.commit()

    logger.info(
        "refetch-article-text: selected=%d refetched=%d failed_fetch=%d "
        "stopped_early=%s dry_run=%s",
        summary.selected,
        summary.refetched,
        summary.failed_fetch,
        summary.stopped_early,
        dry_run,
    )
    return summary


def render_refetch_table(summary: RefetchArticleTextSummary) -> str:
    """Render the run as GitHub-flavored markdown for the Actions step summary."""
    lines: list[str] = []
    mode = "dry-run" if summary.dry_run else "apply"
    lines.append(f"## refetch-article-text — {mode}")
    lines.append("")
    if summary.dry_run:
        lines.append(
            f"- **Candidates selected (would fetch):** {summary.selected}"
        )
        lines.append("- **Writes:** none (dry-run). **Network:** none.")
        lines.append("")
        lines.append(
            "Selection: a `news.google.com` URL OR stored text shorter than "
            f"{MIN_BODY_CHARS} chars, never yet attempted "
            "(`text_refetched_at IS NULL`), prominence-ordered by the owning "
            "company. Run `--apply` to heal these from the publisher pages."
        )
        if summary.sample_urls:
            lines.append("")
            lines.append("### Sample candidate URLs")
            for url in summary.sample_urls:
                lines.append(f"- {url}")
        return "\n".join(lines)

    healed_pct = (
        (summary.refetched / summary.selected * 100) if summary.selected else 0.0
    )
    lines.append(f"- **Candidates processed:** {summary.selected}")
    lines.append(
        f"- **Healed (raw_content refreshed):** {summary.refetched} "
        f"({healed_pct:.0f}%)"
    )
    lines.append(
        f"- **Failed / thin / robots (stamped, not re-billed):** "
        f"{summary.failed_fetch}"
    )
    if summary.stopped_early:
        lines.append(
            "- **Stopped early:** wall-clock budget reached — the rest is picked "
            "up by the next run."
        )
    lines.append("- **Cost:** $0 (polite publisher re-fetches; no LLM).")
    return "\n".join(lines)
