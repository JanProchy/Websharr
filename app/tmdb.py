"""Title lookup from TMDB.

Sonarr/Radarr search under the title they know (often English, or a foreign
alias like "Die Schläfer"), but many Webshare files use the original (e.g.
Czech) name. TMDB gives us both: the canonical display title (`name`/`title`)
and the original one (`original_name`/`original_title`). We use the original as
an extra search term, and the canonical one as the release-name prefix so
Sonarr shows "The Sleepers", not whatever alias happened to match.

Returns are cached in-memory (including negatives) — the same query repeats a
lot. Each result is a (display, original) tuple; `original` is "" when it
equals the display title (i.e. not foreign).
"""

import logging

import httpx

logger = logging.getLogger("websharr.tmdb")

_BASE = "https://api.themoviedb.org/3"
_cache: dict[tuple[str, str], tuple[str, str] | None] = {}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


def _titles(entry: dict, kind: str) -> tuple[str, str]:
    """(display, original) for a TMDB entry; original="" if same as display."""
    if kind == "tv":
        disp, orig = entry.get("name", ""), entry.get("original_name", "")
    else:
        disp, orig = entry.get("title", ""), entry.get("original_title", "")
    disp, orig = (disp or "").strip(), (orig or "").strip()
    return disp, (orig if orig and orig.casefold() != disp.casefold() else "")


async def lookup(token: str, kind: str, query: str) -> tuple[str, str] | None:
    """(display, original) for a TMDB `tv`/`movie` matched by name, or None.

    Fulltext — prefers the first result with a foreign original title (the one
    we want); can still pick wrong for ambiguous names, so the id lookup is
    preferred and the manual alias overrides.
    """
    query = (query or "").strip()
    if not token or not query or kind not in ("tv", "movie"):
        return None
    key = (kind, query.casefold())
    if key in _cache:
        return _cache[key]
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_headers(token)) as client:
            resp = await client.get(f"{_BASE}/search/{kind}", params={"query": query})
            resp.raise_for_status()
            results = resp.json().get("results") or []
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB search %r (%s) failed: %s", query, kind, exc)
        return None
    found = None
    for entry in results[:5]:
        disp, orig = _titles(entry, kind)
        if orig:
            found = (disp, orig)
            break
    _cache[key] = found
    if found:
        logger.info("TMDB: %s %r -> %r (original %r)", kind, query, found[0], found[1])
    return found


async def lookup_by_id(token: str, kind: str, tmdbid=None, imdbid=None,
                       tvdbid=None) -> tuple[str, str] | None:
    """(display, original) from an exact external id (tmdb/imdb/tvdb)."""
    if not token or kind not in ("tv", "movie"):
        return None
    ext = tmdbid or imdbid or tvdbid
    if not ext:
        return None
    key = (kind, f"id:{ext}")
    if key in _cache:
        return _cache[key]
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
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB id lookup %s=%s failed: %s", kind, ext, exc)
        return None
    disp, orig = _titles(entry, kind) if entry else ("", "")
    found = (disp, orig) if (disp or orig) else None
    _cache[key] = found
    if found:
        logger.info("TMDB: %s id=%s -> %r (original %r)", kind, ext, found[0], found[1])
    return found
