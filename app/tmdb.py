"""Title lookup from TMDB.

Sonarr/Radarr search under the title they know (often English, or a foreign
alias like "Die Schläfer"), but many Webshare files use the original (e.g.
Czech) name. TMDB gives us both: the canonical display title (`name`/`title`)
and the original one (`original_name`/`original_title`). We use the original as
an extra search term, and the canonical one as the release-name prefix so
Sonarr shows "The Sleepers", not whatever alias happened to match.

Many titles are English-origin but dubbed on Webshare under a Czech name
("DuckTales" -> "Kačeří příběhy"); that name is not the original title but a
TMDB *alternative title* (country CZ), so those are fetched too and returned
as extra search terms.

Returns are cached in-memory (including negatives) — the same query repeats a
lot. Each result is a (display, original, language, czech_titles) tuple:
`original` is "" when it equals the display title (i.e. not foreign);
`language` is the title's TMDB `original_language` (ISO 639-1, e.g. "en"/"cs"),
used to tag the release so Sonarr/Radarr can honour an "original language"
policy; `czech_titles` are the CZ alternative titles (empty for a Czech-origin
title, whose original already is the Czech name).
"""

import logging

import httpx

logger = logging.getLogger("websharr.tmdb")

_BASE = "https://api.themoviedb.org/3"
_cache: dict[tuple[str, str], tuple[str, str, str, tuple[str, ...]] | None] = {}


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


# Countries whose alternative title we treat as the English name Sonarr/Radarr
# use for a title that has no English name of its own (its `name` is the foreign
# original). Sonarr matches releases by that title, so the release prefix must be it.
_ENGLISH_COUNTRIES = {"US", "GB", "CA", "AU", "NZ", "IE"}


def _pick_english(items: list) -> str:
    """First English-country alternative title from a TMDB alternative_titles list."""
    for it in items or []:
        if (it.get("iso_3166_1") or "").upper() in _ENGLISH_COUNTRIES:
            title = (it.get("title") or "").strip()
            if title:
                return title
    return ""


def _pick_czech(items: list) -> list[str]:
    """All CZ alternative titles from a TMDB alternative_titles list.

    A show can have several — successive dubs used different names ("Kačeří
    příběhy" and "My z Kačerova") and Webshare files exist under each.
    """
    out = []
    for it in items or []:
        if (it.get("iso_3166_1") or "").upper() == "CZ":
            title = (it.get("title") or "").strip()
            if title and title.casefold() not in {t.casefold() for t in out}:
                out.append(title)
    return out


async def _alt_titles(client, kind: str, tmdb_id) -> list:
    """The TMDB alternative_titles list for an id, or [] if none/failed."""
    if not tmdb_id:
        return []
    try:
        r = await client.get(f"{_BASE}/{kind}/{tmdb_id}/alternative_titles")
        if r.status_code != 200:
            return []
        body = r.json()
        # TV returns "results", movie returns "titles".
        return body.get("results") or body.get("titles") or []
    except (httpx.HTTPError, KeyError, ValueError):
        return []


async def _resolve(client, entry: dict, kind: str) -> tuple[str, str, str, tuple[str, ...]]:
    """(display, original, language, czech_titles) for a TMDB entry.

    When the canonical name is itself the foreign original (no distinct English
    name), swap in the English alternative title as the display — that's what
    Sonarr/Radarr call the show and match releases against — and keep the foreign
    name as the search term.

    For a non-Czech-origin title also collect the CZ alternative titles: dubbed
    Webshare files are named after the Czech dub ("Kačeří příběhy"), which is
    not the original title so the orig/display logic never finds it.
    """
    disp, orig = _titles(entry, kind)
    lang = _language(entry)
    need_english = disp and not orig and lang and lang != "en"
    need_czech = lang != "cs"  # Czech-origin: original/display already is the CZ name
    czech: list[str] = []
    if need_english or need_czech:
        alts = await _alt_titles(client, kind, entry.get("id"))
        if need_english:
            eng = _pick_english(alts)
            if eng and eng.casefold() != disp.casefold():
                orig, disp = disp, eng
        if need_czech:
            known = {disp.casefold(), orig.casefold()}
            czech = [t for t in _pick_czech(alts) if t.casefold() not in known]
    return disp, orig, lang, tuple(czech)


async def lookup(token: str, kind: str, query: str) -> tuple[str, str, str, tuple[str, ...]] | None:
    """(display, original, language, czech_titles) for a TMDB `tv`/`movie` matched by name.

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
            if not results:
                _cache[key] = None
                return None
            # Prefer a result with a foreign original title (what we want); fall
            # back to the most relevant match for its language.
            entry = next((e for e in results[:5] if _titles(e, kind)[1]), results[0])
            found = await _resolve(client, entry, kind)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB search %r (%s) failed: %s", query, kind, exc)
        return None
    _cache[key] = found
    logger.info("TMDB: %s %r -> display=%r original=%r lang=%r czech=%r",
                kind, query, *found)
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
                       tvdbid=None) -> tuple[str, str, str, tuple[str, ...]] | None:
    """(display, original, language, czech_titles) from an exact external id.

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
    disp = orig = lang = ""
    czech: tuple[str, ...] = ()
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
            if entry:
                disp, orig, lang, czech = await _resolve(client, entry, kind)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("TMDB id lookup %s (tmdb=%s tvdb=%s imdb=%s) failed: %s",
                       kind, tmdbid, tvdbid, imdb, exc)
        return None
    found = (disp, orig, lang, czech) if (disp or orig or lang) else None
    _cache[key] = found
    if found:
        logger.info("TMDB: %s id(tmdb=%s tvdb=%s imdb=%s) -> display=%r original=%r lang=%r czech=%r",
                    kind, tmdbid, tvdbid, imdb, disp, orig, lang, czech)
    return found
