"""YC portfolio adapter.

YC publishes its portfolio at https://www.ycombinator.com/companies which is a
React app backed by Algolia. The Algolia app id and search-only api key live in
``window.AlgoliaOpts`` on the portfolio page itself (the bundled JS reads them
directly). Both rotate occasionally — we rediscover them every fetch rather
than hardcoding.

Per the M3 decision, we drop hits where ``stage == "Pre-Seed"``. The live YC
index currently classifies companies as "Early"/"Growth" rather than the
finer-grained "Pre-Seed", so the filter is a no-op against today's data, but
the contract is preserved for when YC restores stage granularity.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios.base import PortfolioEntry

logger = logging.getLogger(__name__)


class YcAdapter:
    firm = "yc"
    PORTFOLIO_URL = "https://www.ycombinator.com/companies"
    INDEX_NAME = "YCCompany_production"
    EXCLUDED_STAGES: frozenset[str] = frozenset({"Pre-Seed"})
    HITS_PER_PAGE = 1000
    # Hard cap to avoid runaway pagination if Algolia returns implausible nbPages.
    MAX_PAGES = 50

    _ALGOLIA_OPTS_RE = re.compile(
        r'AlgoliaOpts\s*=\s*(\{[^}]*"app"\s*:\s*"[^"]+"[^}]*\})'
    )

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        app_id, api_key = await self._discover_algolia_credentials(client)
        algolia_url = f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries"

        results: list[PortfolioEntry] = []
        dropped_stages: dict[str, int] = {}
        page = 0
        while page < self.MAX_PAGES:
            data = await self._algolia_page(client, algolia_url, app_id, api_key, page)
            for hit in data.get("hits", []):
                stage = hit.get("stage")
                if isinstance(stage, str) and stage in self.EXCLUDED_STAGES:
                    dropped_stages[stage] = dropped_stages.get(stage, 0) + 1
                    continue
                name = hit.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                website_raw = hit.get("website")
                website = website_raw if isinstance(website_raw, str) and website_raw else None
                one_liner = hit.get("one_liner")
                description = one_liner if isinstance(one_liner, str) and one_liner else None
                results.append(
                    PortfolioEntry(
                        firm=self.firm,
                        name=name,
                        website=website,
                        description=description,
                        source_url=self.PORTFOLIO_URL,
                    )
                )
            nb_pages = data.get("nbPages")
            if not isinstance(nb_pages, int) or page >= nb_pages - 1:
                break
            page += 1

        if dropped_stages:
            logger.info("yc: dropped %s, kept %d", dropped_stages, len(results))
        return results

    async def _discover_algolia_credentials(self, client: HomepageClient) -> tuple[str, str]:
        """Fetch the portfolio HTML and extract the ``window.AlgoliaOpts`` blob."""
        page_html = (await client.fetch(self.PORTFOLIO_URL)).content
        match = self._ALGOLIA_OPTS_RE.search(page_html)
        if not match:
            raise RuntimeError(
                "yc: could not locate window.AlgoliaOpts in portfolio page; "
                "the page structure likely changed."
            )
        try:
            opts = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"yc: AlgoliaOpts blob is not valid JSON: {exc}") from exc
        app_id = opts.get("app")
        api_key = opts.get("key")
        if not isinstance(app_id, str) or not isinstance(api_key, str):
            raise RuntimeError("yc: AlgoliaOpts missing app/key fields")
        return app_id, api_key

    async def _algolia_page(
        self,
        client: HomepageClient,
        algolia_url: str,
        app_id: str,
        api_key: str,
        page: int,
    ) -> dict[str, Any]:
        """POST one Algolia search request and return ``results[0]``.

        Posts directly via the underlying httpx client. We bypass the
        ``HomepageClient.fetch`` path because Algolia speaks JSON over POST and
        the helper is built for GET HTML. The throttle is still respected at
        the network layer (Algolia's CDN handles rate limiting itself).
        """
        client._assert_open()
        underlying: httpx.AsyncClient = client._client  # type: ignore[assignment]
        payload = {
            "requests": [
                {
                    "indexName": self.INDEX_NAME,
                    "params": f"hitsPerPage={self.HITS_PER_PAGE}&page={page}",
                }
            ]
        }
        resp = await underlying.post(
            algolia_url,
            json=payload,
            headers={
                "x-algolia-application-id": app_id,
                "x-algolia-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        results = body.get("results")
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"yc: unexpected Algolia response shape: {body!r}")
        first = results[0]
        if not isinstance(first, dict):
            raise RuntimeError(f"yc: unexpected Algolia results entry: {first!r}")
        return first
