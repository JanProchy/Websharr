"""Newznab/Torznab indexer endpoint backed by Webshare search.

Mounted at /torznab/api. Supports t=caps, t=search, t=tvsearch, t=movie.
Only q-based queries are advertised (Webshare search is filename-based, so
imdbid/tvdbid lookups are not possible).

Add this to Sonarr/Radarr as a **Newznab** indexer, not Torznab: the paired
download client is SABnzbd (usenet protocol), and Sonarr only routes a grab
to a usenet client when the release came from a usenet (Newznab) indexer.
The feed emits attributes in both the newznab and torznab namespaces so it
parses correctly either way.
"""

import asyncio
import email.utils
import hashlib
import logging
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, Request, Response

from .config import config
from .nzb import build_nzb
from .settings import settings
from .tmdb import lookup as tmdb_lookup
from .tmdb import lookup_by_id as tmdb_lookup_by_id
from .tmdb import movie_rivals as tmdb_movie_rivals
from .tmdb import runtime as tmdb_runtime
from .webshare import SearchResult, WebshareError

logger = logging.getLogger("websharr.torznab")

router = APIRouter()

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"

CAT_MOVIES = "2000"
CAT_TV = "5000"

VIDEO_EXTENSIONS = (
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg",
)

# TMDB original_language (ISO 639-1) -> the language name Sonarr/Radarr expect in
# a newznab `language` attribute. Only the codes we can map are tagged; anything
# unknown is left untagged so *arr falls back to parsing the release name.
_LANG_NAMES = {
    "en": "English", "cs": "Czech", "sk": "Slovak", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pl": "Polish", "hu": "Hungarian", "nl": "Dutch",
    "pt": "Portuguese", "ru": "Russian", "uk": "Ukrainian", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "cn": "Chinese", "sv": "Swedish", "da": "Danish",
    "no": "Norwegian", "nb": "Norwegian", "fi": "Finnish", "tr": "Turkish", "ro": "Romanian",
    "el": "Greek", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi", "th": "Thai",
    "bg": "Bulgarian", "hr": "Croatian", "sr": "Serbian", "sl": "Slovenian", "ca": "Catalan",
    "fa": "Persian", "vi": "Vietnamese", "id": "Indonesian", "lt": "Lithuanian",
    "lv": "Latvian", "et": "Estonian", "is": "Icelandic",
}


def lang_name(code: str) -> str:
    """*arr language name for a TMDB language code (""->"", unknown code -> "")."""
    return _LANG_NAMES.get((code or "").strip().lower(), "")


# Filename markers of Czech/Slovak audio, as opposed to the original audio with
# subtitles ("titulky"). On Webshare a standalone CZ/SK marker is the normal
# way uploaders identify the audio language, even when "dabing" is omitted.
_DUB_RE = re.compile(r"\bdab(?:ing|ovan\w*|\b)", re.IGNORECASE)
_SK_RE = re.compile(r"\b(?:sk|slovensk\w*|slovak)\b", re.IGNORECASE)
_CZ_RE = re.compile(r"\b(?:cz|cesk\w*|česk\w*|czech)\b", re.IGNORECASE)
_SUBS_RE = re.compile(r"\b(?:titulky|tit|subs?|subtitles)\b", re.IGNORECASE)


def dub_language(name: str) -> str:
    """"Czech"/"Slovak" when the file name signals CZ/SK audio, else ""."""
    name = name or ""
    has_dub = bool(_DUB_RE.search(name))
    has_czech = bool(_CZ_RE.search(name))
    has_slovak = bool(_SK_RE.search(name))
    if not (has_dub or has_czech or has_slovak):
        return ""
    # A language marker followed by an explicit subtitle marker describes the
    # subtitles, not the audio. Explicit "dabing" still wins when both appear.
    if _SUBS_RE.search(name) and not has_dub:
        return ""
    # A dub marked SK (and not CZ) is Slovak; otherwise assume Czech (the default
    # on Webshare, where a bare "Dabing" is Czech).
    if has_slovak and not has_czech:
        return "Slovak"
    return "Czech"


# Audio-track language codes (Webshare file_info) that mean a CZ/SK release.
_AUDIO_CZECH = {"CZE", "CES", "CS", "CZ"}
_AUDIO_SLOVAK = {"SLO", "SLK", "SK"}


def audio_language(codes) -> str:
    """"Czech"/"Slovak" when the file carries such an audio track, else "".

    Only a positive signal: track tags are often missing or wrong, so their
    absence never overrides a marker in the file name.
    """
    codes = {(c or "").strip().upper() for c in codes or ()}
    if codes & _AUDIO_CZECH:
        return "Czech"
    if codes & _AUDIO_SLOVAK:
        return "Slovak"
    return ""


def _xml_response(element: ET.Element, status_code: int = 200) -> Response:
    body = ET.tostring(element, encoding="utf-8", xml_declaration=True)
    return Response(content=body, media_type="application/xml", status_code=status_code)


def _error(code: int, description: str) -> Response:
    el = ET.Element("error", {"code": str(code), "description": description})
    return _xml_response(el)


def _caps() -> Response:
    caps = ET.Element("caps")
    ET.SubElement(caps, "server", {"title": "Websharr", "version": "1.0"})
    ET.SubElement(caps, "limits", {"max": "100", "default": str(config.search_limit)})
    # Advertise id params so Sonarr/Radarr (via Prowlarr) send tvdbid/imdbid/
    # tmdbid — Websharr resolves them to the exact Czech title via TMDB instead
    # of relying on a fuzzy text match.
    searching = ET.SubElement(caps, "searching")
    ET.SubElement(searching, "search", {"available": "yes", "supportedParams": "q"})
    ET.SubElement(searching, "tv-search",
                  {"available": "yes", "supportedParams": "q,season,ep,tvdbid,imdbid,tmdbid"})
    ET.SubElement(searching, "movie-search",
                  {"available": "yes", "supportedParams": "q,imdbid,tmdbid"})
    cats = ET.SubElement(caps, "categories")
    movies = ET.SubElement(cats, "category", {"id": CAT_MOVIES, "name": "Movies"})
    ET.SubElement(movies, "subcat", {"id": "2040", "name": "Movies/HD"})
    tv = ET.SubElement(cats, "category", {"id": CAT_TV, "name": "TV"})
    ET.SubElement(tv, "subcat", {"id": "5040", "name": "TV/HD"})
    return _xml_response(caps)


_EP_IN_QUERY = re.compile(
    r"^(?P<series>.*?)\s*(?:s(?P<s>\d{1,2})e(?P<e>\d{1,3})|(?P<s2>\d{1,2})x(?P<e2>\d{1,3}))\b",
    re.I,
)


def parse_query(t: str, q: str, season: str | None, ep: str | None):
    """Pull an SxxEyy / 1x05 out of the query text when season/ep weren't
    passed separately — e.g. a user typing "skvrna s01e05" into the box.
    Returns (type, series, season, ep)."""
    q = (q or "").strip()
    if season is None and ep is None:
        m = _EP_IN_QUERY.match(q)
        if m:
            series = (m.group("series") or "").strip()
            if series:
                return "tvsearch", series, (m.group("s") or m.group("s2")), (m.group("e") or m.group("e2"))
    return t, q, season, ep


def build_queries(t: str, q: str, season: str | None, ep: str | None) -> list[str]:
    """Build Webshare search query variants for a Torznab request."""
    q = (q or "").strip()
    if not q:
        return []
    if t == "tvsearch" and season is not None:
        try:
            s = int(season)
        except ValueError:
            return [q]
        if ep is not None:
            try:
                e = int(ep)
            except ValueError:
                return [f"{q} S{s:02d}"]
            # Several naming conventions live on Webshare: S01E02, 1x02, and —
            # common for CZ uploads — a bare episode number ("Series 01 - Title").
            variants = [f"{q} S{s:02d}E{e:02d}", f"{q} {s}x{e:02d}"]
            if s == 1:
                # Only for season 1, where a bare "01" is unambiguous enough;
                # for later seasons it would collide with other episodes.
                variants.append(f"{q} {e:02d}")
            return variants
        return [f"{q} S{s:02d}"]
    return [q]


def _asciify(text: str) -> str:
    """Transliterate diacritics and drop remaining non-ASCII: "Bez vědomí" ->
    "Bez vedomi". Prowlarr puts the release title in an HTTP header when a
    download is proxied and rejects non-latin-1 chars ("Invalid non-ASCII ...
    in header"); *arr matches diacritic-insensitively, so this is safe."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c) and ord(c) < 128)
    return re.sub(r"\s{2,}", " ", text).strip()


def release_title(query: str, season: str | None, ep: str | None, name: str) -> str:
    """Sonarr/Radarr-parseable release name.

    Webshare filenames often lack SxxEyy (esp. CZ uploads like "Skvrna 01 -
    Pohřeb"), which breaks *arr's import parser. For a tvsearch we prepend the
    requested "<series> SxxEyy" so the folder/release name parses, then keep the
    original stem for quality tokens and human recognition.
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if season is None:
        return _asciify(stem)
    try:
        s = int(season)
    except (TypeError, ValueError):
        return _asciify(stem)
    q = (query or "").strip()
    if ep is not None:
        try:
            prefix = f"{q} S{s:02d}E{int(ep):02d}"
        except (TypeError, ValueError):
            prefix = f"{q} S{s:02d}"
    else:
        prefix = f"{q} S{s:02d}"
    # Strip any SxxEyy/1x02 already in the filename so the release doesn't carry
    # two episode markers (confuses *arr's parser: "unable to determine episode").
    stem = re.sub(r"\b(s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3})\b", "", stem, flags=re.I)
    stem = re.sub(r"\.{2,}", ".", stem)          # double dots left by the removal
    stem = re.sub(r"\s{2,}", " ", stem).strip(" .-")
    return _asciify(f"{prefix} - {stem}".strip(" -"))


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTENSIONS)


_RES_RE = re.compile(r"\b(480|540|576|720|1080|2160|4320)p?\b", re.I)


def resolution_class(width: int, height: int) -> int:
    """The standard resolution (360…2160) a video of this size belongs to.

    *arr only parses the labels it knows; a literal "384p" (old DVD-rip AVI) or
    "800p" (2.39:1 crop of a 1080p source) reads as Unknown quality and the
    release is rejected. Width decides for cropped widescreen, height for 4:3,
    whichever puts it higher.
    """
    by_width = 2160 if width >= 3200 else 1080 if width >= 1800 else 720 if width >= 1200 else 0
    by_height = (2160 if height >= 1700 else 1080 if height >= 900 else 720 if height >= 650
                 else 576 if height >= 560 else 540 if height >= 500
                 else 480 if height >= 380 else 360 if height > 0 else 0)
    return max(by_width, by_height)


# file_info is cheap (~30 ms) but Webshare answers HTTP 403 once more than ~6
# calls run at the same time — and *arr fires several searches in parallel, so a
# per-request limit wasn't enough: the 403s were swallowed and the files lost
# their resolution label, audio language and runtime check. One process-wide
# limit, a short retry on 403/429/5xx, and a cache (a Webshare file never
# changes under its ident) keep the probe reliable.
_PROBE_CONCURRENCY = 4
_PROBE_RETRIES = (0.5, 1.5, 3.0)
_PROBE_CACHE_MAX = 5000
_probe_sem: asyncio.Semaphore | None = None
_probe_cache: dict[str, dict] = {}


def _semaphore() -> asyncio.Semaphore:
    global _probe_sem
    if _probe_sem is None:
        _probe_sem = asyncio.Semaphore(_PROBE_CONCURRENCY)
    return _probe_sem


async def _file_info(client, ident: str) -> dict:
    """file_info with a process-wide concurrency limit, retry and cache; {} on failure."""
    if ident in _probe_cache:
        return _probe_cache[ident]
    for delay in (*_PROBE_RETRIES, None):
        try:
            async with _semaphore():
                info = await client.file_info(ident)
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in (403, 429) or exc.response.status_code >= 500
            if retryable and delay is not None:
                await asyncio.sleep(delay)
                continue
            logger.warning("file_info %s failed: HTTP %s", ident, exc.response.status_code)
            return {}
        except (WebshareError, httpx.HTTPError) as exc:
            logger.warning("file_info %s failed: %s", ident, exc)
            return {}
        if info:
            if len(_probe_cache) >= _PROBE_CACHE_MAX:
                _probe_cache.pop(next(iter(_probe_cache)))
            _probe_cache[ident] = info
        return info
    return {}


async def _probe(client, results: list[SearchResult], all_files: bool = False,
                 timeout: float | None = None
                 ) -> tuple[dict[str, int], dict[str, str], dict[str, int], set[str]]:
    """Fetch file_info for results whose *name* leaves something open, and
    return (heights, audio, lengths, unfinished): the video height for names
    without a resolution token — many CZ files ship without one and Sonarr/Radarr
    reject them as 'Unknown' quality otherwise — "Czech"/"Slovak" for names
    without a language marker whose audio track says so (a TV-rip dub named just
    "... 1080p WEB-DL prima+"), and the duration in seconds of every probed file.
    `all_files` probes every result (for the runtime check), not just those.
    With a `timeout`, probes still running after it are abandoned and their
    idents returned in `unfinished` (fail open: those files go out unmeasured)."""
    need = results if all_files else \
        [r for r in results if not _RES_RE.search(r.name) or not dub_language(r.name)]
    if not need:
        return {}, {}, {}, set()

    tasks = {asyncio.ensure_future(_file_info(client, r.ident)): r for r in need}
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()

    heights: dict[str, int] = {}
    audio: dict[str, str] = {}
    lengths: dict[str, int] = {}
    for task, r in tasks.items():
        if task in pending:
            continue
        info = task.result()
        if int(info.get("length") or 0) > 0:
            lengths[r.ident] = int(info["length"])
        height = resolution_class(int(info.get("width") or 0), int(info.get("height") or 0))
        if height and not _RES_RE.search(r.name):
            heights[r.ident] = height
        lang = audio_language(info.get("audio_languages"))
        if lang and not dub_language(r.name):
            audio[r.ident] = lang
    return heights, audio, lengths, {tasks[t].ident for t in pending}


# Runtime check (opt-in, settings.runtime_check). How far a file's duration may
# stray from the TMDB runtime, as (min, max) fractions: movies allow longer cuts,
# episodes double episodes. Only a mismatch is acted on — a file of the right
# length is never taken as proof that it is the right title.
_RUNTIME_BOUNDS = {"movie": (0.6, 2.0), "tv": (0.5, 2.6)}
# A show-level typical episode length (season search, or no runtime for the
# episode itself) is a weak reference: episodes of one show can run several
# times longer, so it only rejects files far *shorter* than it.
_RUNTIME_FALLBACK_MIN = 0.35
# Named editions run longer than the TMDB runtime by any amount; multi-episode
# files are several episodes long. Neither is rejected for being too long.
_EDITION_RE = re.compile(
    r"\b(?:extended|uncut|unrated|redux|directors? cut|director s cut|final cut"
    r"|(?:special|ultimate|collectors?) edition|prodlouzena verze|rezisersky sestrih)\b")
_MULTI_EP_RE = re.compile(r"\bs\d{1,2}e\d{1,3}(?:-?e\d{1,3}|-\d{1,3})+\b", re.I)


def runtime_mismatch(length_s: int, minutes: int, kind: str, *,
                     name: str = "", exact: bool = True) -> bool:
    """True when a file of `length_s` seconds can't be the title TMDB says runs
    `minutes` — a 7-minute Bluey episode vs an hour of "Blue Planet", a feature
    vs a short special, a full episode vs a 5-minute excerpt. `exact` is False
    for a show-level fallback runtime (see _RUNTIME_FALLBACK_MIN); `name` lifts
    the upper bound for a named edition or a multi-episode file. Unknown on
    either side is never a mismatch."""
    if not length_s or not minutes or kind not in _RUNTIME_BOUNDS:
        return False
    lo, hi = _RUNTIME_BOUNDS[kind]
    if not exact:
        lo, hi = _RUNTIME_FALLBACK_MIN, None
    if _EDITION_RE.search(normalize_text(name)) or (kind == "tv" and _MULTI_EP_RE.search(name)):
        hi = None
    ratio = length_s / 60 / minutes
    return ratio < lo or (hi is not None and ratio > hi)


# Time a search may spend on the runtime check (TMDB runtime, probes and refill
# pages) before it answers with what it has: Sonarr gives an indexer 100 s and
# then ignores it for minutes. Whatever isn't measured by then goes out
# unmeasured (fail open, as on a probe error).
_RUNTIME_BUDGET = 30.0
# Extra Webshare result pages fetched per query to replace files the runtime
# check dropped, at most.
_RUNTIME_EXTRA_PAGES = 2


async def _runtime_filtered(client, merged: list[SearchResult], limit: int, kind: str,
                            minutes: int, exact: bool, more, deadline: float
                            ) -> tuple[list[SearchResult], dict[str, int], dict[str, str]]:
    """The first `limit` of `merged` whose duration doesn't contradict the TMDB
    runtime, plus (heights, audio) of the probed ones.

    Files are measured in order, only as many as are needed to fill the limit;
    when dropped files leave it short, `more()` fetches the next Webshare page
    ([] once there are no more, or the page cap is reached). At `deadline` (loop
    time) the remaining candidates go out unmeasured instead of being dropped."""
    loop = asyncio.get_running_loop()
    merged = list(merged)
    rejected: set[str] = set()
    measured: set[str] = set()
    heights: dict[str, int] = {}
    audio: dict[str, str] = {}
    while True:
        candidates = [r for r in merged if r.ident not in rejected][:limit]
        todo = [r for r in candidates if r.ident not in measured]
        if not todo and len(candidates) >= limit:
            break
        left = deadline - loop.time()
        if left <= 0:
            logger.info("Runtime check budget of %ds spent: remaining files go out unmeasured",
                        _RUNTIME_BUDGET)
            break
        if not todo:
            try:
                new = await asyncio.wait_for(more(), timeout=left)
            except asyncio.TimeoutError:
                logger.info("Runtime check budget of %ds spent while refilling", _RUNTIME_BUDGET)
                break
            if not new:
                break
            merged.extend(new)
            continue
        h, a, lengths, unfinished = await _probe(client, todo, all_files=True, timeout=left)
        heights.update(h)
        audio.update(a)
        for r in todo:
            if r.ident in unfinished:
                continue
            measured.add(r.ident)
            if runtime_mismatch(lengths.get(r.ident, 0), minutes, kind, name=r.name, exact=exact):
                logger.info("Dropped %r: %d min, expected ~%d min",
                            r.name, lengths[r.ident] // 60, minutes)
                rejected.add(r.ident)
        if unfinished:
            logger.info("Runtime check budget of %ds spent: %d files go out unmeasured",
                        _RUNTIME_BUDGET, len(unfinished))
            break
    return [r for r in merged if r.ident not in rejected][:limit], heights, audio


_EP_TOKEN = re.compile(r"^(s\d{1,2}e\d{1,3}|s\d{1,2}|\d{1,2}x\d{1,3}|\d{1,4})$")


def normalize_text(text: str) -> str:
    """Lowercase, strip diacritics, split punctuation to spaces."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in text)


def _series_tokens(query: str) -> list[str]:
    """Query tokens that name the show/movie — numbers and SxxEyy/1x05
    episode markers dropped, so only the title words remain."""
    return [t for t in normalize_text(query).split() if not _EP_TOKEN.match(t)]


def _as_titles(query) -> list[str]:
    return [query] if isinstance(query, str) else list(query)


_ARTICLES = ("the", "a", "an")


def _title_key(text: str) -> str:
    """Normalized title with a leading article dropped — Sonarr searches
    'The Sleepers' as 'Sleepers', so aliases must match either form."""
    toks = normalize_text(text).split()
    if toks and toks[0] in _ARTICLES:
        toks = toks[1:]
    return " ".join(toks)


def alias_titles(query: str, aliases: list[dict]) -> list[str]:
    """Extra Webshare/CZ titles for a query, from the user's alias map — an
    alias applies when its `from` (a *arr title) appears in the query, ignoring
    a leading article on either side."""
    qk = _title_key(query)
    out = []
    for a in aliases or []:
        fk, to = _title_key(a.get("from", "")), (a.get("to") or "").strip()
        if fk and to and fk in qk:
            out.append(to)
    return out


def _tmdb_kind(t: str, cat: str | None) -> str:
    if t == "movie":
        return "movie"
    if t == "tvsearch":
        return "tv"
    return "movie" if (cat or "").strip().startswith("2") else "tv"  # 2xxx = movies


async def expand_titles(t: str, q: str, cat: str | None, *, tvdbid: str | None = None,
                        imdbid: str | None = None, tmdbid: str | None = None
                        ) -> tuple[list[str], str, str, list[str], int]:
    """Return (search_titles, display_title, original_language, czech_titles, year).

    search_titles: the query, manual-alias titles, the TMDB original title, and
    the TMDB CZ alternative titles (the Czech dub name of an English-origin
    show, e.g. "Kačeří příběhy" for DuckTales) — all searched, and any matching
    file is accepted.
    display_title: the canonical name used as the release-name prefix, so Sonarr
    shows "The Sleepers" instead of whatever alias/query happened to match.
    original_language: the title's language name (from TMDB) used to tag the
    release feed, or "" when unknown; lets *arr apply an original-language policy.
    czech_titles: the subset of search_titles that are Czech dub names — a file
    named after one is a Czech release even without a "dabing" marker, so the
    feed tags it Czech instead of the original language.
    year: the title's first-air/release year from TMDB (0 unknown), used to drop
    files of a same-named other title (see year_conflict).
    """
    # Aliases stay keyed on the *arr text query (interactive search); an ID-only
    # automatic search has no q, so we lean on the TMDB id lookup below instead.
    titles = ([q] if q and q.strip() else []) + alias_titles(q, settings.aliases)
    display = q
    language = ""
    czech_titles: list[str] = []
    year = 0
    if settings.tmdb_token:
        kind = _tmdb_kind(t, cat)
        res = None
        if tmdbid or imdbid or tvdbid:
            res = await tmdb_lookup_by_id(settings.tmdb_token, kind, tmdbid, imdbid, tvdbid)
        if not res:
            res = await tmdb_lookup(settings.tmdb_token, kind, q)
        if res:
            disp, orig, lang, czech, year = res
            seen = {normalize_text(x) for x in titles}
            if disp:
                display = disp  # prefix releases with the canonical title
                # Also search under it — for a CZ-origin show the canonical name
                # *is* the Czech one, and an ID-only search has no other term.
                if normalize_text(disp) not in seen:
                    titles.append(disp)
                    seen.add(normalize_text(disp))
            for extra in (orig, *czech):
                if extra and normalize_text(extra) not in seen:
                    titles.append(extra)
                    seen.add(normalize_text(extra))
            czech_titles = list(czech)
            language = lang_name(lang)
    return titles, display, language, czech_titles, year


def year_conflict(name: str, year: int) -> bool:
    """True when every year token in the file name contradicts the title's year.

    Two different titles can share a Czech name — the 1987 and 2017 DuckTales
    are both "Kačeří příběhy" on Webshare — and such files often disambiguate
    only by a year in the name ("Kaceri pribehy 2017 ..."). ±1 tolerated
    (releases are often stamped a year off); resolution tokens (1080, 2160)
    fall outside the 1900-2099 window.
    """
    if not year:
        return False
    years = [int(t) for t in normalize_text(name).split()
             if t.isdigit() and len(t) == 4 and 1900 <= int(t) <= 2099]
    return bool(years) and all(abs(y - year) > 1 for y in years)


def matches_query(query, name: str) -> bool:
    """True when the file name *starts with* the title words of the query (or of
    any of its alias titles, when a list is passed).

    Webshare's fulltext is loose — a "Skvrna S01E05" search also returns any
    file merely containing "S01E05"/"05" (WWE, football...), and for common-word
    titles unrelated files too. Requiring the name to *begin* with the title
    keeps the right ones; multiple titles let a CZ alias ("Bez vědomí") match a
    query whose *arr title is English ("The Sleepers").
    """
    ntoks = normalize_text(name).split()
    for title in _as_titles(query):
        tokens = _series_tokens(title)
        if not tokens:
            return True
        if ntoks[:len(tokens)] == tokens:
            return True
    return False


def file_marker(query, name: str) -> tuple[int | None, int | None]:
    """(season, episode) implied by the file name, read from the first marker
    after the (matched) show title: SxxEyy, 1x05, or a bare "05" (no season).

    Webshare fulltext is OR-based, so a "Skvrna 05" query returns every Skvrna
    episode — and a "DuckTales S01E02" query returns "DuckTales.S02E02..." too
    (matched on the show name alone). The caller must check both numbers: an
    episode-only match let season-2 files impersonate season 1, and the
    release-name rewrite then hid the real season from *arr entirely.
    """
    ntoks = normalize_text(name).split()
    for title in _as_titles(query):
        series = _series_tokens(title)
        if series and ntoks[:len(series)] != series:
            continue  # this title isn't the one the file starts with
        is_special = False
        for tk in ntoks[len(series):]:
            if tk in ("special", "specials"):
                is_special = True
                continue
            m = re.match(r"^s(\d{1,2})e(\d{1,3})$", tk) or re.match(r"^(\d{1,2})x(\d{1,3})$", tk)
            if m:
                return int(m.group(1)), int(m.group(2))
            if tk.isdigit() and len(tk) <= 2:  # bare episode number (skip years/1080)
                return (0 if is_special else None), int(tk)
        break
    return None, None


def file_episode(query, name: str) -> int | None:
    """Episode number implied by the file name (see file_marker)."""
    return file_marker(query, name)[1]


def relevance(queries: list[str], name: str) -> float:
    """Best fraction of a query's tokens present in the file name."""
    ntoks = set(normalize_text(name).split())
    best = 0.0
    for q in queries:
        qt = normalize_text(q).split()
        if qt:
            best = max(best, sum(1 for t in qt if t in ntoks) / len(qt))
    return best


# Window the synthetic publish dates fall into (see _pub_date).
_PUB_EPOCH = 1640995200  # 2022-01-01 UTC
_PUB_SPAN = 365 * 86400


def _pub_date(ident: str) -> str:
    """A fixed, per-file publish date (RFC 2822).

    Webshare search has no upload date, and *arr needs one. It must not move:
    Sonarr/Radarr recognise a blocklisted usenet release by title + *exact*
    publish date, so "now" meant a failed release was never seen as blocklisted
    and was grabbed again on every search. Derived from the ident so two files
    sharing a release title still differ; years old is harmless (retention is
    the only age check, and age merely breaks ties between equal releases).
    """
    offset = int(hashlib.md5(ident.encode()).hexdigest(), 16) % _PUB_SPAN
    return email.utils.formatdate(_PUB_EPOCH + offset)


# --- opt-in: the TMDB title in front of a movie name Radarr can't parse -----
# A Czech-only movie name ("Pribeh hracek 1080p CZ") doesn't parse to the movie
# in Radarr; mapped only by the echoed id, the grab is blocked from automatic
# import ("matched to movie by ID, Manual Import required"). That block is a
# safety net, so with settings.movie_title_prefix on "<TMDB title> <year> - "
# goes in front only when nothing leaves the film in doubt — the contract is
# movie_prefix_refusal. Off (the default), release names are left as they are.

# Language markers may sit between the title and the tags, but never hide what
# follows them: "Rocky CZ 2" still has a "2" after the title.
_PREFIX_LANG_TAGS = frozenset("""
cz sk cze ces czech cesky ceske ceska slovak slovensky slovenske slovenska svk slo
en eng english ger german fre french ita spa pol hun rus jpn
dabing dab dabovano dabovany dabovane titulky tit sub subs multi multisub dual
""".split())
# Technical tags (resolution, source, codec, audio); the first one ends the part
# of the name that says which film it is.
_PREFIX_TECH_TAGS = frozenset("""
480p 540p 576p 720p 1080p 1080i 2160p 4320p 480 540 576 720 1080 2160 4320 4k 8k uhd fhd hd sd
bluray bdrip brrip bdremux remux bd web webdl webrip webdlrip hdtv hdtvrip tvrip dvdrip dvd dvd5
dvd9 hdrip satrip dvbrip pdtv vhsrip amzn nf dsnp hmax atvp
x264 x265 h264 h265 hevc avc xvid divx av1 vp9 10bit 8bit hdr hdr10 dv dovi sdr
aac ac3 eac3 dts dtshd dd ddp dd5 ddp5 truehd atmos flac mp3 lpcm opus
""".split())
_PREFIX_TECH_PAIRS = frozenset({("h", "264"), ("h", "265"), ("x", "264"), ("x", "265"), ("blu", "ray"),
                                ("tv", "rip"), ("dolby", "vision"), ("dolby", "atmos")})
_ROMAN = {r: n for n, r in enumerate(
    "i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split(), 1)}
# Anywhere in a name: a part of the film, a split file, or not the film at all.
_PREFIX_PART_RE = re.compile(
    r"^(?:part|pt|parte|partie|teil|dil|cast|vol|volume|chapter|kapitola|cd|disc|disk)\d*$")
_PREFIX_JUNK = frozenset({"sample", "trailer", "teaser", "extras", "bonus", "featurette"})
_AUDIO_CHANNELS = ({"1", "2", "5", "6", "7"}, {"0", "1"})  # "5 1", "2 0", "dd5 1"
# A file's length must be this close to the TMDB runtime. Tighter than the
# runtime check's bounds: here it is a required signal (still never proof).
_PREFIX_RUNTIME = (0.85, 1.25)
# Time the prefix decision may take per search (TMDB lookups, probes); a file
# not confirmed by then gets no prefix (fail closed).
_PREFIX_BUDGET = 15.0


def _numeral(tok: str) -> int:
    """The value of an arabic (1-99) or Roman (I-XX) number token, else 0."""
    if tok.isdigit() and len(tok) <= 2:
        return int(tok)
    return _ROMAN.get(tok, 0)


def _is_year(tok: str) -> bool:
    return tok.isdigit() and len(tok) == 4 and 1900 <= int(tok) <= 2099


def _starts_with_title(toks: list[str], title: list[str]) -> bool:
    """`toks` begin with the `title` tokens; a title's closing number may be
    written either way ("Rocky II" = "Rocky 2")."""
    if not title or len(toks) < len(title) or toks[:len(title) - 1] != title[:-1]:
        return False
    head, last = toks[len(title) - 1], title[-1]
    return head == last or (len(title) > 1 and _numeral(last) > 0 and _numeral(head) == _numeral(last))


def _is_tag(toks: list[str], i: int) -> bool:
    """A technical tag starts at token `i` ("1080p", "H.264")."""
    return toks[i] in _PREFIX_TECH_TAGS or tuple(toks[i:i + 2]) in _PREFIX_TECH_PAIRS


def movie_name_identity(name: str, display: str, aliases, year: int) -> tuple[str, bool, str]:
    """(title, dated, refusal): whether a file name names exactly the searched
    movie, as far as the name alone can tell.

    The name (extension stripped, normalized) must begin with the TMDB title or
    one of its TMDB original/Czech titles (`title`: the one it begins with), and
    then hold nothing but the year, language markers and, from the first
    technical tag on, release details: no subtitle word ("Rocky Returns"), no
    number or Roman numeral ("Rocky 2", "Rocky V", "Rocky CZ 2"), no part, CD or
    sample marker anywhere. Every year in it must be the TMDB year — exactly:
    a year off is how a remake or a namesake differs. When the TMDB title ends
    in a number ("Rocky II"), only titles carrying that number count. `dated`
    says whether the name has the year; `refusal` is "" when it passes.
    """
    dtoks = normalize_text(display).split()
    if not dtoks or not year:
        return "", False, "no TMDB title or year"
    titles = [dtoks] + [normalize_text(a).split() for a in aliases or () if a]
    if len(dtoks) > 1 and _numeral(dtoks[-1]):
        titles = [t for t in titles if any(_numeral(x) == _numeral(dtoks[-1]) for x in t)]
    stem = name.rsplit(".", 1)[0] if _is_video(name) else name
    toks = normalize_text(stem).split()
    title = next((t for t in sorted(titles, key=len, reverse=True) if _starts_with_title(toks, t)), None)
    if title is None:
        return "", False, "doesn't start with the movie's title"
    matched = " ".join(title)
    rest = toks[len(title):]
    dated = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if _is_year(tok):
            if int(tok) != year:
                return matched, False, f"year {tok}, not {year}"
            dated = True
        elif tok in _PREFIX_LANG_TAGS:
            pass
        elif _is_tag(rest, i):
            break
        elif _numeral(tok):
            return matched, False, f"number {tok!r} after the title"
        else:
            return matched, False, f"{tok!r} after the title"
        i += 1
    tail = rest[i:]
    for j, tok in enumerate(tail):
        prev, nxt = (tail[j - 1] if j else ""), (tail[j + 1] if j + 1 < len(tail) else "")
        if _is_year(tok):
            if int(tok) != year:
                return matched, False, f"year {tok}, not {year}"
            dated = True
        elif _PREFIX_PART_RE.match(tok) or tok in _PREFIX_JUNK:
            return matched, False, f"{tok!r} in the name"
        elif tok.isdigit() and len(tok) <= 2:
            channels = (tok in _AUDIO_CHANNELS[0] and nxt in _AUDIO_CHANNELS[1]) or \
                (tok in _AUDIO_CHANNELS[1] and prev[-1:] in _AUDIO_CHANNELS[0])
            if not (channels or nxt == "bit"):
                return matched, False, f"number {tok!r} in the name"
        elif tok in _ROMAN and tok not in ("i", "v", "x"):  # those three are also tags/words
            return matched, False, f"numeral {tok!r} in the name"
    return matched, dated, ""


def movie_prefix_refusal(name: str, display: str, aliases, year: int, *,
                         length_s: int, minutes: int, rivals) -> str:
    """Why `name` must not get "<display> <year> - " in front; "" when it may.

    The matching-confidence contract — the prefix needs ALL of:
    1. the name names exactly the movie (movie_name_identity);
    2. the file's length is known and within _PREFIX_RUNTIME of the TMDB
       runtime. A required signal only: a matching length never proves the
       film ("Rocky Balboa" runs as long as "Rocky"), an unknown one refuses;
    3. TMDB said which other films the name could be (`rivals` from
       tmdb.movie_rivals; None = the lookup failed, refuse): another film of
       the same title, or a collection part whose title extends it ("Rocky" ->
       "Rocky II"), makes a name without the year ambiguous, and one of the
       same year makes even a dated name ambiguous. A name that also carries
       the words such a part adds ("Rocky 1976 1080p Balboa") is refused.
    """
    title, dated, why = movie_name_identity(name, display, aliases, year)
    if why:
        return why
    if not length_s or not minutes:
        return "length or TMDB runtime unknown"
    lo, hi = _PREFIX_RUNTIME
    if not lo <= length_s / 60 / minutes <= hi:
        return f"{length_s // 60} min, TMDB says {minutes} min"
    if rivals is None:
        return "TMDB couldn't say which other films share the name"
    ttoks = title.split()
    toks = set(normalize_text(name).split())
    for other, other_year, part in rivals:
        otoks = normalize_text(other).split()
        extra = otoks[len(ttoks):]
        if not _starts_with_title(otoks, ttoks) or (extra and not part):
            continue
        if extra and set(extra) <= toks:
            return f"the name also has the words of {other!r}"
        if other_year == year:
            return f"{other!r} ({other_year}) has the same name and year"
        if not dated:
            return f"without the year it could be {other!r} ({other_year or '?'})"
    return ""


def _canonical(name: str, display: str, year: int) -> bool:
    """The name already begins with "<display> <year>": nothing to add."""
    toks, dtoks = normalize_text(name).split(), normalize_text(display).split()
    return toks[:len(dtoks) + 1] == dtoks + [str(year)]


async def _movie_prefixes(client, shown: list[SearchResult], tmdbid, imdbid) -> dict[str, str]:
    """ident -> "<TMDB title> <year> -" for the shown files that get it under
    movie_prefix_refusal's contract. TMDB answers come from the caches of the
    same search where possible; a file not confirmed within _PREFIX_BUDGET
    gets nothing."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PREFIX_BUDGET

    def left() -> float:
        return max(0.0, deadline - loop.time())

    token = settings.tmdb_token
    try:
        found = await asyncio.wait_for(tmdb_lookup_by_id(token, "movie", tmdbid, imdbid), timeout=left())
        if not found or not found[0] or not found[4]:
            return {}
        display, orig, _lang, czech, year = found
        aliases = (orig, *czech)
        named: dict[str, tuple[SearchResult, str]] = {}
        for r in shown:
            title, _dated, why = movie_name_identity(r.name, display, aliases, year)
            if why:
                logger.debug("No title prefix for %r: %s", r.name, why)
            elif not _canonical(r.name, display, year):
                named[r.ident] = (r, title)
        if not named:
            return {}
        minutes, exact = await asyncio.wait_for(
            tmdb_runtime(token, "movie", tmdbid, imdbid), timeout=left())
        # the TMDB spelling of each title a file begins with, to look up namesakes
        spelled = {" ".join(normalize_text(t).split()): t for t in (*aliases, display) if t}
        rivals = await asyncio.wait_for(tmdb_movie_rivals(
            token, sorted({spelled.get(t, t) for _r, t in named.values()}), tmdbid, imdbid), timeout=left())
    except asyncio.TimeoutError:
        logger.info("Title prefix: TMDB didn't answer within %ds, no prefix", _PREFIX_BUDGET)
        return {}
    _h, _a, lengths, _u = await _probe(client, [r for r, _t in named.values()], all_files=True,
                                       timeout=left())
    prefix = _asciify(f"{display} {year} -")
    out: dict[str, str] = {}
    for ident, (r, _title) in named.items():
        why = movie_prefix_refusal(r.name, display, aliases, year, length_s=lengths.get(ident, 0),
                                   minutes=minutes if exact else 0, rivals=rivals)
        if why:
            logger.info("No title prefix for %r: %s", r.name, why)
            continue
        logger.info("Title prefix %r for %r", prefix, r.name)
        out[ident] = prefix
    return out


def _render_feed(request: Request, results: list[SearchResult], category: str,
                 *, query: str | None = None, season: str | None = None,
                 ep: str | None = None, episodes: dict[str, int] | None = None,
                 heights: dict[str, int] | None = None, audio: dict[str, str] | None = None,
                 language: str = "", czech_titles: list[str] | None = None,
                 prefixes: dict[str, str] | None = None) -> Response:
    heights = heights or {}
    prefixes = prefixes or {}
    audio = audio or {}
    episodes = episodes or {}
    ET.register_namespace("torznab", TORZNAB_NS)
    ET.register_namespace("newznab", NEWZNAB_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Websharr"
    ET.SubElement(channel, "description").text = "Webshare.cz Newznab bridge"

    base = str(request.base_url).rstrip("/")

    for r in results:
        item = ET.SubElement(channel, "item")
        title = release_title(query, season, episodes.get(r.ident, ep), r.name) \
            if query is not None else \
            (r.name.rsplit(".", 1)[0] if "." in r.name else r.name)
        # Opt-in canonical movie title ("Toy Story 1995 - Pribeh hracek ..."),
        # see _movie_prefixes; never set with the setting off.
        if prefixes.get(r.ident):
            title = f"{prefixes[r.ident]} {title}"
        # Label quality from the real video height when the name lacks one,
        # so *arr doesn't reject the release as "Unknown" quality.
        if not _RES_RE.search(title) and heights.get(r.ident):
            title = f"{title} {heights[r.ident]}p"
        # A dub recognised without a marker in its name — by its audio track, or
        # by being named after the Czech title ("Cerveny trpaslik ...") — gets the
        # marker added, so title-based custom formats ("CZ" in release title)
        # see it too.
        inferred = "" if dub_language(r.name) else audio.get(r.ident) or \
            ("Czech" if czech_titles and matches_query(czech_titles, r.name) else "")
        marker, marker_re = ("SK", _SK_RE) if inferred == "Slovak" else ("CZ", _CZ_RE)
        if inferred and not marker_re.search(title):
            title = f"{title} {marker}"
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f"websharr-{r.ident}"
        # The saved file keeps the raw filename; the folder/title (nzbname) carries
        # the parseable SxxEyy so *arr import works. Pass the title as nzbname so a
        # direct GET of this link (or SAB addurl) names the job correctly.
        link = (
            f"{base}/torznab/nzb/{r.ident}"
            f"?apikey={config.api_key}"
            f"&name={urllib.parse.quote(_asciify(r.name))}&size={r.size}"
            f"&nzbname={urllib.parse.quote(title)}"
        )
        ET.SubElement(item, "link").text = link
        ET.SubElement(item, "pubDate").text = _pub_date(r.ident)
        ET.SubElement(item, "size").text = str(r.size)
        ET.SubElement(item, "enclosure", {
            "url": link,
            "length": str(r.size),
            "type": "application/x-nzb",
        })
        # A CZ/SK dub overrides the title's original language (a dubbed release
        # is in the dub language, not the original), so *arr's original-language
        # policy grabs the original audio and skips the dub. A file named after
        # the Czech dub title ("Kačeří příběhy ...") is a Czech release even
        # when it carries no "dabing" marker.
        item_lang = dub_language(r.name) or inferred or language
        # Emit attrs in both namespaces so the feed parses whether Sonarr/Radarr
        # treats it as Newznab (usenet — the correct choice) or Torznab.
        for ns in (NEWZNAB_NS, TORZNAB_NS):
            ET.SubElement(item, "{%s}attr" % ns, {"name": "category", "value": category})
            ET.SubElement(item, "{%s}attr" % ns, {"name": "size", "value": str(r.size)})
            ET.SubElement(item, "{%s}attr" % ns,
                          {"name": "grabs", "value": str(r.positive_votes)})
            if item_lang:
                ET.SubElement(item, "{%s}attr" % ns,
                              {"name": "language", "value": item_lang})

    return _xml_response(rss)


@router.get("/torznab/api")
async def torznab_api(request: Request):
    params = request.query_params
    if params.get("apikey") != config.api_key:
        return _error(100, "Invalid API key")

    t = params.get("t", "caps")
    if t == "caps":
        return _caps()
    if t not in ("search", "tvsearch", "movie"):
        return _error(203, f"Function '{t}' not available")

    t, q, season, ep = parse_query(t, params.get("q", ""), params.get("season"), params.get("ep"))
    # A *arr query may match a Webshare/CZ title (alias map or TMDB lookup);
    # search all and accept files matching any of them. `display` is the nice
    # title used to prefix the release name.
    titles, display, language, czech_titles, year = await expand_titles(
        t, q, params.get("cat"), tvdbid=params.get("tvdbid"),
        imdbid=params.get("imdbid"), tmdbid=params.get("tmdbid"))
    queries = []
    for title in titles:
        for v in build_queries(t, title, season, ep):
            if v not in queries:
                queries.append(v)
    category = CAT_TV if t == "tvsearch" else CAT_MOVIES

    if not queries and (params.get("q") or any(params.get(k) for k in ("tvdbid", "imdbid", "tmdbid"))):
        # A real search that yields nothing to look for (an id TMDB can't
        # resolve, a query that normalizes to nothing): no results. The
        # placeholder below would show up as an unparseable row in *arr's
        # interactive search for that title.
        return _render_feed(request, [], category)
    if not queries:
        # Webshare has no RSS/"latest" feed, but Sonarr/Radarr reject an indexer
        # whose capability-test query returns zero items ("no results in the
        # configured categories"). Return one deliberately unparseable placeholder
        # in the requested category so the test passes; the decision engine has no
        # title to parse during RSS sync, so it is never grabbed.
        cat = (params.get("cat", "") or "").split(",")[0].strip() or category
        placeholder = SearchResult(
            ident="websharr-online",
            name="Websharr online - no automatic feed, use interactive search",
            size=1,
        )
        return _render_feed(request, [placeholder], cat)

    limit = min(int(params.get("limit", str(config.search_limit)) or config.search_limit), 100)
    offset = int(params.get("offset", "0") or 0)

    want_ep = int(ep) if (t == "tvsearch" and ep and str(ep).isdigit()) else None
    want_season = int(season) if (t == "tvsearch" and season and str(season).isdigit()) else None
    client = request.app.state.webshare
    seen: set[str] = set()
    merged: list[SearchResult] = []
    episodes: dict[str, int] = {}  # season search: each file's own episode number

    def accept(results: list[SearchResult]) -> list[SearchResult]:
        """The results that are files of the requested title (and episode)."""
        kept = []
        for r in results:
            if r.ident in seen or r.password or not _is_video(r.name):
                continue
            if not matches_query(titles, r.name):
                continue  # drop Webshare's loose non-matching fulltext hits
            if year_conflict(r.name, year):
                continue  # same-named other title (DuckTales 1987 vs 2017)
            if want_ep is not None or want_season is not None:
                fs, fe = file_marker(titles, r.name)
                if want_ep is not None and fe != want_ep:
                    continue  # OR fulltext returns every episode; keep the asked one
                if want_season is not None and fs is not None and fs != want_season:
                    continue  # an S02E02 file is not the requested S01E02
                if want_ep is None:
                    # Season search (Sonarr's automatic search when several
                    # episodes of a season are missing). Webshare has no season
                    # packs, so release each file under its own episode; a bare
                    # "05" only counts in season 1 (see build_queries).
                    if fe is None or (fs is None and want_season != 1):
                        continue
                    episodes[r.ident] = fe
            seen.add(r.ident)
            kept.append(r)
        return kept

    full: list[str] = []  # queries whose page came back full: more may follow
    for query in queries:
        try:
            results = await client.search(query, limit=limit, offset=offset)
        except (WebshareError, httpx.HTTPError) as exc:
            logger.error("Search '%s' failed: %s", query, exc)
            return _error(900, f"Webshare search failed: {exc}")
        if len(results) >= limit:
            full.append(query)
        merged.extend(accept(results))

    def rank(results: list[SearchResult]) -> None:
        results.sort(key=lambda r: (-relevance(queries, r.name), -r.size))

    rank(merged)
    logger.info("Newznab %s q=%r -> %d results", t, q, len(merged))
    # Opt-in: with an exact id, TMDB knows how long the title runs; files far
    # off that are another title that shares the name, a special or an excerpt.
    # Off, nothing below asks TMDB or probes more than the labels need.
    check = settings.runtime_check and settings.tmdb_token and t in ("tvsearch", "movie") \
        and want_season != 0 and any(params.get(k) for k in ("tmdbid", "imdbid", "tvdbid"))
    if not check:
        shown = merged[:limit]
        heights, audio, _lengths, _unfinished = await _probe(client, shown)
    else:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RUNTIME_BUDGET
        kind = "tv" if t == "tvsearch" else "movie"
        try:
            minutes, exact = await asyncio.wait_for(tmdb_runtime(
                settings.tmdb_token, kind, params.get("tmdbid"), params.get("imdbid"),
                params.get("tvdbid"), season if t == "tvsearch" else None,
                ep if t == "tvsearch" else None), timeout=_RUNTIME_BUDGET)
        except asyncio.TimeoutError:
            logger.info("TMDB runtime lookup outlived the budget: no runtime check")
            minutes, exact = 0, False
        pages = 0

        async def more() -> list[SearchResult]:
            """The next result page of every query that still had a full one."""
            nonlocal full, pages
            new: list[SearchResult] = []
            while not new and full and pages < _RUNTIME_EXTRA_PAGES:
                pages += 1
                still_full = []
                for query in full:
                    try:
                        results = await client.search(query, limit=limit, offset=offset + pages * limit)
                    except (WebshareError, httpx.HTTPError) as exc:
                        logger.warning("Refill search '%s' failed: %s", query, exc)
                        continue
                    if len(results) >= limit:
                        still_full.append(query)
                    new.extend(accept(results))
                full = still_full
            rank(new)
            return new

        if minutes:
            shown, heights, audio = await _runtime_filtered(
                client, merged, limit, kind, minutes, exact, more, deadline)
        else:
            shown = merged[:limit]  # runtime unknown: label probes only, as with the check off
            heights, audio, _lengths, _unfinished = await _probe(client, shown)
    # Opt-in: the TMDB title and year in front of movie names that leave no
    # doubt which film they are, so Radarr can import them by itself. Off,
    # nothing here runs and release names are unchanged.
    prefixes: dict[str, str] = {}
    if settings.movie_title_prefix and settings.tmdb_token and t == "movie" and shown \
            and (params.get("tmdbid") or params.get("imdbid")):
        prefixes = await _movie_prefixes(client, shown, params.get("tmdbid"), params.get("imdbid"))
    return _render_feed(request, shown, category, heights=heights, audio=audio,
                        query=display, season=(season if t == "tvsearch" else None), ep=ep,
                        episodes=episodes, language=language, czech_titles=czech_titles,
                        prefixes=prefixes)


@router.get("/torznab/nzb/{ident}")
async def torznab_nzb(ident: str, request: Request):
    if request.query_params.get("apikey") != config.api_key:
        return _error(100, "Invalid API key")

    # Filename/size are carried in the link generated by the search feed, so
    # the NZB is self-contained; ident alone is still enough to download.
    name = request.query_params.get("name") or ident
    try:
        size = int(request.query_params.get("size", "0"))
    except ValueError:
        size = 0

    content = build_nzb(ident, name, size)
    # Name the NZB after the parseable release title (nzbname) when present:
    # Sonarr re-uploads it to the SABnzbd client using this filename as the job
    # name, so the download folder carries SxxEyy for import.
    stem = request.query_params.get("nzbname") or \
        (name.rsplit(".", 1)[0] if "." in name else name)
    # Fully transliterate to ASCII — no RFC 5987 filename*: Prowlarr decodes that
    # back to the diacritics and re-emits them raw in its own header, which its
    # HTTP layer rejects ("Invalid non-ASCII in header 0x011B" = ě).
    ascii_name = re.sub(r'[^\x20-\x7e]', "_", _asciify(stem)).replace('"', "_") or "download"
    disposition = f'attachment; filename="{ascii_name}.nzb"'
    return Response(
        content=content,
        media_type="application/x-nzb",
        headers={"Content-Disposition": disposition},
    )
