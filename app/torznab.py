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
    """*arr language names of the tagged audio tracks, CZ/SK first, no duplicates."""
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


async def _probe(client, results: list[SearchResult], all_files: bool = False
                 ) -> tuple[dict[str, int], dict[str, str], dict[str, int], dict[str, dict]]:
    """Fetch file_info for results whose *name* leaves something open, and
    return (heights, audio, lengths): the video height for names without a
    resolution token — many CZ files ship without one and Sonarr/Radarr reject
    them as 'Unknown' quality otherwise — "Czech"/"Slovak" for names without a
    language marker whose audio track says so (a TV-rip dub named just
    "... 1080p WEB-DL prima+"), and the duration in seconds of every probed file.
    `all_files` probes every result (for the runtime check), not just those."""
    need = results if all_files else \
        [r for r in results if not _RES_RE.search(r.name) or not dub_language(r.name)]
    if not need:
        return {}, {}, {}, {}

    async def one(r: SearchResult):
        return r, await _file_info(client, r.ident)

    heights: dict[str, int] = {}
    audio: dict[str, str] = {}
    lengths: dict[str, int] = {}
    infos: dict[str, dict] = {}
    for r, info in await asyncio.gather(*(one(r) for r in need)):
        if info:
            infos[r.ident] = info
        if int(info.get("length") or 0) > 0:
            lengths[r.ident] = int(info["length"])
        height = resolution_class(int(info.get("width") or 0), int(info.get("height") or 0))
        if height and not _RES_RE.search(r.name):
            heights[r.ident] = height
        lang = audio_language(info.get("audio_languages"))
        if lang and not dub_language(r.name):
            audio[r.ident] = lang
    return heights, audio, lengths, infos


# How far a file's duration may stray from the TMDB runtime, as (min, max)
# fractions. Movies allow extended cuts; episodes allow double episodes.
_RUNTIME_BOUNDS = {"movie": (0.6, 1.6), "tv": (0.5, 2.6)}


def runtime_mismatch(length_s: int, minutes: int, kind: str) -> bool:
    """True when a file of `length_s` seconds can't be the title TMDB says runs
    `minutes` — a 7-minute Bluey episode vs an hour of "Blue Planet", a feature
    vs a short special, a full episode vs a 5-minute excerpt. Unknown on either
    side is never a mismatch."""
    if not length_s or not minutes or kind not in _RUNTIME_BOUNDS:
        return False
    lo, hi = _RUNTIME_BOUNDS[kind]
    ratio = length_s / 60 / minutes
    return ratio < lo or ratio > hi


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


# Releases that are never the title itself: cinema recordings, trailers and
# samples, 3D frame-packed versions. Matched on whole normalized tokens (not
# substrings), after the extension is stripped so a ".ts" container isn't "TS".
_JUNK_TOKENS = frozenset("""
cam camrip hdcam ts telesync hdts tc telecine hdtc scr screener dvdscr bdscr webscr
kinorip pdvd predvd predvdrip r5
trailer trailers teaser sample ukazka upoutavka upoutavky
3d sbs hsbs mvc
""".split())
_JUNK_PHRASES = (("kino", "rip"), ("hq", "clean", "audio"), ("half", "ou"))
_MOVIE_EP_RE = re.compile(r"(?<![a-z0-9])s\d{1,2}\s?e\d{1,3}(?!\d)|(?<!\d)\d{1,2}x\d{2}(?!\d)", re.I)


def junk_reason(name: str, movie: bool = False) -> str:
    """Why a file is never the wanted title ("" when it may be): a cinema
    recording (CAM/TS/TC/screener/kinorip), a trailer or sample, a 3D
    frame-packed version, or — in a movie search — an episode (a one-word title
    like "Avatar" otherwise pulls in a whole series)."""
    stem = name.rsplit(".", 1)[0] if _is_video(name) else name
    toks = normalize_text(stem).split()
    hit = next((t for t in toks if t in _JUNK_TOKENS), "")
    if hit:
        return hit
    for phrase in _JUNK_PHRASES:
        n = len(phrase)
        if any(tuple(toks[i:i + n]) == phrase for i in range(len(toks) - n + 1)):
            return " ".join(phrase)
    if movie and _MOVIE_EP_RE.search(stem):
        return "episode in a movie search"
    return ""


# --- measured quality -------------------------------------------------------
# Webshare's file_info is a media probe: resolution, video codec, duration,
# overall bitrate and every audio track (codec, channels, language) are real.
# It does NOT know HDR/DV, bit depth, subtitles or the source (WEB/BluRay), so
# those are never invented. What is measured goes into the release title as
# tokens *arr's parser and custom formats already understand.

# Below this a file is a stub, not a watchable encode (HARAKIRI.mp4: 10 MB / 135 min).
_TORSO_MB_PER_MIN = 3
# Bitrate floors (MB/min) under which an encode is visibly starved — x264 / HEVC
# (HEVC needs ~0.7x for the same picture). Scene 1080p x264 runs 40–100.
_LOW_BITRATE = {1080: (20, 14), 720: (12, 8)}
# A real UHD encode is >= ~30 MB/min; "2160p" below that is an upscale.
_UHD_FLOOR = 30

_HAS_CODEC_RE = re.compile(r"\b(x ?26[45]|h ?\.?26[45]|hevc|avc|xvid|divx|av1|vc-?1|mpeg-?[24])\b", re.I)
_HAS_AUDIO_RE = re.compile(
    r"(\bdd\+|\bddp|\be-?ac-?3|\bac-?3|\baac|\bdts|\btruehd|\batmos|\bflac|\bmp3|\bopus|\bl?pcm)", re.I)
_UHD_CLAIM_RE = re.compile(r"\b(2160p?|4k|uhd)\b", re.I)
_HEVC_FORMATS = {"HEVC", "H265", "H.265"}
_AVC_FORMATS = {"H264", "AVC", "H.264"}
_CHANNELS = {8: "7.1", 7: "6.1", 6: "5.1", 3: "2.1", 2: "2.0", 1: "1.0"}
# Audio codec preference, best first (DTS with 8 channels is DTS-HD MA / DTS:X —
# the lossy DTS core tops out at 6).
_AUDIO_RANK = {"TRUEHD": 6, "DTSHD": 5, "FLAC": 5, "EAC3": 4, "DTS": 3, "AC3": 2, "AAC": 1, "MP3": 0}


def _mb_per_min(size: int, length_s: int) -> float:
    return size / 1048576 / (length_s / 60) if size and length_s else 0.0


def is_torso(size: int, length_s: int) -> bool:
    """A few MB per minute: a stub/broken upload, not a watchable encode."""
    mbmin = _mb_per_min(size, length_s)
    return bool(mbmin) and mbmin < _TORSO_MB_PER_MIN


def audio_token(info: dict, prefer: tuple[str, ...] = ()) -> str:
    """Release-title token for the best audio track ("DDP5.1", "DTS-HD MA 7.1",
    "TrueHD 7.1", "AAC2.0"…), taken from the tracks in the preferred languages
    (e.g. the Czech dub) when there are any. "" when nothing is known."""
    tracks = info.get("audio") or []
    pool = [t for t in tracks if _ISO_LANGS.get(t.get("language", "")) in prefer] or tracks
    best, best_key = None, (-1, -1)
    for t in pool:
        fmt, ch = t.get("format", ""), int(t.get("channels") or 0)
        kind = "DTSHD" if fmt.startswith("DTS") and ch >= 8 else ("DTS" if fmt.startswith("DTS") else fmt)
        key = (_AUDIO_RANK.get(kind, -1), ch)
        if key > best_key:
            best, best_key = (kind, ch), key
    if not best or best_key[0] < 0:
        return ""
    kind, ch = best
    chs = _CHANNELS.get(ch, "")
    return {
        "TRUEHD": f"TrueHD {chs}", "DTSHD": f"DTS-HD MA {chs}", "DTS": f"DTS {chs}",
        "FLAC": f"FLAC {chs}", "EAC3": f"DDP{chs}", "AC3": f"DD{chs}", "AAC": f"AAC{chs}",
        "MP3": "MP3",
    }[kind].strip()


def quality_tokens(name: str, size: int, info: dict, *, czech: bool = False,
                   tags: bool = False) -> tuple[str, list[str]]:
    """(name with a corrected resolution, extra tokens) from the measured file.

    - a resolution claim ("4K"/"UHD"/"2160p"/"1080p"…) that measures lower is
      replaced by the real height (a 1080p inside a "4K" name, a 720p inside a
      "1080p" one); an understated name is left alone
    - codec (x264/x265, never a bare "HEVC"/"AVC": with "BluRay" those read as
      BR-DISK) and the best audio track, only when the name carries none — the
      uploader's own tags (Atmos, DTS-HD, HDR…) stay authoritative
    - "Upscaled" for 2160p below a real UHD bitrate (a TRaSH custom format
      already blocks it)

    With `tags` (the "Release tags" setting), also Websharr's own tokens, which
    only mean something to custom formats made for them (see the README):
    - "LowBitrate" for a starved 720p/1080p encode — to score, not a hard
      reject: an old CZ dub may have nothing better
    - "CZaudio"/"SKaudio" when a track is tagged Czech/Slovak (a verified dub,
      not just a name claim), "CZunverified" when the name claims a dub the
      tagged tracks don't show
    """
    if not info:
        return name, []
    tokens: list[str] = []
    measured = resolution_class(int(info.get("width") or 0), int(info.get("height") or 0))
    if measured:
        claims = [2160 for _ in _UHD_CLAIM_RE.findall(name)] + [int(r) for r in _RES_RE.findall(name)]
        if claims and max(claims) > measured:
            # the name overstates the picture ("4K"/"1080p" with 720p inside): say what it is
            name = _UHD_CLAIM_RE.sub(f"{measured}p", name)
            name = _RES_RE.sub(lambda m: f"{measured}p" if int(m.group(1)) > measured else m.group(0), name)
    fmt = (info.get("format") or "").upper()
    if not _HAS_CODEC_RE.search(name):
        if fmt in _HEVC_FORMATS:
            tokens.append("x265")
        elif fmt in _AVC_FORMATS:
            tokens.append("x264")
    if not _HAS_AUDIO_RE.search(name):
        tok = audio_token(info, ("Czech", "Slovak") if czech else ())
        if tok:
            tokens.append(tok)
    mbmin = _mb_per_min(size, int(info.get("length") or 0))
    klass = measured or next((int(m) for m in _RES_RE.findall(name)), 0)
    if mbmin and klass >= 2160 and mbmin < _UHD_FLOOR:
        tokens.append("Upscaled")
    elif tags and mbmin and klass in _LOW_BITRATE:
        floor = _LOW_BITRATE[klass][1 if fmt in _HEVC_FORMATS else 0]
        if mbmin < floor:
            tokens.append("LowBitrate")
    langs = track_languages(info.get("audio_languages")) if tags else []
    if "Czech" in langs:
        tokens.append("CZaudio")
    elif "Slovak" in langs:
        tokens.append("SKaudio")
    elif langs and dub_language(name):
        tokens.append("CZunverified")  # named as a dub, the tagged tracks say otherwise
    return name, tokens


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


# Words that may stand between a show title and a bare episode number.
_EP_WORDS = frozenset({"dil", "cast", "epizoda", "epizody", "ep", "e", "episode"})
# Words that may stand between a show title and its SxxEyy marker without making
# it a different show: season words, language/dub markers and technical tags.
_NEUTRAL_WORDS = frozenset("""
season seasons series serie serial seria sezona sezony rada rady rocnik dil cast epizoda episode ep
cz sk en eng cze czech slovak cesky slovensky dab dabing dabovano dub dubbed titulky tit sub subs
multi dual audio complete kompletni hd fhd uhd full web webdl webrip dl bluray bdrip brrip hdtv
tvrip dvdrip remux hevc avc aac ac3 eac3 dts dd ddp atmos truehd hdr dv mkv avi mp4 the and
""".split())


def file_marker(query, name: str) -> tuple[int | None, int | None]:
    """(season, episode) implied by the file name, read from the first marker
    after the (matched) show title: SxxEyy, 1x05, or a bare "05" (no season).

    Webshare fulltext is OR-based, so a "Skvrna 05" query returns every Skvrna
    episode — and a "DuckTales S01E02" query returns "DuckTales.S02E02..." too
    (matched on the show name alone). The caller must check both numbers: an
    episode-only match let season-2 files impersonate season 1, and the
    release-name rewrite then hid the real season from *arr entirely.

    A bare number only counts when it follows the title directly — at most
    behind a year or an episode word ("dil", "epizoda"). Scanning the whole name
    read the "1" of a "DDP5.1" audio tag as episode 1, so a "Blue" alias turned
    "Blue Planet II One Ocean ... DDP5.1" into "Bluey S01E01". SxxEyy/1x05 are
    unambiguous and still count anywhere.

    Any other word between the title and an SxxEyy marker means another show
    that merely starts with the same word: a TMDB Czech title "Blue" (Bluey)
    matched "Blue Planet II S01E01", "Blue Thunder S01E01", "Blue Lights
    S01E01", and the rewrite released them as Bluey. Season/language/technical
    words and the title's other names are allowed (`_NEUTRAL_WORDS`), and so is
    anything before a "special" marker (special episodes carry their own name).
    """
    ntoks = normalize_text(name).split()
    titles = _as_titles(query)
    for title in titles:
        series = _series_tokens(title)
        if series and ntoks[:len(series)] != series:
            continue  # this title isn't the one the file starts with
        other = {t for x in titles for t in _series_tokens(x)}
        is_special = False
        bare_ok = True  # still right behind the title (years/episode words only)
        foreign = False  # a word that can't belong to this show's episode name
        for tk in ntoks[len(series):]:
            if tk in ("special", "specials"):
                is_special = True
                bare_ok = True  # "... special 06": the number right after it is the special's
                continue
            m = re.match(r"^s(\d{1,2})e(\d{1,3})$", tk) or re.match(r"^(\d{1,2})x(\d{1,3})$", tk)
            if m:
                if foreign and not is_special:
                    return None, None  # "Blue Planet II S01E01" is not "Blue" S01E01
                return int(m.group(1)), int(m.group(2))
            if tk.isdigit() and len(tk) <= 2:
                if bare_ok:
                    return (0 if is_special else None), int(tk)
                continue
            if tk.isalpha() and len(tk) > 1 and tk not in _NEUTRAL_WORDS and tk not in other:
                foreign = True
            if tk in _EP_WORDS or (tk.isdigit() and len(tk) == 4 and 1900 <= int(tk) <= 2099):
                continue
            bare_ok = False  # any other word: later bare numbers are tech tokens
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


def _render_feed(request: Request, results: list[SearchResult], category: str,
                 *, query: str | None = None, season: str | None = None,
                 ep: str | None = None, episodes: dict[str, int] | None = None,
                 heights: dict[str, int] | None = None, audio: dict[str, str] | None = None,
                 language: str = "", czech_titles: list[str] | None = None,
                 infos: dict[str, dict] | None = None, ids: dict | None = None) -> Response:
    heights = heights or {}
    infos = infos or {}
    ids = {k: v for k, v in (ids or {}).items() if v}
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
        info = infos.get(r.ident, {})
        tagged = track_languages(info.get("audio_languages"))
        # A Czech-title match is only a hint: when the tracks are tagged and none
        # of them is CZ/SK, the file is not a dub (an English file named in Czech).
        by_title = "Czech" if czech_titles and matches_query(czech_titles, r.name) and \
            (not tagged or {"Czech", "Slovak"} & set(tagged)) else ""
        inferred = "" if dub_language(r.name) else audio.get(r.ident) or by_title
        marker, marker_re = ("SK", _SK_RE) if inferred == "Slovak" else ("CZ", _CZ_RE)
        if inferred and not marker_re.search(title):
            title = f"{title} {marker}"
        # The audio token describes the CZ/SK track whenever the file has one
        # (tagged or claimed): that's the track a CZ profile cares about.
        title, extra = quality_tokens(title, r.size, info, czech=bool(
            dub_language(r.name) or inferred or {"Czech", "Slovak"} & set(tagged)),
            tags=settings.release_tags)
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
        claimed = dub_language(r.name) or inferred
        if tagged:
            # every tagged track language; a name-claimed dub the tags don't show
            # stays in (tags are often missing or rewritten) — "CZunverified" marks it
            item_langs = ([claimed] if claimed and claimed not in tagged else []) + tagged
        else:
            item_langs = [claimed or language] if (claimed or language) else []
        item_lang = ", ".join(item_langs)
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
            # Echo the ids the search came with: *arr maps a Czech-only file name
            # to the right movie/series by id even when the title doesn't parse.
            for key, attr in (("tmdbid", "tmdbid"), ("imdbid", "imdb"), ("tvdbid", "tvdbid")):
                if ids.get(key):
                    ET.SubElement(item, "{%s}attr" % ns, {"name": attr, "value": str(ids[key])})

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
    for query in queries:
        try:
            results = await client.search(query, limit=limit, offset=offset)
        except (WebshareError, httpx.HTTPError) as exc:
            logger.error("Search '%s' failed: %s", query, exc)
            return _error(900, f"Webshare search failed: {exc}")
        for r in results:
            if r.ident in seen or r.password or not _is_video(r.name):
                continue
            if junk_reason(r.name, movie=(t == "movie")):
                continue  # CAM/trailer/3D, or an episode in a movie search
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
            merged.append(r)

    merged.sort(key=lambda r: (-relevance(queries, r.name), -r.size))
    logger.info("Newznab %s q=%r -> %d results", t, q, len(merged))
    # With an exact id, TMDB knows how long the title runs; files far off that
    # are another title that shares the name, a special or an excerpt.
    minutes = 0
    kind = "tv" if t == "tvsearch" else "movie"
    if settings.tmdb_token and t in ("tvsearch", "movie") and (
            params.get("tmdbid") or params.get("imdbid") or params.get("tvdbid")):
        minutes = await tmdb_runtime(settings.tmdb_token, kind, params.get("tmdbid"),
                                     params.get("imdbid"), params.get("tvdbid"),
                                     season if t == "tvsearch" else None,
                                     ep if t == "tvsearch" else None)
    # Check a few more than asked for, so dropped files don't leave *arr with
    # fewer results than the limit when more good ones exist.
    shown = merged[:limit * 2] if minutes else merged[:limit]
    # file_info is cheap and cached: probe every shown file, the measured
    # resolution/codec/audio feed the release title and language attrs.
    heights, audio, lengths, infos = await _probe(client, shown, all_files=True)
    shown = [r for r in shown if not is_torso(r.size, lengths.get(r.ident, 0))]
    if minutes:
        kept = []
        for r in shown:
            if runtime_mismatch(lengths.get(r.ident, 0), minutes, kind):
                logger.info("Dropped %r: %d min, expected ~%d min",
                            r.name, lengths[r.ident] // 60, minutes)
                continue
            kept.append(r)
        shown = kept[:limit]
    return _render_feed(request, shown, category, heights=heights, audio=audio,
                        query=display, season=(season if t == "tvsearch" else None), ep=ep,
                        episodes=episodes, language=language, czech_titles=czech_titles,
                        infos=infos, ids={k: params.get(k) for k in ("tmdbid", "imdbid", "tvdbid")})


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
