"""Adapter canary / health-check stage.

VC portfolio scrapers break *silently* on site redesigns: a layout change
yields zero rows, a badge-bleed mangles every name, a slug-cased selector
produces junk.  The catalog quietly stops growing for that firm and nothing
alarms — a recent prod incident (Lightspeed badge-bleed → 96 mangled rows; KP
yielding slug-cased names) is exactly this failure mode.

This stage is the canary.  It runs every registered adapter in
:data:`nous.sources.vc_portfolios.ADAPTERS` against a live
:class:`~nous.sources.homepage.HomepageClient`, counts the entries each one
yields, and compares the count to a *floor*.  Any adapter at or below its floor
(including one that raised) is flagged: a GitHub Actions ``::warning::``
annotation surfaces it in the run UI immediately, it appears in the step-summary
table, and a ``pipeline_runs`` audit row is recorded for history.

Floor strategy
--------------
Firms list wildly different counts — YC surfaces hundreds to thousands, a
boutique fund lists a few dozen — so a single global floor would either be too
low to catch a big firm's collapse or too high for a small one.  We use a
**configurable global floor** (:data:`DEFAULT_GLOBAL_FLOOR`, 10) with optional
**per-adapter overrides** in :data:`ADAPTER_FLOORS`.  An adapter passes when its
count is strictly greater than its effective floor; ``count <= floor`` (and a
count of 0 from a raise) trips the canary.  Overrides are deliberately set well
below each firm's observed steady-state size — the goal is to catch a *collapse*
(a redesign dropping a 600-company list to single digits), not to police
week-to-week churn.

News feeds
----------
The six broad funding-news feeds (TechCrunch venture tag, SiliconANGLE,
PR Newswire VC, Crunchbase News, VentureBeat, GeekWire funding tag) feed the
same auto-create path via ingest-news and die just as silently — a feed URL
that starts 404ing or a robots change turns into a permanent ``[]`` inside
the fetcher.  The sweep therefore also probes every :data:`NEWS_FEEDS` URL
through a live :class:`~nous.sources.news.NewsClient` and reports it under a
``news:<slug>`` key.

Feed probes measure **aliveness, not funding yield**: the probe parses the
feed raw (no keyword filter) and counts items in a
:data:`NEWS_PROBE_LOOKBACK_DAYS`-day window against
:data:`DEFAULT_NEWS_FLOOR` (0 — healthy means *any* item parsed).  Probing
the keyword-filtered fetchers instead would flap: VentureBeat's window is ~7
items and regularly contains zero funding stories, and a flapping canary
teaches people to ignore it.  Probing the URL directly also means transport
failures (robots block, 404, network) surface as recorded *errors* here
rather than being swallowed into ``[]`` the way the ingest-path fetchers
deliberately do.

This stage is **read-only**: it performs network fetches and writes exactly one
``pipeline_runs`` audit row, nothing else.  It is **resilient**: each adapter is
isolated, so one raising adapter is recorded as a failed/zero-count entry and
the remaining adapters still run.

Exit code
---------
Always exits 0 by default (annotate only — never block the pipeline, matching
``pipeline-health`` and the continue-on-error workflow design).  Pass
``--strict`` to exit non-zero when any adapter is below floor, for a dedicated
alerting cron.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel

from nous.observability import write_step_summary
from nous.sources.crunchbase_news import CB_NEWS_FEED
from nous.sources.geekwire import GEEKWIRE_FUNDING_FEED
from nous.sources.homepage import HomepageClient
from nous.sources.news import NewsClient
from nous.sources.prnewswire import PRNEWSWIRE_VC_FEED
from nous.sources.siliconangle import SILICONANGLE_FEED
from nous.sources.techcrunch import TC_FUNDING_FEED
from nous.sources.vc_portfolios import ADAPTERS, PortfolioAdapter
from nous.sources.venturebeat import VENTUREBEAT_FEED

logger = logging.getLogger(__name__)

# Broad funding-news feed URLs probed by the sweep, keyed by the same slug
# used for ``companies.discovered_via``. Reported as ``news:<slug>`` so firm
# and feed keys can never collide in the summary maps. Probed as raw URLs
# (not through the fetch_*_funding_articles wrappers) — see the module
# docstring's "News feeds" section for why.
NEWS_FEEDS: dict[str, str] = {
    "techcrunch": TC_FUNDING_FEED,
    "siliconangle": SILICONANGLE_FEED,
    "prnewswire": PRNEWSWIRE_VC_FEED,
    "crunchbase_news": CB_NEWS_FEED,
    "venturebeat": VENTUREBEAT_FEED,
    "geekwire": GEEKWIRE_FUNDING_FEED,
}

# Feed floor: healthy means *any* raw item parsed (count > 0). The probe is
# unfiltered, so a live feed always clears this; zero items over
# NEWS_PROBE_LOOKBACK_DAYS means the feed is dead, frozen, or no longer RSS.
DEFAULT_NEWS_FLOOR = 0

# Wide probe window so a slow publishing fortnight cannot trip the canary on
# its own (the standing ingest lookback is 14 days).
NEWS_PROBE_LOOKBACK_DAYS = 30

# Global default floor: an adapter yielding this many entries or fewer is
# treated as a collapse.  10 is low enough to never false-positive on a real
# (if small) portfolio yet catches a redesign that drops a list to single
# digits.  Configurable at the CLI via --floor.
DEFAULT_GLOBAL_FLOOR = 10

# Per-adapter floor overrides, keyed by firm slug.  Only firms whose healthy
# steady-state count is large enough that the global floor would miss a
# meaningful collapse need an entry here.  Each value is set well below the
# firm's observed size so normal churn never trips it — these guard against a
# *structural* break, not week-to-week variation.  Firms absent from this map
# fall back to DEFAULT_GLOBAL_FLOOR.
ADAPTER_FLOORS: dict[str, int] = {
    # YC's public directory lists thousands; a few hundred is a hard collapse.
    "yc": 200,
    # a16z surfaces several hundred portfolio companies.
    "a16z": 100,
    # Large, established multi-stage funds with big public portfolios.
    "sequoia": 50,
    "lightspeed": 50,
    "greylock": 50,
    "bessemer": 50,
    "accel": 50,
    "general_catalyst": 50,
    "founders_fund": 30,
    "khosla": 30,
    "index_ventures": 30,
    "felicis": 30,
    "kleiner_perkins": 30,
}


def floor_for(firm: str, *, global_floor: int = DEFAULT_GLOBAL_FLOOR) -> int:
    """Return the effective entry floor for *firm*.

    A per-firm override in :data:`ADAPTER_FLOORS` wins; otherwise the supplied
    *global_floor* applies.  Pure + side-effect-free for trivial unit testing.
    """
    return ADAPTER_FLOORS.get(firm, global_floor)


def news_floor_for(slug: str) -> int:
    """Return the effective entry floor for a news feed *slug*.

    An ``ADAPTER_FLOORS`` entry keyed ``news:<slug>`` wins (none are set
    today — feeds share :data:`DEFAULT_NEWS_FLOOR`); the prefixed key keeps
    feed overrides from ever colliding with a firm slug.
    """
    return ADAPTER_FLOORS.get(f"news:{slug}", DEFAULT_NEWS_FLOOR)


@dataclass
class AdapterHealth:
    """Canary result for a single adapter."""

    firm: str
    count: int
    floor: int
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """True when the adapter ran and yielded *more* than its floor.

        A raise (``error`` set, ``count == 0``) is never healthy; a count at or
        below the floor is a collapse signal.
        """
        return self.error is None and self.count > self.floor


@dataclass
class AdapterHealthReport:
    """Result of one adapter-health sweep."""

    adapters: list[AdapterHealth] = field(default_factory=list)
    global_floor: int = DEFAULT_GLOBAL_FLOOR

    @property
    def unhealthy(self) -> list[AdapterHealth]:
        """Adapters below floor or that raised, in input order."""
        return [a for a in self.adapters if not a.healthy]

    @property
    def all_healthy(self) -> bool:
        return len(self.unhealthy) == 0

    @property
    def total_entries(self) -> int:
        return sum(a.count for a in self.adapters)


class AdapterHealthSummary(BaseModel):
    """Serializable summary persisted to ``pipeline_runs.summary``."""

    adapters_checked: int = 0
    adapters_healthy: int = 0
    adapters_unhealthy: int = 0
    total_entries: int = 0
    global_floor: int = DEFAULT_GLOBAL_FLOOR
    # firm slug -> entry count (every adapter, healthy or not).
    counts: dict[str, int] = {}
    # firm slug -> effective floor it was measured against.
    floors: dict[str, int] = {}
    # firm slug -> error repr, for adapters that raised.
    failures: dict[str, str] = {}
    # firm slugs that were below floor or raised (the canary trips).
    below_floor: list[str] = []


async def _probe_adapter(
    firm: str,
    adapter: PortfolioAdapter,
    client: HomepageClient,
    *,
    global_floor: int,
) -> AdapterHealth:
    """Run one adapter's ``fetch`` and classify it against its floor.

    Adapter-failure isolation lives here: any exception is caught, logged, and
    converted to a zero-count failed result so a single broken site can never
    abort the sweep (mirrors ``refresh_vc_portfolios``' per-adapter try/except).
    """
    floor = floor_for(firm, global_floor=global_floor)
    try:
        entries = await adapter.fetch(client)
    except Exception as exc:  # noqa: BLE001 — per-adapter isolation is the point
        logger.exception("adapter-health: adapter %s raised during fetch", firm)
        return AdapterHealth(firm=firm, count=0, floor=floor, error=repr(exc))

    count = len(entries)
    health = AdapterHealth(firm=firm, count=count, floor=floor)
    logger.info(
        "adapter-health: firm=%s count=%d floor=%d healthy=%s",
        firm,
        count,
        floor,
        health.healthy,
    )
    return health


async def _probe_news_feed(
    slug: str,
    url: str,
    client: NewsClient,
) -> AdapterHealth:
    """Fetch one feed URL, parse it raw, and classify against its floor.

    Deliberately bypasses the ``fetch_*_funding_articles`` wrappers: they
    keyword-filter (which makes counts flap on funding-free windows) and map
    transport failures to ``[]`` (which hides them). Here a robots block,
    HTTP error, or network failure is *recorded as the error it is*, and the
    count reflects raw feed aliveness.
    """
    key = f"news:{slug}"
    floor = news_floor_for(slug)
    try:
        xml_text = await client.fetch_text(url)
        results = client._parse_rss(
            xml_text,
            lookback_days=NEWS_PROBE_LOOKBACK_DAYS,
            require_keywords=False,
        )
    except Exception as exc:  # noqa: BLE001 — per-feed isolation is the point
        logger.exception("adapter-health: news feed %s failed during probe", slug)
        return AdapterHealth(firm=key, count=0, floor=floor, error=repr(exc))

    count = len(results)
    health = AdapterHealth(firm=key, count=count, floor=floor)
    logger.info(
        "adapter-health: feed=%s count=%d floor=%d healthy=%s",
        slug,
        count,
        floor,
        health.healthy,
    )
    return health


async def run_adapter_health(
    client: HomepageClient,
    *,
    adapters: dict[str, PortfolioAdapter] | None = None,
    global_floor: int = DEFAULT_GLOBAL_FLOOR,
    news_client: NewsClient | None = None,
    news_feeds: dict[str, str] | None = None,
) -> AdapterHealthReport:
    """Probe every adapter (and news feed) and return an :class:`AdapterHealthReport`.

    Args:
        client: An entered :class:`HomepageClient` the adapters fetch through.
        adapters: Registry to probe; defaults to the real
            :data:`nous.sources.vc_portfolios.ADAPTERS`.  Injectable so tests
            can pass fakes without monkeypatching.
        global_floor: Floor applied to any firm without an
            :data:`ADAPTER_FLOORS` override.
        news_client: An entered :class:`NewsClient` for the news-feed probes.
            ``None`` (the default) skips the feed sweep entirely — VC-only
            callers and existing tests are unaffected.
        news_feeds: slug -> feed-URL registry to probe when ``news_client``
            is given; defaults to :data:`NEWS_FEEDS`.  Results are keyed
            ``news:<slug>``.

    Adapters are probed sequentially.  Each adapter already throttles its own
    HTTP (the shared per-domain 1 req/s budget in :class:`HomepageClient`), and
    different firms live on different domains, so sequential probing keeps the
    code simple without sacrificing politeness.
    """
    registry = adapters if adapters is not None else ADAPTERS
    report = AdapterHealthReport(global_floor=global_floor)
    # Deterministic order so annotations / summaries are stable across runs.
    for firm in sorted(registry):
        health = await _probe_adapter(
            firm, registry[firm], client, global_floor=global_floor
        )
        report.adapters.append(health)
    if news_client is not None:
        feed_registry = news_feeds if news_feeds is not None else NEWS_FEEDS
        for slug in sorted(feed_registry):
            report.adapters.append(
                await _probe_news_feed(slug, feed_registry[slug], news_client)
            )
    return report


def build_summary(report: AdapterHealthReport) -> AdapterHealthSummary:
    """Project a report into the serializable ``pipeline_runs`` summary model."""
    return AdapterHealthSummary(
        adapters_checked=len(report.adapters),
        adapters_healthy=len(report.adapters) - len(report.unhealthy),
        adapters_unhealthy=len(report.unhealthy),
        total_entries=report.total_entries,
        global_floor=report.global_floor,
        counts={a.firm: a.count for a in report.adapters},
        floors={a.firm: a.floor for a in report.adapters},
        failures={a.firm: a.error for a in report.adapters if a.error is not None},
        below_floor=[a.firm for a in report.unhealthy],
    )


def emit_adapter_health_annotations(report: AdapterHealthReport) -> None:
    """Print GitHub Actions annotations + a step-summary table.

    One ``::warning::`` per unhealthy adapter surfaces in the Actions run UI
    even though the stage exits 0 (matching ``emit_health_annotations`` in
    ``pipeline_health``).  Safe outside CI: annotations are harmless plain text
    and ``write_step_summary`` is a no-op when GITHUB_STEP_SUMMARY is unset.
    """
    if report.all_healthy:
        print(
            f"adapter-health: all {len(report.adapters)} adapter(s) above floor ✓",
            flush=True,
        )
        _write_summary_table(report)
        return

    for adapter in report.unhealthy:
        if adapter.error is not None:
            print(
                f"::warning::adapter-health: adapter '{adapter.firm}' FAILED "
                f"(raised during fetch: {adapter.error})",
                flush=True,
            )
        else:
            print(
                f"::warning::adapter-health: adapter '{adapter.firm}' yielded "
                f"{adapter.count} entries, at or below floor {adapter.floor} — "
                f"possible site redesign / scraper breakage",
                flush=True,
            )
        logger.warning(
            "adapter-health: firm=%s count=%d floor=%d error=%s",
            adapter.firm,
            adapter.count,
            adapter.floor,
            adapter.error,
        )

    _write_summary_table(report)


def _write_summary_table(report: AdapterHealthReport) -> None:
    """Append a per-adapter markdown table to the step summary."""
    rows_md = "\n".join(
        (
            f"| {a.firm} | **{a.count}** | {a.floor} | "
            f"{'raised' if a.error is not None else 'below floor'} |"
        )
        if not a.healthy
        else f"| {a.firm} | {a.count} | {a.floor} | ok |"
        for a in report.adapters
    )
    bad = len(report.unhealthy)
    if bad == 0:
        heading = (
            f"### Adapter health — all {len(report.adapters)} adapter(s) "
            "above floor ✓"
        )
    else:
        heading = f"### Adapter health — :warning: {bad} adapter(s) below floor"
    md = (
        f"\n{heading}\n\n"
        "| firm | entries | floor | status |\n"
        "| --- | --- | --- | --- |\n"
        f"{rows_md}\n\n"
    )
    write_step_summary(md)


async def adapter_health_main(
    *, user_agent: str, global_floor: int = DEFAULT_GLOBAL_FLOOR
) -> AdapterHealthReport:
    """End-to-end driver: open a client, probe adapters, record the audit row.

    Mirrors ``pipeline-health``'s telemetry pattern: records exactly one
    ``pipeline_runs`` row (status='empty' when any adapter is below floor, so
    ``pipeline-health`` surfaces this canary alongside the other stages) and
    returns the report for the CLI to annotate + decide the exit code on.

    Kept out of the CLI body so the network-free unit tests can exercise
    classification/annotation directly against :func:`run_adapter_health`.
    """
    from datetime import UTC, datetime

    from nous.observability import record_pipeline_run

    started = datetime.now(UTC)
    async with (
        HomepageClient(
            user_agent,
            requests_per_second_per_domain=1.0,
        ) as client,
        NewsClient(
            user_agent,
            requests_per_second_per_domain=1.0,
        ) as news_client,
    ):
        report = await run_adapter_health(
            client, global_floor=global_floor, news_client=news_client
        )

    summary = build_summary(report)
    # rows_written counts healthy adapters; with flag_empty, an all-unhealthy
    # sweep that still "saw" adapters records status='empty' so the silent
    # collapse shows up in pipeline-health too.  An explicit annotation is also
    # emitted below by the CLI via emit_adapter_health_annotations.
    await record_pipeline_run(
        "adapter-health",
        started_at=started,
        inputs_seen=summary.adapters_checked,
        rows_written=summary.adapters_healthy,
        summary=summary,
        flag_empty=True,
    )
    return report


def run_adapter_health_sync(
    *, user_agent: str, global_floor: int = DEFAULT_GLOBAL_FLOOR
) -> AdapterHealthReport:
    """Synchronous entry point for the Click command."""
    return asyncio.run(
        adapter_health_main(user_agent=user_agent, global_floor=global_floor)
    )
