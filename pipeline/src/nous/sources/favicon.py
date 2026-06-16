"""Logo / favicon discovery for company homepages.

Company cards and headers want a small logo. The cheapest, attribution-clean
source is the company's own favicon / apple-touch-icon — already declared in
the homepage ``<head>`` and served from the company's own domain. We never
download or re-host the image: ``companies.logo_url`` stores the *external*
URL on the company's domain, and the frontend ``<img>`` loads it directly.

Two layers:

- :func:`best_logo_candidate` — pure. Given the homepage HTML + the URL it was
  fetched from, return the single best logo-URL *candidate* (no network):
  parse ``<link rel="icon" | "apple-touch-icon" | "shortcut icon">``, prefer
  the apple-touch-icon (a real raster logo, sized for home-screen tiles) and
  otherwise the highest declared ``sizes`` resolution, resolve to an absolute
  URL, and fall back to ``<origin>/favicon.ico`` when nothing is declared.

- :func:`fetch_logo_url` — async. Take the candidate, verify it *actually*
  resolves to an image (SSRF-guarded; ``content-type: image/*``; non-empty and
  not absurdly large) and return the validated absolute URL, else ``None``.
  The validation matters because ``/favicon.ico`` frequently 200s with the
  site's SPA shell (``text/html``) rather than an image, and we must not store
  an HTML page as a logo.

SSRF: every network call goes through ``assert_public_url`` first (the same
guard used by the curl_cffi / Playwright paths in this package), so a candidate
host that resolves to a loopback / link-local / RFC-1918 address is rejected
before a socket is opened — even though the passed-in client is itself expected
to be SSRF-guarded. Belt and suspenders, by design.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from nous.util.ssrf import BlockedAddressError, assert_public_url

__all__ = ["best_logo_candidate", "fetch_logo_url"]

logger = logging.getLogger(__name__)

# rel-attribute tokens (lower-cased, whitespace-split) that mark an icon link.
# HTML allows multiple space-separated tokens, e.g. ``rel="shortcut icon"``.
_ICON_REL_TOKENS: frozenset[str] = frozenset({"icon", "shortcut", "apple-touch-icon"})
# Tokens that specifically denote an apple-touch-icon (incl. the -precomposed
# variant). These are real raster logos and the best card thumbnail available.
_APPLE_REL_TOKENS: frozenset[str] = frozenset(
    {"apple-touch-icon", "apple-touch-icon-precomposed"}
)

# Favicons are tiny. Anything past this is almost certainly a mis-tagged hero
# image or a tarpit; reject by Content-Length without downloading the body.
# 2 MiB is comfortably above even large 512×512 PNG/ICO app icons.
_MAX_LOGO_BYTES: int = 2 * 1024 * 1024

# Per-request timeout for the lightweight validation probe. The homepage fetch
# already paid the slow-DNS / TLS cost for this origin; the icon is a small
# same-host asset, so keep this short and never let it stall a scrape.
_VALIDATE_TIMEOUT_S: float = 10.0


def _rel_tokens(node_rel: str | None) -> set[str]:
    """Lower-cased, whitespace-split tokens of a ``rel`` attribute."""
    if not node_rel:
        return set()
    return {tok for tok in node_rel.lower().split() if tok}


def _parse_sizes(sizes_attr: str | None) -> int:
    """Return the max edge length declared in a ``sizes`` attribute.

    ``sizes="16x16 32x32"`` → 32. ``sizes="any"`` (SVG, scalable) sorts highest.
    Missing / unparseable → 0 (lowest priority among declared icons).
    """
    if not sizes_attr:
        return 0
    value = sizes_attr.strip().lower()
    if value == "any":
        # Scalable (typically SVG): treat as the highest resolution available.
        return 1 << 30
    best = 0
    for token in value.split():
        # token like "32x32" / "180x180"; take the first integer dimension.
        head = token.split("x", 1)[0]
        try:
            best = max(best, int(head))
        except ValueError:
            continue
    return best


def _is_fetchable_href(href: str) -> bool:
    """True if ``href`` is something we can resolve to a fetchable http(s) URL.

    Rejects empty hrefs and non-fetchable schemes (``data:``, ``javascript:``,
    ``mailto:``). Protocol-relative (``//cdn/...``) and relative hrefs are fine —
    :func:`urljoin` resolves them against the base.
    """
    if not href:
        return False
    lowered = href.strip().lower()
    return not lowered.startswith(("data:", "javascript:", "mailto:", "about:"))


def best_logo_candidate(html: str, base_url: str) -> str | None:
    """Return the best logo-URL candidate from a homepage's HTML, or ``None``.

    Pure (no network). Preference order:
      1. ``apple-touch-icon`` / ``apple-touch-icon-precomposed`` links (real
         raster logos), highest declared ``sizes`` first.
      2. Other icon links (``rel`` containing ``icon`` / ``shortcut icon``),
         highest declared ``sizes`` first.
      3. ``<origin>/favicon.ico`` as a last resort (the well-known default).

    All hrefs are resolved to absolute URLs against ``base_url``. Inline
    ``data:`` icons are ignored (cannot be hosted/linked). Returns ``None`` only
    when ``base_url`` has no usable http(s) host (so even ``/favicon.ico`` can't
    be constructed).
    """
    base_parts = urlsplit(base_url)
    if base_parts.scheme not in ("http", "https") or not base_parts.hostname:
        return None
    origin = f"{base_parts.scheme}://{base_parts.netloc}"

    tree = HTMLParser(html or "")

    # (is_apple, size, absolute_url) — sort so apple-touch-icon and larger sizes
    # come first. Negate for ascending sort → most-preferred at index 0.
    scored: list[tuple[int, int, str]] = []
    for node in tree.css("link[rel]"):
        tokens = _rel_tokens(node.attributes.get("rel"))
        if tokens.isdisjoint(_ICON_REL_TOKENS):
            continue
        href = (node.attributes.get("href") or "").strip()
        if not _is_fetchable_href(href):
            continue
        absolute = urljoin(base_url, href)
        abs_parts = urlsplit(absolute)
        if abs_parts.scheme not in ("http", "https") or not abs_parts.hostname:
            continue
        is_apple = 1 if tokens & _APPLE_REL_TOKENS else 0
        size = _parse_sizes(node.attributes.get("sizes"))
        scored.append((-is_apple, -size, absolute))

    if scored:
        scored.sort()
        return scored[0][2]

    # Nothing declared — fall back to the well-known default location.
    return urljoin(origin + "/", "favicon.ico")


def _content_type_is_image(resp: httpx.Response) -> bool:
    """True if the response advertises an image content-type."""
    raw: str = resp.headers.get("content-type", "")
    ctype = raw.split(";")[0].strip().lower()
    return ctype.startswith("image/")


def _content_length_ok(resp: httpx.Response) -> bool:
    """Validate Content-Length against the favicon size envelope.

    Rejects an explicit ``0`` (an empty body is not a logo — common when a HEAD
    to a missing icon 200s with no content) and anything past ``_MAX_LOGO_BYTES``
    (a mis-tagged hero image / tarpit). A missing or garbage Content-Length is
    allowed (many CDNs omit it on HEAD); the empty-GET guard in the caller
    covers the no-Content-Length empty-body case.
    """
    raw = resp.headers.get("content-length")
    if raw is None:
        return True
    try:
        length = int(raw)
    except ValueError:
        return True
    return 0 < length <= _MAX_LOGO_BYTES


async def fetch_logo_url(
    client: httpx.AsyncClient,
    html: str,
    base_url: str,
) -> str | None:
    """Pick the best logo candidate and verify it resolves to a real image.

    ``client`` should be an SSRF-guarded ``httpx.AsyncClient`` (as built by
    :func:`nous.util.ssrf.guarded_async_client`); regardless, this function
    independently runs :func:`assert_public_url` on the candidate before any
    request, so an internal-resolving host is rejected even if a non-guarded
    client is passed.

    Validation: a HEAD (falling back to GET on 405/501) must return 2xx with a
    ``content-type: image/*`` and a plausible size. On any failure — non-image,
    non-2xx, oversized, empty body, SSRF block, or network error — returns
    ``None``. Never raises: logo discovery is strictly best-effort and must not
    break a scrape.

    Returns the validated *absolute* candidate URL (on the company's own
    domain) — the caller stores it as-is; the image is not downloaded/hosted.
    """
    candidate = best_logo_candidate(html, base_url)
    if candidate is None:
        return None

    try:
        # Independent SSRF check before any socket — mirrors the curl_cffi /
        # Playwright paths. A candidate host resolving to loopback/link-local/
        # RFC-1918 is rejected here, never fetched.
        await assert_public_url(candidate)

        resp = await _probe(client, candidate)
        if resp is None:
            return None

        if not (200 <= resp.status_code < 300):
            return None
        if not _content_type_is_image(resp):
            logger.debug(
                "favicon: %s is not an image (content-type=%r) — skipping",
                candidate,
                resp.headers.get("content-type"),
            )
            return None
        if not _content_length_ok(resp):
            logger.debug("favicon: %s exceeds size cap — skipping", candidate)
            return None
        # Guard the empty-body case the Content-Length check can miss (e.g. a
        # GET that streamed nothing): an image with no bytes is not a logo.
        if resp.request.method == "GET" and len(resp.content) == 0:
            return None
        return candidate
    except BlockedAddressError as exc:
        logger.debug("favicon: SSRF guard blocked %s: %s", candidate, exc)
        return None
    except httpx.HTTPError as exc:
        logger.debug(
            "favicon: probe failed for %s: %s: %s",
            candidate,
            type(exc).__name__,
            exc,
        )
        return None


async def _probe(
    client: httpx.AsyncClient, url: str
) -> httpx.Response | None:
    """HEAD ``url``, falling back to a GET when HEAD is unsupported (405/501).

    Returns the response to validate, or ``None`` if even the GET could not be
    issued. A HEAD avoids downloading the image body for the common case; the
    GET fallback covers servers (and some CDNs) that reject HEAD.
    """
    try:
        resp = await client.head(url, timeout=_VALIDATE_TIMEOUT_S)
    except httpx.HTTPError:
        # HEAD is optional — retry with GET before giving up.
        resp = None
    if resp is not None and resp.status_code not in (405, 501):
        return resp
    return await client.get(url, timeout=_VALIDATE_TIMEOUT_S)
