"""VC portfolio adapters.

One adapter per supported firm, registered in :data:`ADAPTERS` and consumed by
the M3 ``vc-portfolios`` pipeline stage (Chunk 6b). Each adapter conforms to
:class:`PortfolioAdapter` — instantiable, with ``firm`` and ``fetch``.

Benchmark is intentionally absent: its entire public site (benchmark.com) is a
single splash page (logo + office addresses) with no portfolio listing anywhere,
so there is nothing to scrape. See the M5 plan — documented and skipped rather
than guessed.
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
