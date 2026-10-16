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


# Audio-track language codes (Webshare file_info, ISO 639-2/1) -> *arr language names.
_ISO_LANGS = {
    "CZE": "Czech", "CES": "Czech", "CS": "Czech", "CZ": "Czech",
    "SLO": "Slovak", "SLK": "Slovak", "SK": "Slovak",
    "ENG": "English", "EN": "English", "GER": "German", "DEU": "German", "FRE": "French",
    "FRA": "French", "SPA": "Spanish", "ITA": "Italian", "POL": "Polish", "HUN": "Hungarian",
    "RUS": "Russian", "UKR": "Ukrainian", "JPN": "Japanese", "KOR": "Korean", "CHI": "Chinese",
    "ZHO": "Chinese", "DAN": "Danish", "SWE": "Swedish", "NOR": "Norwegian", "FIN": "Finnish",
    "DUT": "Dutch", "NLD": "Dutch", "POR": "Portuguese", "TUR": "Turkish",
}


def track_languages(codes) -> list[str]:
    """*arr language names of the tagged audio tracks, CZ/SK first, no duplicates.

    What the track metadata says, not a verified spoken language: tags are
    sometimes missing or wrong (a dub muxed under the original's tag)."""
    out: list[str] = []
    for code in codes or ():
        name = _ISO_LANGS.get((code or "").strip().upper())
        if name and name not in out:
            out.append(name)
    return sorted(out, key=lambda n: n not in ("Czech", "Slovak"))


def audio_language(codes) -> str:
    """"Czech"/"Slovak" when the file carries such an audio track, else "".

    Only a positive signal: track tags are often missing or wrong, so their
    absence never overrides a marker in the file name.
    """
    langs = track_languages(codes)
    return "Czech" if "Czech" in langs else "Slovak" if "Slovak" in langs else ""


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
# changes under its ident) keep the probe reliable. Parallel searches often
# share files, so concurrent misses for one ident share one request.
_PROBE_CONCURRENCY = 4
_PROBE_RETRIES = (0.5, 1.5, 3.0)
_PROBE_CACHE_MAX = 5000
_probe_sem: asyncio.Semaphore | None = None
_probe_cache: dict[str, dict] = {}
_probe_inflight: dict[str, asyncio.Future] = {}


def _semaphore() -> asyncio.Semaphore:
    global _probe_sem
    if _probe_sem is None:
        _probe_sem = asyncio.Semaphore(_PROBE_CONCURRENCY)
    return _probe_sem


async def _file_info(client, ident: str) -> dict:
    """file_info with a process-wide concurrency limit, retry and cache; {} on
    failure. Concurrent calls for one ident wait for a single request.

    A caller that stops waiting (its search budget spent) doesn't cancel that
    request: it finishes for the others waiting on it and fills the cache."""
    if ident in _probe_cache:
        return _probe_cache[ident]
    task = _probe_inflight.get(ident)
    if task is None:
        task = asyncio.ensure_future(_fetch_file_info(client, ident))
        _probe_inflight[ident] = task
        task.add_done_callback(lambda done: _probe_finished(ident, done))
    return await asyncio.shield(task)


def _probe_finished(ident: str, task: asyncio.Future) -> None:
    if _probe_inflight.get(ident) is task:
        del _probe_inflight[ident]
    if not task.cancelled() and task.exception() is not None:
        logger.warning("file_info %s failed: %r", ident, task.exception())


async def _fetch_file_info(client, ident: str) -> dict:
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
                 ) -> tuple[dict[str, int], dict[str, str], dict[str, dict], set[str]]:
    """Fetch file_info for results whose *name* leaves something open, and
    return (heights, audio, infos, unfinished): the video height for names
    without a resolution token — many CZ files ship without one and Sonarr/Radarr
    reject them as 'Unknown' quality otherwise — "Czech"/"Slovak" for names
    without a language marker whose audio track says so (a TV-rip dub named just
    "... 1080p WEB-DL prima+"), and the file_info of every probed file.
    `all_files` probes every result (for the runtime check and the measured
    metadata), not just those.
    With a `timeout`, probes still running after it are no longer waited for
    and their idents returned in `unfinished` (fail open: those files go out
    unmeasured; the probes finish in the background and fill the cache)."""
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
    infos: dict[str, dict] = {}
    for task, r in tasks.items():
        if task in pending:
            continue
        # a probe that raised is unmeasured, like one that returned nothing
        info = {} if task.cancelled() or task.exception() is not None else task.result() or {}
        if info:
            infos[r.ident] = info
        height = resolution_class(int(info.get("width") or 0), int(info.get("height") or 0))
        if height and not _RES_RE.search(r.name):
            heights[r.ident] = height
        lang = audio_language(info.get("audio_languages"))
        if lang and not dub_language(r.name):
            audio[r.ident] = lang
    return heights, audio, infos, {tasks[t].ident for t in pending}


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


# Time a search may spend measuring files — the runtime check (TMDB runtime,
# probes and refill pages) and the measured-metadata opt-ins share it — before
# it answers with what it has: Sonarr gives an indexer 100 s and then ignores it
# for minutes. Whatever isn't measured by then goes out unmeasured (fail open,
# as on a probe error).
_RUNTIME_BUDGET = 30.0
# Extra Webshare result pages fetched per query to replace files the runtime
# check or the stub filter dropped, at most.
_RUNTIME_EXTRA_PAGES = 2


async def _measured(client, merged: list[SearchResult], limit: int, reject, more,
                    deadline: float
                    ) -> tuple[list[SearchResult], dict[str, int], dict[str, str], dict[str, dict]]:
    """The first `limit` of `merged` that `reject(result, info)` doesn't drop (it
    returns the reason, "" to keep), with every one of them measured, plus
    (heights, audio, infos) of the probed ones.

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
    infos: dict[str, dict] = {}
    while True:
        candidates = [r for r in merged if r.ident not in rejected][:limit]
        todo = [r for r in candidates if r.ident not in measured]
        if not todo and (len(candidates) >= limit or not rejected):
            break  # full, or short without having dropped anything: nothing to replace
        left = deadline - loop.time()
        if left <= 0:
            logger.info("Measuring budget of %ds spent: remaining files go out unmeasured",
                        _RUNTIME_BUDGET)
            break
        if not todo:
            try:
                new = await asyncio.wait_for(more(), timeout=left)
            except asyncio.TimeoutError:
                logger.info("Measuring budget of %ds spent while refilling", _RUNTIME_BUDGET)
                break
            if not new:
                break
            merged.extend(new)
            continue
        h, a, i, unfinished = await _probe(client, todo, all_files=True, timeout=left)
        heights.update(h)
        audio.update(a)
        infos.update(i)
        for r in todo:
            if r.ident in unfinished:
                continue
            measured.add(r.ident)
            reason = reject(r, i.get(r.ident, {}))
            if reason:
                logger.info("Dropped %r: %s", r.name, reason)
                rejected.add(r.ident)
        if unfinished:
            logger.info("Measuring budget of %ds spent: %d files go out unmeasured",
                        _RUNTIME_BUDGET, len(unfinished))
            break
    return [r for r in merged if r.ident not in rejected][:limit], heights, audio, infos


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


# --- measured metadata (opt-in, settings.measured_metadata) -------------------
# Webshare's file_info is a media probe: video codec and size, and per audio
# track the codec, a channel count and a language tag, as reported. That is
# all it knows: no channel layout (6 channels need not be 5.1), no codec
# profile (DTS-HD MA and Atmos look like DTS / TrueHD), no HDR/DV, bit depth
# or source. So only the reported codecs are added to a release title, and
# only when the name names none: the uploader's own tags always win.

# Codec tags already in a name: any of them means the name keeps its own.
_VIDEO_TAG_RE = re.compile(
    r"(?<![a-z0-9])(?:[xh][ ._-]?26[45]|hevc|avc|xvid|divx|av1|vp9|vc-?1|mpeg-?[24])(?![a-z0-9])", re.I)
_AUDIO_TAG_RE = re.compile(
    r"(?<![a-z0-9])(?:dd(?:p|\+)?(?:[ ._-]?[1-7][ ._]?[01])?|dolby(?:[ ._-]?digital)?|e-?ac-?3|ac-?3"
    r"|dts(?:-?(?:hd|x|es|ma))*|truehd|atmos|aac(?:[ ._-]?[1-7][ ._]?[01])?|flac|mp[23]|opus|vorbis"
    r"|l?pcm)(?![a-z0-9])", re.I)
# Reported video codec -> title token. "H264"/"H265", never a bare "AVC"/"HEVC":
# next to "BluRay" those read as a BR-DISK to Radarr and TRaSH's custom format.
_VIDEO_TOKENS = {"H264": "H264", "AVC": "H264", "AVC1": "H264", "HEVC": "H265", "H265": "H265",
                 "AV1": "AV1", "VP9": "VP9"}
# Reported audio codec -> (title token, rank when picking one of several tracks).
_AUDIO_TOKENS = {"TRUEHD": ("TrueHD", 6), "FLAC": ("FLAC", 5), "PCM": ("PCM", 5), "LPCM": ("PCM", 5),
                 "EAC3": ("EAC3", 4), "DTS": ("DTS", 3), "AC3": ("AC3", 2), "OPUS": ("Opus", 1),
                 "AAC": ("AAC", 1), "MP3": ("MP3", 0)}


def _codec_key(fmt) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(fmt or "").upper())


def video_codec(info: dict) -> str:
    """Title token for the reported video codec, "" when unknown."""
    return _VIDEO_TOKENS.get(_codec_key(info.get("format")), "")


def audio_codec(info: dict, prefer: str = "") -> str:
    """Title token for the reported codec of the best audio track, taken from
    the tracks tagged `prefer` (e.g. the Czech dub) when there are any; "" when
    nothing is known. The codec only: a channel count is not a layout, and a
    DTS track is "DTS" whatever profile it may really have."""
    tracks = info.get("audio") or []
    pool = [t for t in tracks if prefer and _ISO_LANGS.get(t.get("language") or "") == prefer] or tracks
    best, best_key = "", (-1, -1)
    for t in pool:
        key = _codec_key(t.get("format"))
        token, rank = _AUDIO_TOKENS.get("DTS" if key.startswith("DTS") else key, ("", -1))
        if token and (rank, int(t.get("channels") or 0)) > best_key:
            best, best_key = token, (rank, int(t.get("channels") or 0))
    return best


def measured_tags(name: str, info: dict, prefer: str = "") -> list[str]:
    """The reported video and audio codec tokens `name` doesn't carry yet: none
    for a kind the name already tags in any form (DD5.1, DDP, AC3, DTS-HD,
    TrueHD, Atmos, AAC2.0, x264, HEVC…), so nothing is doubled or contradicted."""
    tokens = []
    if not _VIDEO_TAG_RE.search(name) and video_codec(info):
        tokens.append(video_codec(info))
    if not _AUDIO_TAG_RE.search(name) and audio_codec(info, prefer):
        tokens.append(audio_codec(info, prefer))
    return tokens


# --- quality heuristics (opt-in, settings.quality_heuristics) -----------------
# Unlike the measured metadata above, these are inferences: they rewrite a
# resolution the uploader claimed, or call a file an upscale. Each can be wrong
# for some file, so they stay behind their own switch, off by default.

# A resolution the name claims: a height ("1080p", "1080") or "4K"/"UHD" (2160).
_RES_CLAIM_RE = re.compile(r"(?<![a-z0-9])(?:(480|540|576|720|1080|2160|4320)p?|4k|uhd)(?![a-z0-9])", re.I)
_UPSCALED_RE = re.compile(r"(?<![a-z0-9])upscaled(?![a-z0-9])", re.I)
# Below this bitrate (MiB per minute) an H264/H265 "2160p" is taken for an
# upscale; a real UHD encode of those codecs runs well above it. AV1/VP9 and
# unknown codecs are never judged by bitrate (no floor fits them).
_UHD_FLOOR = 30
_UHD_FLOOR_CODECS = ("H264", "H265")
# Uploaders mark AI upscales in the name ("1080p.AI.WEB", "AI.Upscale.2160p");
# TRaSH's Upscaled custom format misses the bare "AI" form. A lone "AI" only
# counts next to a resolution/source/codec tag, so "Ai Weiwei" or "A.I." stay
# untouched. "Regrade" is colour grading, not an upscale, and doesn't count.
_UPSCALE_WORDS = frozenset({"upscale", "upscaled", "upscaling", "aius"})
_AI_NEIGHBOURS = frozenset({"web", "webrip", "webdl", "dl", "bluray", "bdrip", "brrip", "hdtv", "uhd", "fhd",
                            "4k", "enhanced", "x264", "x265", "h264", "h265", "hevc", "avc"})
_RES_TOKEN_RE = re.compile(r"^\d{3,4}p$")


def upscale_claim(name: str) -> bool:
    """True when the name itself says the picture is an (AI) upscale."""
    toks = [x for x in re.split(r"[^a-z0-9]+", name.lower()) if x]
    for i, tok in enumerate(toks):
        if tok in _UPSCALE_WORDS:
            return True
        if tok == "ai" and any(n in _AI_NEIGHBOURS or _RES_TOKEN_RE.match(n)
                               for n in toks[max(0, i - 1):i + 2]):
            return True
    return False


def _correct_resolution(name: str, measured: int) -> str:
    """`name` without the resolution claims above the `measured` class; when no
    truthful claim is left, the first overstated one becomes the measured class,
    so the name keeps exactly one resolution token."""
    claims = [(m, int(m.group(1)) if m.group(1) else 2160) for m in _RES_CLAIM_RE.finditer(name)]
    over = [m for m, height in claims if height > measured]
    if not over:
        return name
    say_measured = len(over) == len(claims)
    out, pos = [], 0
    for m in over:
        start = m.start()
        if say_measured:
            out.append(name[pos:start] + f"{measured}p")
            say_measured = False
        else:
            # drop the token and the separator before it ("4K.UHD" -> "4K")
            out.append(name[pos:start - 1 if start and name[start - 1] in " ._" else start])
        pos = m.end()
    return "".join(out) + name[pos:]


def _mib_per_min(size: int, length_s: int) -> float:
    return size / 1048576 / (length_s / 60) if size and length_s else 0.0


# Stub rejection (opt-in, settings.reject_stubs): below this many MiB per minute
# a file is a stub or a broken upload, not a watchable encode (10 MB over 135
# minutes). Unknown size or length is never a stub.
_STUB_MIB_PER_MIN = 3


def is_stub(size: int, length_s: int) -> bool:
    """A few MiB per minute: a stub/broken upload, not a watchable encode."""
    return 0 < _mib_per_min(size, length_s) < _STUB_MIB_PER_MIN


def quality_heuristics(name: str, size: int, info: dict) -> tuple[str, list[str]]:
    """(name with an overstated resolution corrected, extra tokens).

    - a resolution claim ("4K"/"UHD"/"2160p"/"1080p"…) above the measured class
      is replaced by it (a 1080p inside a "4K" name, a 720p inside a "1080p"
      one); 4:3 and cropped widescreen keep their class, an understated name
      is left alone
    - "Upscaled" when the name says it is an AI upscale (also unprobed), or
      for a measured 2160p H264/H265 file below a real UHD bitrate
    """
    tokens: list[str] = []
    measured = resolution_class(int(info.get("width") or 0), int(info.get("height") or 0))
    if measured:
        name = _correct_resolution(name, measured)
    if not _UPSCALED_RE.search(name):
        mibmin = _mib_per_min(size, int(info.get("length") or 0))
        if upscale_claim(name) or (measured >= 2160 and video_codec(info) in _UHD_FLOOR_CODECS
                                   and 0 < mibmin < _UHD_FLOOR):
            tokens.append("Upscaled")
    return name, tokens


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


def _render_feed(request: Request, results: list[SearchResult], category: str,
                 *, query: str | None = None, season: str | None = None,
                 ep: str | None = None, episodes: dict[str, int] | None = None,
                 heights: dict[str, int] | None = None, audio: dict[str, str] | None = None,
                 language: str = "", czech_titles: list[str] | None = None,
                 infos: dict[str, dict] | None = None, metadata: bool = False,
                 heuristics: bool = False) -> Response:
    """`metadata` (the measured-metadata opt-in) adds what `infos` reports: the
    codecs to the title, every tagged audio language to the language attrs.
    `heuristics` (the quality-heuristics opt-in) corrects an overstated
    resolution and marks upscales (see quality_heuristics)."""
    heights = heights or {}
    infos = infos or {}
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
        info = infos.get(r.ident, {})
        claimed = dub_language(r.name) or inferred
        upscaled: list[str] = []
        if heuristics:
            title, upscaled = quality_heuristics(title, r.size, info)
        # The audio codec of the CZ/SK track when the release is a dub: that's
        # the track a CZ profile cares about.
        extra = (measured_tags(title, info, claimed) if metadata and info else []) + upscaled
        if extra:
            title = f"{title} {' '.join(extra)}"
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
        item_langs = [claimed or language] if claimed or language else []
        # Measured metadata: the languages the audio tracks are tagged with, the
        # name's dub claim first (never dropped: tags are sometimes missing or
        # rewritten). Reported track metadata, not a verified dub.
        tagged = track_languages(info.get("audio_languages")) if metadata else []
        if tagged:
            item_langs = ([claimed] if claimed else []) + [x for x in tagged if x != claimed]
        # Emit attrs in both namespaces so the feed parses whether Sonarr/Radarr
        # treats it as Newznab (usenet — the correct choice) or Torznab; one
        # language attr per language, as *arr reads them.
        for ns in (NEWZNAB_NS, TORZNAB_NS):
            ET.SubElement(item, "{%s}attr" % ns, {"name": "category", "value": category})
            ET.SubElement(item, "{%s}attr" % ns, {"name": "size", "value": str(r.size)})
            ET.SubElement(item, "{%s}attr" % ns,
                          {"name": "grabs", "value": str(r.positive_votes)})
            for lang in item_langs:
                ET.SubElement(item, "{%s}attr" % ns,
                              {"name": "language", "value": lang})

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
    check = settings.runtime_check and settings.tmdb_token and t in ("tvsearch", "movie") \
        and want_season != 0 and any(params.get(k) for k in ("tmdbid", "imdbid", "tvdbid"))
    # Opt-ins that measure every shown file, not only names that lack a
    # resolution or a language marker. They share the runtime check's budget.
    measure_all = settings.measured_metadata or settings.quality_heuristics or settings.reject_stubs
    # With all of these off, nothing below asks TMDB or probes more than the
    # labels need.
    if not check and not measure_all:
        shown = merged[:limit]
        heights, audio, infos, _unfinished = await _probe(client, shown)
    else:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RUNTIME_BUDGET
        kind = "tv" if t == "tvsearch" else "movie"
        minutes, exact = 0, False
        if check:
            try:
                minutes, exact = await asyncio.wait_for(tmdb_runtime(
                    settings.tmdb_token, kind, params.get("tmdbid"), params.get("imdbid"),
                    params.get("tvdbid"), season if t == "tvsearch" else None,
                    ep if t == "tvsearch" else None), timeout=_RUNTIME_BUDGET)
            except asyncio.TimeoutError:
                logger.info("TMDB runtime lookup outlived the budget: no runtime check")
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

        def reject(r: SearchResult, info: dict) -> str:
            """Why a measured file is dropped, "" to keep it."""
            length = int(info.get("length") or 0)
            if runtime_mismatch(length, minutes, kind, name=r.name, exact=exact):
                return f"{length // 60} min, expected ~{minutes} min"
            if settings.reject_stubs and is_stub(r.size, length):
                return f"a stub, {_mib_per_min(r.size, length):.1f} MiB/min"
            return ""

        if minutes or measure_all:
            shown, heights, audio, infos = await _measured(
                client, merged, limit, reject, more, deadline)
        else:
            shown = merged[:limit]  # runtime unknown: label probes only, as with the check off
            heights, audio, infos, _unfinished = await _probe(client, shown)
    return _render_feed(request, shown, category, heights=heights, audio=audio,
                        query=display, season=(season if t == "tvsearch" else None), ep=ep,
                        episodes=episodes, language=language, czech_titles=czech_titles,
                        infos=infos, metadata=settings.measured_metadata,
                        heuristics=settings.quality_heuristics)


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
