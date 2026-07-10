"""Title lookup from TMDB.

Sonarr/Radarr search under the title they know (often English, or a foreign
alias like "Die Schläfer"), but many Webshare files use the original (e.g.
Czech) name. TMDB gives us both: the canonical display title (`name`/`title`)
and the original one (`original_name`/`original_title`). We use the original as
an extra search term, and the canonical one as the release-name prefix so
Sonarr shows "The Sleepers", not whatever alias happened to match.

Returns are cached in-memory (including negatives) — the same query repeats a
lot. Each result is a (display, original, language) tuple: `original` is "" when
it equals the display title (i.e. not foreign); `language` is the title's TMDB
`original_language` (ISO 639-1, e.g. "en"/"cs"), used to tag the release so
Sonarr/Radarr can honour an "original language" policy.
"""

import logging

import httpx

logger = logging.getLogger("websharr.tmdb")

_BASE = "https://api.themoviedb.org/3"
_cache: dict[tuple[str, str], tuple[str, str, str] | None] = {}


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


def _language(entry: dict) -> str:
    return (entry.get("original_language") or "").strip().lower()


async def lookup(token: str, kind: str, query: str) -> tuple[str, str, str] | None:
    """(display, original, language) for a TMDB `tv`/`movie` matched by name.

    Fulltext — prefers the first result with a foreign original title (the one
    we want) for the display/original names; can still pick wrong for ambiguous
    names, so the id lookup is preferred and the manual alias overrides. The
    language always comes from the best match (results[0]), so even a title with
    no foreign original (e.g. an English show) still yields its language. When no
    foreign original is found the display/original are empty (don't override the
    query name) but the language is still returned. None only when nothing matched.
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
    if not results:
        _cache[key] = None
        return None
    lang = _language(results[0])
    disp_out, orig_out = "", ""
    for entry in results[:5]:
        disp, orig = _titles(entry, kind)
        if orig:
            disp_out, orig_out = disp, orig
            lang = _language(entry) or lang
            break
    found = (disp_out, orig_out, lang)
    _cache[key] = found
    logger.info("TMDB: %s %r -> display=%r original=%r lang=%r",
                kind, query, disp_out, orig_out, lang)
    return found


def _imdb_id(imdbid) -> str:
    """TMDB's /find expects the tt-prefixed IMDb id; Sonarr often sends it bare."""
    if not imdbid:
        return ""
    s = str(imdbid).strip()
    if not s:
        return ""
    return s if s.lower().startswith("tt") else "tt" + s


async def lookup_by_id(token: str, kind: str, tmdbid=None, imdbid=None,
                       tvdbid=None) -> tuple[str, str, str] | None:
    """(display, original, language) from an exact external id.

    Tries each supplied id in turn — direct TMDB id, then TVDB, then IMDb — and
    uses the first that resolves, so a Sonarr search carrying both tvdbid and
    imdbid still works if one source has no TMDB mapping.
    """
    if not token or kind not in ("tv", "movie"):
        return None
    imdb = _imdb_id(imdbid)
    if not (tmdbid or imdb or tvdbid):
        return None
    key = (kind, f"id:{tmdbid}:{tvdbid}:{imdb}")
    if key in _cache:
        return _cache[key]

    async def _find(client, ext, source):
        r = await client.get(f"{_BASE}/find/{ext}", params={"external_source": source})
        if r.status_code != 200:
            return {}
        hits = r.json().get(f"{kind}_results") or []
        return hits[0] if hits else {}

    entry: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_headers(token)) as client:
            if tmdbid:
                r = await client.get(f"{_BASE}/{kind}/{tmdbid}")
                if r.status_code == 200:
                    entry = r.json()
            if not entry and tvdbid:
                entry = await _find(client, tvdbid, "tvdb_id")
            if not entry and imdb:
                entry = await _find(client, imdb, "imdb_id")
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB id lookup %s (tmdb=%s tvdb=%s imdb=%s) failed: %s",
                       kind, tmdbid, tvdbid, imdb, exc)
        return None
    disp, orig = _titles(entry, kind) if entry else ("", "")
    lang = _language(entry) if entry else ""
    found = (disp, orig, lang) if (disp or orig or lang) else None
    _cache[key] = found
    if found:
        logger.info("TMDB: %s id(tmdb=%s tvdb=%s imdb=%s) -> display=%r original=%r lang=%r",
                    kind, tmdbid, tvdbid, imdb, disp, orig, lang)
    return found
