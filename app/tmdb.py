"""Best-effort Czech-title lookup from TMDB.

When a Sonarr/Radarr search arrives under the title *arr knows (usually
English), we look the show/movie up on TMDB and return its Czech title, so the
Webshare search can also try the name CZ files actually use. Results are cached
in-memory (including negatives) to keep API calls down — the same query repeats
a lot.
"""

import logging

import httpx

logger = logging.getLogger("websharr.tmdb")

_BASE = "https://api.themoviedb.org/3"
_cache: dict[tuple[str, str], str] = {}  # (kind, query) -> czech title ("" = none)


async def _czech_from_tmdb_id(client: httpx.AsyncClient, kind: str, tmdb_id) -> str:
    tr = await client.get(f"{_BASE}/{kind}/{tmdb_id}/translations")
    tr.raise_for_status()
    for t in tr.json().get("translations", []):
        if t.get("iso_639_1") == "cs":
            data = t.get("data") or {}
            name = (data.get("name") or data.get("title") or "").strip()
            if name:
                return name
    return ""


async def czech_title_by_id(token: str, kind: str, tmdbid=None, imdbid=None,
                            tvdbid=None) -> str | None:
    """Czech title from an exact external id (tmdb/imdb/tvdb) — no guessing.

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

    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            tmdb_id = tmdbid
            if not tmdb_id:  # resolve external id -> tmdb id
                source = "imdb_id" if imdbid else "tvdb_id"
                r = await client.get(f"{_BASE}/find/{ext}",
                                     params={"external_source": source})
                r.raise_for_status()
                hits = r.json().get(f"{kind}_results") or []
                if not hits:
                    _cache[key] = ""
                    return None
                tmdb_id = hits[0]["id"]
            czech = await _czech_from_tmdb_id(client, kind, tmdb_id)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB id lookup %s=%s (%s) failed: %s", kind, ext, kind, exc)
        return None

    _cache[key] = czech
    if czech:
        logger.info("TMDB: %s id=%s -> Czech title %r", kind, ext, czech)
    return czech or None


async def czech_title(token: str, kind: str, query: str) -> str | None:
    """Czech title for a TMDB `tv`/`movie` matching `query`, or None.

    Fulltext match — takes TMDB's top result, so it can be wrong for ambiguous
    names; the manual alias map is the override for those.
    """
    query = (query or "").strip()
    if not token or not query or kind not in ("tv", "movie"):
        return None
    key = (kind, query.lower())
    if key in _cache:
        return _cache[key] or None

    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(f"{_BASE}/search/{kind}", params={"query": query})
            resp.raise_for_status()
            results = resp.json().get("results") or []
            if not results:
                _cache[key] = ""
                return None
            tmdb_id = results[0]["id"]

            tr = await client.get(f"{_BASE}/{kind}/{tmdb_id}/translations")
            tr.raise_for_status()
            czech = ""
            for t in tr.json().get("translations", []):
                if t.get("iso_639_1") == "cs":
                    data = t.get("data") or {}
                    czech = (data.get("name") or data.get("title") or "").strip()
                    if czech:
                        break
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB lookup for %r (%s) failed: %s", query, kind, exc)
        return None  # don't cache transient failures

    _cache[key] = czech
    if czech:
        logger.info("TMDB: %s %r -> Czech title %r", kind, query, czech)
    return czech or None
