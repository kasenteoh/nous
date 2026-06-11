"""VC portfolio adapters.

One adapter per supported firm, registered in :data:`ADAPTERS` and consumed by
the M3 ``vc-portfolios`` pipeline stage (Chunk 6b). Each adapter conforms to
:class:`PortfolioAdapter` — instantiable, with ``firm`` and ``fetch``.
"""

from __future__ import annotations

from nous.sources.vc_portfolios.a16z import A16zAdapter
from nous.sources.vc_portfolios.base import PortfolioAdapter, PortfolioEntry
from nous.sources.vc_portfolios.founders_fund import FoundersFundAdapter
from nous.sources.vc_portfolios.greylock import GreylockAdapter
from nous.sources.vc_portfolios.khosla import KhoslaAdapter
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
}

__all__ = ["ADAPTERS", "FIRM_DISPLAY_NAMES", "PortfolioAdapter", "PortfolioEntry"]
