"""dedup-companies pipeline stage.

Collapses duplicate company rows that name-only matching let through. Two
passes:

1. **Exact-domain clustering.** Group companies by ``canonical_domain(website)``
   (shared-hosting domains and websiteless rows are skipped). A shared domain
   is NOT decisive on its own: a wrong website that slipped past the aggregator
   reject list (the Kalshi-profile-carrying-FrenFlow's-site class that
   repair-wrong-websites cleans up) would otherwise fuse two real companies,
   and merges are irreversible. So within a cluster, rows auto-merge only when
   their NAMES corroborate the shared domain (:func:`names_corroborate_domain`
   — normalized-name equality, or both names independently spelling the
   domain's registrable label). Members that corroborate each other collapse
   into one survivor per corroborated group; any two groups left distinct
   inside one domain cluster become a candidate pair for the pass-2 LLM gate
   (judged first, ahead of fuzzy pairs, under the same budget and the same
   same_company + high-confidence bar).

2. **Fuzzy adjudication.** Among the rows left standing, generate candidate
   pairs from soft signals — trigram-similar normalized names, or a shared
   (hq_city, hq_state) with a weaker name similarity — and ask the LLM whether
   each pair is the same company. Merge ONLY on ``same_company=true`` AND
   ``confidence='high'``. Everything else is left alone.

Survivor preference (both passes): prefer the row that already has a
``description_long`` (most-enriched), then one with a ``website``, then the
earliest ``created_at`` (most-established). Ties broken by id for determinism.

Idempotency: merging is a one-way fold (the loser id ceases to exist), so a
second run finds the same domain groups collapsed to one row and the same
fuzzy pairs already merged — nothing new to do. Per-merge commits mean a
partial run leaves a consistent DB.

A merged-away loser's slug is not lost: ``merge_companies`` records it in
``slug_aliases`` so the web layer permanently redirects the dead URL to the
survivor (see the slug_aliases section of its docstring for chain semantics).

Quota discipline (spec §11): at most ``llm_limit`` LLM judgments per run
(uncorroborated domain pairs first, then highest-similarity fuzzy pairs).
When more candidates exist than the cap, the overflow count is logged and
reported in the summary — never silently dropped.

``dry_run=True`` performs every read and LLM call but skips the merges/commits,
reporting what *would* be merged.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.db.models import Company
from nous.db.upsert import merge_companies
from nous.llm.client import LLMError, LLMParseError, LLMRateLimitError, complete_json
from nous.llm.prompts.company_match import CompanyMatch, build_company_match_prompt
from nous.util.slugify import normalize_name, slugify
from nous.util.url import canonical_domain

logger = logging.getLogger(__name__)

# Trigram thresholds for fuzzy-candidate generation (pass 2).
# NAME_SIMILARITY_THRESHOLD is the floor for a name-only candidate; the lower
# CO_LOCATED_NAME_THRESHOLD applies only when two rows also share an HQ, where
# the location corroborates a weaker name match. Both are deliberately loose —
# they only nominate candidates; the LLM is the actual gate.
NAME_SIMILARITY_THRESHOLD = 0.45
CO_LOCATED_NAME_THRESHOLD = 0.30


# Name-corroboration rule for the domain pass (see names_corroborate_domain).
# A name token-prefix shorter than this can't vouch for a domain label on its
# own ("ai", "go" prefix a thousand names) — mirrors article_links'
# _MIN_DOMAIN_LABEL_MATCH. A label equal to the WHOLE name is exempt (x.com).
_MIN_LABEL_PREFIX_MATCH = 4
# Marketing affixes companies wrap their name in when the bare .com is taken
# (getclay.com, tryramp.com, acmehq.com). One affix is stripped before
# matching; kept deliberately short — every entry widens what counts as
# "the name spells the domain".
_DOMAIN_LABEL_PREFIXES = ("get", "try", "use", "join", "hello", "meet")
_DOMAIN_LABEL_SUFFIXES = ("hq", "app", "labs", "ai")
# Second-level labels under a two-letter ccTLD that are part of the public
# suffix (acme.co.uk → "acme", not "co").
_CCTLD_SECOND_LEVELS = frozenset({"co", "com", "org", "net", "gov", "ac", "edu"})


class DedupSummary(BaseModel):
    companies_seen: int = 0
    # Merges that came out of a shared-domain cluster: name-corroborated
    # auto-merges plus LLM-confirmed domain pairs (never also in llm_merges).
    domain_merges: int = 0
    domain_merges_name_corroborated: int = 0
    # Shared-domain pairs whose names did NOT corroborate the domain, routed to
    # the LLM gate instead of auto-merging: judged / merged / rejected (judged
    # but not same_company + high). Pairs past llm_limit count in ``skipped``.
    domain_pairs_sent_to_llm: int = 0
    domain_pairs_llm_merged: int = 0
    domain_pairs_rejected: int = 0
    # Every LLM judgment made (domain + fuzzy), bounded by llm_limit.
    llm_judged: int = 0
    # Fuzzy-pass (non-domain) LLM merges.
    llm_merges: int = 0
    skipped: int = 0


class _CompanyRow(BaseModel):
    """Lightweight projection of a company used for survivor selection + the
    LLM prompt, so we don't hold full ORM objects across per-merge commits."""

    model_config = {"arbitrary_types_allowed": True}

    id: UUID
    name: str
    normalized_name: str
    website: str | None
    hq_city: str | None
    hq_state: str | None
    description_short: str | None
    description_long: str | None
    latest_round_amount: Decimal | None
    latest_round_date: date | None
    latest_round_type: str | None
    created_at: datetime

    def to_prompt_dict(self) -> dict[str, object]:
        # The latest-round denorms are the strongest same-company evidence
        # for website-less husks (bunkerhill + bunkerhill-health both carried
        # one fresh $55M round, but the adjudicator couldn't see it and kept
        # declining the merge — 2026-07-17 QA). Rendered as one line; absent
        # facts are omitted, never guessed.
        funding = None
        if self.latest_round_amount is not None or self.latest_round_date is not None:
            parts = []
            if self.latest_round_type:
                parts.append(self.latest_round_type)
            if self.latest_round_amount is not None:
                parts.append(f"${self.latest_round_amount:,.0f}")
            if self.latest_round_date is not None:
                parts.append(f"announced {self.latest_round_date.isoformat()}")
            funding = " ".join(parts)
        return {
            "name": self.name,
            "website": self.website,
            # Prefer the long description for the adjudicator, fall back to short.
            "description": self.description_long or self.description_short,
            "hq_city": self.hq_city,
            "hq_state": self.hq_state,
            "latest_funding": funding,
        }


def _survivor_sort_key(row: _CompanyRow) -> tuple[int, int, datetime, str]:
    """Sort key whose minimum is the preferred survivor.

    Lower is better: rows with a long description rank ahead of those without;
    then rows with a website; then earlier ``created_at``; id as a final stable
    tiebreak. (Booleans are inverted via ``not`` so True → 0 sorts first.)
    """
    return (
        int(not bool(row.description_long)),
        int(not bool(row.website)),
        row.created_at,
        str(row.id),
    )


def _choose_survivor(rows: list[_CompanyRow]) -> _CompanyRow:
    return min(rows, key=_survivor_sort_key)


# ---------------------------------------------------------------------------
# Name corroboration for the domain pass (pure — unit-tested without a DB)
# ---------------------------------------------------------------------------


def registrable_label(domain: str) -> str:
    """The registrable label of a ``canonical_domain`` host, alphanumerics only.

    "app.acme.com" → "acme"; "get-clay.com" → "getclay"; "acme.co.uk" →
    "acme". A two-letter ccTLD under a generic second level (co/com/org/…) is
    treated as a two-part public suffix; anything else uses the label left of
    the TLD. Not a full public-suffix list — an unusual suffix only makes the
    rule stricter (the label won't match a name, so the pair goes to the LLM).
    """
    labels = [part for part in domain.lower().split(".") if part]
    if not labels:
        return ""
    if len(labels) == 1:
        core = labels[0]
    elif (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in _CCTLD_SECOND_LEVELS
    ):
        core = labels[-3]
    else:
        core = labels[-2]
    return re.sub(r"[^a-z0-9]+", "", core)


def name_explains_domain_label(name: str, label: str) -> bool:
    """True when ``name`` spells the domain ``label`` — exactly, or as a
    leading run of its tokens.

    The name is tokenized like a slug (unicode-folded, corporate suffix
    stripped, split on non-alphanumerics), and the label must equal the
    concatenation of the first k tokens for some k: "Acme Robotics" explains
    "acme" and "acmerobotics"; "Hooli XYZ" explains "hooli"; "Kalshi" does not
    explain "frenflow". One marketing affix may be stripped from the label
    first (getclay → clay, acmehq → acme). A partial or affix-stripped match
    must be ≥ ``_MIN_LABEL_PREFIX_MATCH`` chars; only a label equal to the
    WHOLE name matches at any length.
    """
    tokens = [token for token in slugify(name).split("-") if token]
    if not tokens or not label:
        return False
    prefix_keys = {"".join(tokens[:k]) for k in range(1, len(tokens) + 1)}
    if label == "".join(tokens):
        return True
    candidates = {label}
    for prefix in _DOMAIN_LABEL_PREFIXES:
        if label.startswith(prefix):
            candidates.add(label[len(prefix):])
    for suffix in _DOMAIN_LABEL_SUFFIXES:
        if label.endswith(suffix):
            candidates.add(label[: -len(suffix)])
    return any(
        len(candidate) >= _MIN_LABEL_PREFIX_MATCH and candidate in prefix_keys
        for candidate in candidates
    )


def names_corroborate_domain(name_a: str, name_b: str, domain: str) -> bool:
    """Do two companies' names corroborate that their shared ``domain`` means
    they are the same company? The domain pass auto-merges only when this holds.

    True when EITHER
    - the normalized names are equal and non-empty ("Acme, Inc." / "ACME"), OR
    - BOTH names independently explain the domain's registrable label
      (:func:`name_explains_domain_label`).

    Requiring both names to explain the label is what blocks the known
    wrong-website class: when one company carries another's site (a Kalshi row
    pointing at frenflow.com), the intruder's name doesn't spell the domain,
    so the pair goes to the LLM gate instead of auto-merging. Known limit: two
    distinct same-stem companies that BOTH got the stem's domain by a blind
    name→TLD guess ("Sierra" / "Sierra Space" → sierra.com) still corroborate
    — the rule can't tell a descriptor ("Robotics") from a distinct product
    name ("Space"); that wrong-website class is repair-wrong-websites' job.
    """
    norm_a, norm_b = normalize_name(name_a), normalize_name(name_b)
    if norm_a and norm_a == norm_b:
        return True
    label = registrable_label(domain)
    return name_explains_domain_label(name_a, label) and name_explains_domain_label(
        name_b, label
    )


def _corroborated_groups(
    rows: list[_CompanyRow], domain: str
) -> list[list[_CompanyRow]]:
    """Partition one domain cluster into groups whose names corroborate the
    domain with each other (connected components of
    :func:`names_corroborate_domain`).

    Each group of ≥2 auto-merges into its own survivor; distinct groups are
    NOT merged with each other without an LLM verdict. Deterministic: groups
    and their members come out in survivor-preference order.
    """
    ordered = sorted(rows, key=_survivor_sort_key)
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if names_corroborate_domain(ordered[i].name, ordered[j].name, domain):
                root_i, root_j = find(i), find(j)
                if root_i != root_j:
                    parent[max(root_i, root_j)] = min(root_i, root_j)

    groups: dict[int, list[_CompanyRow]] = {}
    for i, row in enumerate(ordered):
        groups.setdefault(find(i), []).append(row)
    # Roots are each group's lowest index, so sorting by root keeps the
    # survivor-preference order across groups too.
    return [groups[root] for root in sorted(groups)]


async def _load_companies(session: AsyncSession) -> list[_CompanyRow]:
    stmt = select(
        Company.id,
        Company.name,
        Company.normalized_name,
        Company.website,
        Company.hq_city,
        Company.hq_state,
        Company.description_short,
        Company.description_long,
        Company.latest_round_amount,
        Company.latest_round_date,
        Company.latest_round_type,
        Company.created_at,
    )
    result = await session.execute(stmt)
    return [
        _CompanyRow(
            id=r.id,
            name=r.name,
            normalized_name=r.normalized_name,
            website=r.website,
            hq_city=r.hq_city,
            hq_state=r.hq_state,
            description_short=r.description_short,
            description_long=r.description_long,
            latest_round_amount=r.latest_round_amount,
            latest_round_date=r.latest_round_date,
            latest_round_type=r.latest_round_type,
            created_at=r.created_at,
        )
        for r in result
    ]


async def _run_domain_pass(
    session: AsyncSession,
    rows: list[_CompanyRow],
    summary: DedupSummary,
    *,
    dry_run: bool,
) -> tuple[set[UUID], list[tuple[UUID, UUID]]]:
    """Cluster ``rows`` by canonical domain; auto-merge only name-corroborated
    members, and hand the rest to the LLM gate.

    Each multi-row cluster is split into :func:`_corroborated_groups`; every
    group of ≥2 collapses into its preferred survivor (no LLM). When a cluster
    holds more than one group, every pair of group survivors is returned as an
    LLM candidate — the shared domain makes them worth judging, but nothing
    about their names says they are one company.

    Returns ``(merged_away, llm_pairs)``: the loser ids (so the LLM pass can
    exclude them) and the uncorroborated pairs, survivor-preferred id first.
    In ``dry_run`` mode no merge/commit happens but losers are still reported
    so the count reflects what would be collapsed.
    """
    clusters: dict[str, list[_CompanyRow]] = {}
    for row in rows:
        domain = canonical_domain(row.website)
        if domain is None:
            continue
        clusters.setdefault(domain, []).append(row)

    merged_away: set[UUID] = set()
    llm_pairs: list[tuple[UUID, UUID]] = []
    for domain, cluster in clusters.items():
        if len(cluster) < 2:
            continue
        groups = _corroborated_groups(cluster, domain)
        representatives: list[_CompanyRow] = []
        merged_here = 0
        for group in groups:
            survivor = _choose_survivor(group)
            representatives.append(survivor)
            for loser in group:
                if loser.id == survivor.id:
                    continue
                merged_away.add(loser.id)
                summary.domain_merges += 1
                summary.domain_merges_name_corroborated += 1
                merged_here += 1
                if dry_run:
                    continue
                await merge_companies(
                    session, survivor_id=survivor.id, loser_id=loser.id
                )
            if not dry_run and len(group) > 1:
                logger.info(
                    "dedup: domain %s — merged %d name-corroborated row(s) into "
                    "survivor %s",
                    domain,
                    len(group) - 1,
                    survivor.id,
                )
        if not dry_run and merged_here:
            await session.commit()

        for i, rep_a in enumerate(representatives):
            for rep_b in representatives[i + 1 :]:
                llm_pairs.append((rep_a.id, rep_b.id))
        if len(representatives) > 1:
            logger.info(
                "dedup: domain %s shared by %d name-uncorroborated companies "
                "(%s) — routing to the LLM gate, not auto-merging",
                domain,
                len(representatives),
                ", ".join(rep.name for rep in representatives),
            )
    return merged_away, llm_pairs


async def _generate_fuzzy_pairs(
    session: AsyncSession, candidate_ids: set[UUID]
) -> list[tuple[UUID, UUID, float]]:
    """Return unordered candidate pairs ``(id_a, id_b, similarity)`` among
    ``candidate_ids``, highest similarity first.

    A pair qualifies when either:
    - normalized-name trigram similarity ≥ NAME_SIMILARITY_THRESHOLD, OR
    - the two rows share a non-null (hq_city, hq_state) AND name similarity ≥
      CO_LOCATED_NAME_THRESHOLD.

    The self-join is ordered ``a.id < b.id`` so each pair appears once. The
    pg_trgm GIN index on normalized_name backs ``func.similarity``.
    """
    if len(candidate_ids) < 2:
        return []

    a = Company.__table__.alias("a")
    b = Company.__table__.alias("b")
    similarity = func.similarity(a.c.normalized_name, b.c.normalized_name)

    co_located = and_(
        a.c.hq_city.is_not(None),
        a.c.hq_state.is_not(None),
        func.lower(a.c.hq_city) == func.lower(b.c.hq_city),
        func.lower(a.c.hq_state) == func.lower(b.c.hq_state),
        similarity >= CO_LOCATED_NAME_THRESHOLD,
    )

    stmt = (
        select(a.c.id, b.c.id, similarity.label("sim"))
        .select_from(
            a.join(
                b,
                and_(
                    a.c.id < b.c.id,
                    a.c.id.in_(candidate_ids),
                    b.c.id.in_(candidate_ids),
                    or_(similarity >= NAME_SIMILARITY_THRESHOLD, co_located),
                ),
            )
        )
        .order_by(similarity.desc())
    )
    result = await session.execute(stmt)
    return [(r[0], r[1], float(r.sim)) for r in result]


async def _run_llm_pass(
    session: AsyncSession,
    rows: list[_CompanyRow],
    merged_away: set[UUID],
    domain_pairs: list[tuple[UUID, UUID]],
    summary: DedupSummary,
    *,
    llm_limit: int,
    dry_run: bool,
) -> None:
    """Adjudicate candidate pairs with the LLM and merge HIGH-confidence matches.

    Candidates are the domain pass's name-uncorroborated pairs FIRST (a shared
    domain is the strongest nomination signal we have), then the fuzzy pairs,
    highest-similarity first; a fuzzy pair already nominated by the domain pass
    is not judged twice. Every candidate faces the same gate (``company_match``
    prompt; merge only on same_company AND confidence='high') and the same
    ``llm_limit`` budget.
    """
    by_id = {row.id: row for row in rows if row.id not in merged_away}
    candidate_ids = set(by_id)
    fuzzy_pairs = await _generate_fuzzy_pairs(session, candidate_ids)

    nominated = {frozenset(pair) for pair in domain_pairs}
    # (id_a, id_b, similarity, from_domain_pass). Domain pairs carry no trigram
    # similarity; NaN keeps the log line honest.
    pairs: list[tuple[UUID, UUID, float, bool]] = [
        (id_a, id_b, float("nan"), True) for id_a, id_b in domain_pairs
    ] + [
        (id_a, id_b, sim, False)
        for id_a, id_b, sim in fuzzy_pairs
        if frozenset((id_a, id_b)) not in nominated
    ]

    if len(pairs) > llm_limit:
        summary.skipped += len(pairs) - llm_limit
        logger.warning(
            "dedup: %d LLM candidate pairs (%d domain, %d fuzzy) exceed "
            "llm_limit=%d; judging the first %d (domain pairs, then highest-"
            "similarity) and deferring %d to the next run.",
            len(pairs),
            len(domain_pairs),
            len(pairs) - len(domain_pairs),
            llm_limit,
            llm_limit,
            len(pairs) - llm_limit,
        )
        pairs = pairs[:llm_limit]

    # A row may appear in several pairs; once it's merged away (as survivor or
    # loser) we must not reuse the stale projection. Track the live id set.
    gone: set[UUID] = set()

    for id_a, id_b, _sim, from_domain in pairs:
        if id_a in gone or id_b in gone:
            continue
        row_a = by_id.get(id_a)
        row_b = by_id.get(id_b)
        if row_a is None or row_b is None:
            continue

        prompt = build_company_match_prompt(
            row_a.to_prompt_dict(), row_b.to_prompt_dict()
        )
        try:
            match: CompanyMatch = await complete_json(prompt, CompanyMatch)
        except LLMRateLimitError:
            logger.warning(
                "dedup: LLM rate limit hit — stopping the LLM pass to avoid "
                "further quota exhaustion."
            )
            break
        except (LLMParseError, LLMError) as exc:
            logger.warning(
                "dedup: LLM error judging %s vs %s: %s", id_a, id_b, exc
            )
            continue

        summary.llm_judged += 1
        if from_domain:
            summary.domain_pairs_sent_to_llm += 1

        if not (match.same_company and match.confidence == "high"):
            if from_domain:
                summary.domain_pairs_rejected += 1
                logger.info(
                    "dedup: LLM declined shared-domain pair %r / %r "
                    "(same_company=%s, confidence=%s) — left unmerged",
                    row_a.name,
                    row_b.name,
                    match.same_company,
                    match.confidence,
                )
            continue

        survivor = _choose_survivor([row_a, row_b])
        loser = row_b if survivor.id == row_a.id else row_a
        if from_domain:
            summary.domain_merges += 1
            summary.domain_pairs_llm_merged += 1
        else:
            summary.llm_merges += 1
        if dry_run:
            continue
        await merge_companies(
            session, survivor_id=survivor.id, loser_id=loser.id
        )
        await session.commit()
        gone.add(loser.id)
        logger.info(
            "dedup: LLM merged %s into %s (%s, sim=%.2f)",
            loser.id,
            survivor.id,
            "shared domain" if from_domain else "fuzzy",
            _sim,
        )


async def run_dedup_companies(
    session: AsyncSession,
    *,
    llm_limit: int = 200,
    dry_run: bool = False,
) -> DedupSummary:
    """Deduplicate companies: name-corroborated exact-domain auto-merge, then an
    LLM-gated pass over uncorroborated shared-domain pairs and fuzzy pairs.

    See the module docstring for the algorithm and survivor rule. Returns a
    :class:`DedupSummary` of counts.
    """
    summary = DedupSummary()

    rows = await _load_companies(session)
    summary.companies_seen = len(rows)

    merged_away, domain_pairs = await _run_domain_pass(
        session, rows, summary, dry_run=dry_run
    )

    # Reload projections after the domain pass so the LLM pass sees survivors'
    # inherited description/website/HQ (and not the merged-away losers). The
    # domain pairs reference group survivors, which are never merged away, so
    # they stay valid across the reload. In a dry run nothing was merged, so
    # the original snapshot is still accurate.
    if not dry_run and merged_away:
        rows = await _load_companies(session)
        merged_away = set()

    await _run_llm_pass(
        session,
        rows,
        merged_away,
        domain_pairs,
        summary,
        llm_limit=llm_limit,
        dry_run=dry_run,
    )

    return summary
