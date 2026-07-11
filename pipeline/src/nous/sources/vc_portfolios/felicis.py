"""Felicis Ventures portfolio adapter.

https://www.felicis.com/portfolio is a Next.js (App Router / RSC) page backed by
Sanity. There's no static company list in the DOM and no clean JSON island;
instead each company is a Sanity document embedded in the React Flight payload
across ``self.__next_f.push([1,"..."])`` chunks. We concatenate the chunk
bodies, undo the JS-string escaping, then walk the resulting text pulling out
each balanced ``{...}`` object whose ``"_type"`` is ``"company"``.

The company objects carry ``name``, ``slug.current`` and an ``excerpt``
(short tagline). The homepage URL is only present as a Sanity reference
(``"domains":"$NN"``), not a literal, so ``website`` is ``None`` and
resolve-homepages fills it in later.
"""

from __future__ import annotations

import codecs
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from nous.sources.homepage import HomepageClient
from nous.sources.vc_portfolios._json_island import extract_balanced
from nous.sources.vc_portfolios.base import PortfolioEntry, ensure_entries

logger = logging.getLogger(__name__)

_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', re.DOTALL)
# Company docs appear as {"_id":"<uuid>","_type":"company", ...}. Anchor on that
# prefix, then extract the balanced object from the opening brace.
_COMPANY_START_RE = re.compile(r'\{"_id":"[^"]+","_type":"company"')


class FelicisAdapter:
    firm = "felicis"
    PORTFOLIO_URL = "https://www.felicis.com/portfolio"

    async def fetch(self, client: HomepageClient) -> list[PortfolioEntry]:
        html = (await client.fetch(self.PORTFOLIO_URL)).content
        payload = _decode_rsc_payload(html)
        entries: list[PortfolioEntry] = []
        seen: set[str] = set()
        for obj in _iter_company_objects(payload):
            raw_name = obj.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            name = raw_name.strip()
            if name in seen:
                continue
            seen.add(name)
            entries.append(
                PortfolioEntry(
                    firm=self.firm,
                    name=name,
                    website=None,
                    description=_clean_excerpt(obj.get("excerpt")),
                    source_url=self.PORTFOLIO_URL,
                )
            )
        return ensure_entries(
            entries,
            self.firm,
            context="no company objects found in the RSC payload",
        )


def _decode_rsc_payload(html: str) -> str:
    """Concatenate the __next_f chunk bodies and undo JS-string escaping."""
    chunks = _CHUNK_RE.findall(html)
    if not chunks:
        return ""
    return codecs.decode("".join(chunks), "unicode_escape", "replace")


def _iter_company_objects(payload: str) -> Iterator[dict[str, Any]]:
    for match in _COMPANY_START_RE.finditer(payload):
        obj_text = extract_balanced(payload, match.start())
        if obj_text is None:
            continue
        try:
            obj = json.loads(obj_text)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _clean_excerpt(value: object) -> str | None:
    # Sanity references show up as "$NN"; keep only real literal taglines.
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and not stripped.startswith("$"):
            return stripped
    return None
