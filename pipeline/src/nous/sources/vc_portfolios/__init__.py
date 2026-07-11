"""VC portfolio adapters.

One adapter per supported firm, registered in :data:`ADAPTERS` and consumed by
the M3 ``vc-portfolios`` pipeline stage (Chunk 6b). Each adapter conforms to
:class:`PortfolioAdapter` — instantiable, with ``firm`` and ``fetch``.

Benchmark is intentionally absent: its entire public site (benchmark.com) is a
single splash page (logo + office addresses) with no portfolio listing anywhere,
so there is nothing to scrape. See the M5 plan — documented and skipped rather
than guessed.

Accelerator portfolios are also intentionally absent (investigated 2026-07 for
the discovery-expansion workstream; YC is already covered above). Every
candidate renders its company list client-side, with no server-side fallback
an httpx adapter could parse deterministically:

- **Techstars** (techstars.com/portfolio): Next.js pages-router whose
  ``__NEXT_DATA__`` carries only CMS page chrome; the company grid loads at
  runtime from an endpoint not discoverable in the served HTML/JS bundles,
  and the site publishes no sitemap.
- **500 Global** (500.co/companies): Next.js RSC app shell; the flight
  payload contains zero company documents and the Strapi sitemap lists only
  regional *listing* pages, no per-company URLs.
- **Antler** (antler.co/portfolio): Webflow + Finsweet CMS-filter — items are
  fetched client-side; the sitemap has no company URLs and robots.txt
  disallows ``/portfolio/company/``.
- **Alchemist** (alchemistaccelerator.com/portfolio): the company grid is a
  client-side ``${name}`` JS template over a runtime payload.

A headless-browser adapter is possible but belongs in a deliberate follow-up
(it would be this package's only JS-rendering scraper); a scrape of the empty
shells would ship a permanently-flaky adapter instead.
"""

from __future__ import annotations

from nous.sources.vc_portfolios.a16z import A16zAdapter
from nous.sources.vc_portfolios.accel import AccelAdapter
from nous.sources.vc_portfolios.base import (
    AdapterStructuralError,
    PortfolioAdapter,
    PortfolioEntry,
)
from nous.sources.vc_portfolios.bessemer import BessemerAdapter
from nous.sources.vc_portfolios.felicis import FelicisAdapter
from nous.sources.vc_portfolios.founders_fund import FoundersFundAdapter
from nous.sources.vc_portfolios.general_catalyst import GeneralCatalystAdapter
from nous.sources.vc_portfolios.greylock import GreylockAdapter
from nous.sources.vc_portfolios.index_ventures import IndexVenturesAdapter
from nous.sources.vc_portfolios.khosla import KhoslaAdapter
from nous.sources.vc_portfolios.kleiner_perkins import KleinerPerkinsAdapter
from nous.sources.vc_portfolios.lightspeed import LightspeedAdapter
from nous.sources.vc_portfolios.sequoia import SequoiaAdapter
from nous.sources.vc_portfolios.yc import YcAdapter

ADAPTERS: dict[str, PortfolioAdapter] = {
    "yc": YcAdapter(),
    "a16z": A16zAdapter(),
    "sequoia": SequoiaAdapter(),
    "lightspeed": LightspeedAdapter(),
    "founders_fund": FoundersFundAdapter(),
    "greylock": GreylockAdapter(),
    "khosla": KhoslaAdapter(),
    # M5 additions
    "bessemer": BessemerAdapter(),
    "index_ventures": IndexVenturesAdapter(),
    "accel": AccelAdapter(),
    "felicis": FelicisAdapter(),
    "kleiner_perkins": KleinerPerkinsAdapter(),
    "general_catalyst": GeneralCatalystAdapter(),
}

# Proper investor display names per firm slug — used when recording the
# discovering firm as a company-level investor (refresh-vc-portfolios).
FIRM_DISPLAY_NAMES: dict[str, str] = {
    "yc": "Y Combinator",
    "a16z": "Andreessen Horowitz",
    "sequoia": "Sequoia Capital",
    "lightspeed": "Lightspeed Venture Partners",
    "founders_fund": "Founders Fund",
    "greylock": "Greylock",
    "khosla": "Khosla Ventures",
    "bessemer": "Bessemer Venture Partners",
    "index_ventures": "Index Ventures",
    "accel": "Accel",
    "felicis": "Felicis Ventures",
    "kleiner_perkins": "Kleiner Perkins",
    "general_catalyst": "General Catalyst",
}

__all__ = [
    "ADAPTERS",
    "FIRM_DISPLAY_NAMES",
    "AdapterStructuralError",
    "PortfolioAdapter",
    "PortfolioEntry",
]
