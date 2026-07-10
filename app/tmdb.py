"""Best-effort original-title lookup from TMDB.

Sonarr/Radarr search under the title they know (usually English), but many
Webshare files use the show/movie's original (e.g. Czech) name. TMDB stores
that in `original_name`/`original_title`, so we look it up and let the Webshare
search try it too. Results are cached in-memory (including negatives) to keep
API calls down — the same query repeats a lot.
"""

import logging

import httpx

logger = logging.getLogger("websharr.tmdb")

_BASE = "https://api.themoviedb.org/3"
_cache: dict[tuple[str, str], str] = {}  # (kind, key) -> original title ("" = none)


def _original(entry: dict, kind: str) -> str:
    """The original title if it differs from the display title, else ""."""
    if kind == "tv":
        orig, disp = entry.get("original_name", ""), entry.get("name", "")
    else:
        orig, disp = entry.get("original_title", ""), entry.get("title", "")
    orig = (orig or "").strip()
    return orig if orig and orig.casefold() != (disp or "").strip().casefold() else ""


async def czech_title(token: str, kind: str, query: str) -> str | None:
    """Original title for a TMDB `tv`/`movie` matching `query` by name.

    Fulltext — takes TMDB's top result, so it can pick the wrong entry for
    ambiguous names; prefer the id-based lookup, and the manual alias overrides.
    """
    query = (query or "").strip()
    if not token or not query or kind not in ("tv", "movie"):
        return None
    key = (kind, query.casefold())
    if key in _cache:
        return _cache[key] or None
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_headers(token)) as client:
            resp = await client.get(f"{_BASE}/search/{kind}", params={"query": query})
            resp.raise_for_status()
            results = resp.json().get("results") or []
            orig = _original(results[0], kind) if results else ""
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB search %r (%s) failed: %s", query, kind, exc)
        return None
    _cache[key] = orig
    if orig:
        logger.info("TMDB: %s %r -> original title %r", kind, query, orig)
    return orig or None


async def czech_title_by_id(token: str, kind: str, tmdbid=None, imdbid=None,
                            tvdbid=None) -> str | None:
    """Original title from an exact external id (tmdb/imdb/tvdb) — no guessing.

    *arr sends these when the Newznab caps advertise id support.
    """
    if not token or kind not in ("tv", "movie"):
        return None
    ext = tmdbid or imdbid or tvdbid
    if not ext:
        return None
    key = (kind, f"id:{ext}")
    if key in _cache:
        return _cache[key] or None
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_headers(token)) as client:
            if tmdbid:
                r = await client.get(f"{_BASE}/{kind}/{tmdbid}")
                r.raise_for_status()
                entry = r.json()
            else:
                source = "imdb_id" if imdbid else "tvdb_id"
                r = await client.get(f"{_BASE}/find/{ext}",
                                     params={"external_source": source})
                r.raise_for_status()
                hits = r.json().get(f"{kind}_results") or []
                entry = hits[0] if hits else {}
            orig = _original(entry, kind)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB id lookup %s=%s failed: %s", kind, ext, exc)
        return None
    _cache[key] = orig
    if orig:
        logger.info("TMDB: %s id=%s -> original title %r", kind, ext, orig)
    return orig or None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}
