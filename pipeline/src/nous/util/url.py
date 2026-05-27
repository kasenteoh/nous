"""URL canonicalization and hostname helpers.

Used to deduplicate news article URLs across feeds (Google News and TechCrunch
will often surface the same article with different tracking parameters).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query-param prefixes that we always strip — tracking-only params that don't
# affect the destination page. UTM is the obvious family; we also drop the
# Google-Click-ID (gclid), Facebook (fbclid), and a handful of common
# newsletter-tracker keys.
_TRACKING_PREFIXES: tuple[str, ...] = ("utm_",)
_TRACKING_EXACT: frozenset[str] = frozenset(
    {
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "ref",
        "ref_src",
    }
)

# Default ports we strip from the host. ``urlsplit`` preserves them when
# present even if they're the protocol default.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def canonical_url(url: str) -> str:
    """Return a canonical form of ``url`` suitable for dedup.

    Rules:
    - Scheme and host are lowercased.
    - Default ports (80 for http, 443 for https) are stripped.
    - Query params whose names start with a tracking prefix (e.g. ``utm_``) or
      match a known tracking key (``gclid``, ``fbclid``, ...) are dropped.
    - Remaining query params are kept in their original order.
    - Fragment (``#anchor``) is dropped.
    - A trailing ``/`` is stripped from the path, except when the path is
      exactly ``/`` (the root).

    Two URLs that differ only in tracking params, port-defaults, fragment, or
    trailing slash canonicalize to the same string.
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    # urlsplit's .hostname already lowercases; be defensive.
    host = host.lower()

    netloc = host
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    # Preserve userinfo if present (rare for news URLs but be safe).
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Filter tracking params while preserving order.
    kept_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in _TRACKING_EXACT:
            continue
        if any(key_lower.startswith(prefix) for prefix in _TRACKING_PREFIXES):
            continue
        kept_params.append((key, value))
    query = urlencode(kept_params)

    # Always drop the fragment.
    fragment = ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def hostname(url: str) -> str:
    """Return the lowercased hostname for ``url``, with any leading ``www.`` stripped.

    Returns an empty string if the URL has no host (e.g. relative URLs).
    """
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host
