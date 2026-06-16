"""name-quality stage — conservative, CASING-ONLY company-name repair (zero LLM).

Some company names enter the catalog with degenerate casing: a VC-portfolio
sitemap/slug adapter yields ``Docusign`` (Kleiner Perkins' sitemap) when the
real brand is ``DocuSign``, or a logo-alt scrape yields ``AIRBNB`` /
``airbnb``.  The company's OWN homepage almost always carries the correctly
cased brand in its ``<title>`` (and/or ``og:site_name``).  scrape-homepages
already stores that signal: ``extract_visible_text`` PREPENDS the page title
and SEO meta tags (og:title / og:site_name / description) as the first line(s)
of ``RawPage.content`` (see ``nous.util.text.extract_visible_text``), so the
brand is on record without any new fetch.

This stage reads each company's homepage ``RawPage``, derives a candidate
display name from that stored title/og line, and — only when the candidate is
an *unambiguous pure-casing variant* of the current name — upgrades
``company.name`` to the better-cased form.

Conservatism (this is the whole point — a wrong rewrite is user-facing):

  - The candidate must **normalize to the SAME ``normalized_name``** as the
    current name (``util.slugify.normalize_name``), so it is unambiguously the
    same company.
  - On top of that, the candidate must be a **pure casing variant**:
    ``candidate.casefold() == current.casefold()``.  That guarantees the
    letters, spacing and punctuation are identical and ONLY the case differs —
    so the stage can never swap in a different word ("Acme" → "Globex" is
    rejected even if both somehow normalized alike).
  - The candidate must be *materially better* cased than the current name:
    either the current name is degenerate (all-lowercase or all-uppercase) and
    the candidate is not, or they are pure casing variants of each other and
    the candidate is the non-degenerate one.  An already-well-cased name is a
    no-op.

It never touches ``slug`` or ``normalized_name`` (both already lowercased, so
a casing fix leaves them correct).  Idempotent: once a name is upgraded the
candidate equals the stored name and the row is skipped on re-run.  ``dry_run``
logs the intended upgrades without writing.

When the stored homepage content has no usable title/brand line (the candidate
cannot be derived, or it does not normalize to the same company), the company
is simply skipped — the stage is a safe no-op on missing/unusable data.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company, RawPage
from nous.util.slugify import normalize_name, strip_corporate_suffix
from nous.util.title_subject import _leading_segment, _strip_leading_boilerplate
from nous.util.url import canonical_domain

logger = logging.getLogger(__name__)


class NameQualitySummary(BaseModel):
    """Result of one name-quality run."""

    companies_seen: int = 0  # companies with a homepage RawPage considered
    candidates_derived: int = 0  # rows where a usable title brand was extracted
    names_upgraded: int = 0  # rows whose casing was actually improved


def _candidate_from_content(content: str) -> str | None:
    """Derive a candidate brand name from a stored RawPage's content.

    ``extract_visible_text`` prepends the page ``<title>`` / og meta as the
    first line(s) of the stored content, so the brand lives on the FIRST
    non-empty line.  We take that line, keep its leading brand segment (the
    part before the first ``"<Brand> — tagline"`` / ``"<Brand> | Section"``
    separator), drop any "Welcome to" / "Home" boilerplate, and strip a
    corporate suffix.  Returns the cleaned candidate, or None when no usable
    first line exists.

    Pure and I/O-free, so it is unit-testable without Postgres.
    """
    first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), None)
    if first_line is None:
        return None
    segment = _strip_leading_boilerplate(_leading_segment(first_line))
    candidate = strip_corporate_suffix(segment).strip()
    return candidate or None


def _is_degenerate_casing(name: str) -> bool:
    """True when *name*'s cased letters are ALL one case (all-lower / all-upper).

    These are the names worth repairing: ``docusign`` (sitemap slug) and
    ``AIRBNB`` (logo alt) both read as a single case across every letter, which
    a real brand ("DocuSign", "Airbnb") never does.  A name with no cased
    letters at all (digits/symbols only) is treated as non-degenerate — there
    is nothing to improve.
    """
    has_cased = any(c.isalpha() for c in name)
    if not has_cased:
        return False
    return name == name.lower() or name == name.upper()


def _better_casing(candidate: str, current: str) -> str | None:
    """Return the better-cased name when *candidate* is a strict casing upgrade
    of *current*, else None.

    Requires a PURE casing variant (``candidate.casefold() == current.casefold()``)
    so the letters/spacing/punctuation are identical and only the case differs —
    the candidate can never be a different word.  The candidate is sourced from
    the company's OWN homepage ``<title>``/og meta, which is authoritative for
    its own brand capitalization, so among pure casing variants we adopt it —
    EXCEPT we never *downgrade*:

      - ``"docusign"`` → ``"DocuSign"``   (all-lower → mixed: upgrade)
      - ``"AIRBNB"``   → ``"Airbnb"``     (all-upper → mixed: upgrade)
      - ``"Docusign"`` → ``"DocuSign"``   (mixed → richer mixed: upgrade — the
                                            homepage knows its own capital S)

    and is rejected when:

      - the names are identical (nothing to do); or
      - the candidate would DEGRADE the casing — turning a name that already has
        internal capitalization into an all-lowercase or all-uppercase one
        (``"DocuSign"`` → ``"docusign"`` / ``"DOCUSIGN"``).  A homepage title
        rendered entirely in one case (a flat lowercase logo, an all-caps
        banner) must not flatten a properly-cased catalog name.
    """
    if candidate == current:
        return None
    if candidate.casefold() != current.casefold():
        return None
    # Never flatten a name that already carries internal capitalization down to
    # a single-case (all-lower / all-upper) candidate — that is a downgrade.
    if not _is_degenerate_casing(current) and _is_degenerate_casing(candidate):
        return None
    return candidate


async def _homepage_page(session: AsyncSession, company: Company) -> RawPage | None:
    """Return the company's own homepage RawPage, else its newest page.

    A company can have several raw_pages (the scraper fetches ``/``, ``/about``,
    …).  Prefer the page served from the resolved website's host so a linked
    sub-resource's title cannot drive the rename; fall back to the most recent
    page when the host cannot be matched (shared-hosting site →
    ``canonical_domain`` returns None by design).  Mirrors
    ``repair_wrong_websites._homepage_page``.
    """
    pages = (
        (
            await session.execute(
                select(RawPage)
                .where(RawPage.company_id == company.id)
                .order_by(RawPage.fetched_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not pages:
        return None
    site_domain = canonical_domain(company.website)
    if site_domain is not None:
        for page in pages:
            if canonical_domain(page.url) == site_domain:
                return page
    return pages[0]


async def run_name_quality(
    session: AsyncSession,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> NameQualitySummary:
    """Improve company display-name CASING from the stored homepage title.

    For every company that has at least one homepage ``RawPage``, derive a
    candidate brand from the stored title line and upgrade ``company.name`` when
    the candidate is an unambiguous pure-casing improvement (same
    ``normalized_name``; only the case differs; the current casing is
    degenerate).  Never touches ``slug``/``normalized_name``.  Commits once at
    the end.  ``dry_run`` logs intended upgrades and writes nothing.

    Idempotent: a re-run finds each upgraded name already equal to its candidate
    and changes nothing.
    """
    summary = NameQualitySummary()

    # Companies that have at least one raw_page. The homepage selection itself
    # happens per-company in _homepage_page; this bounds the work-list to rows
    # that could possibly have a usable title on record.
    stmt = (
        select(Company)
        .where(Company.id.in_(select(RawPage.company_id).distinct()))
        .order_by(Company.name.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    companies = (await session.execute(stmt)).scalars().all()

    for company in companies:
        page = await _homepage_page(session, company)
        if page is None:
            continue
        summary.companies_seen += 1

        candidate = _candidate_from_content(page.content)
        if candidate is None:
            continue
        # Same company only: the candidate must reduce to the SAME match key.
        if normalize_name(candidate) != company.normalized_name:
            continue
        summary.candidates_derived += 1

        better = _better_casing(candidate, company.name)
        if better is None:
            continue

        logger.info(
            "name-quality: %r -> %r (slug=%s)%s",
            company.name,
            better,
            company.slug,
            " [dry-run]" if dry_run else "",
        )
        summary.names_upgraded += 1
        if not dry_run:
            company.name = better
            session.add(company)

    if not dry_run:
        await session.commit()

    logger.info("name-quality summary: %s", summary.model_dump_json())
    return summary
