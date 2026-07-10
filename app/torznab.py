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
import logging
import re
import time
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
                  {"available": "yes", "supportedParams": "q,season,ep,tvdbid,imdbid"})
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


async def _resolutions(client, results: list[SearchResult]) -> dict[str, int]:
    """Fetch video height (via file_info) for results whose name has no
    resolution token, so we can label quality — many CZ files ship without
    one and Sonarr/Radarr reject them as 'Unknown' quality otherwise."""
    need = [r for r in results if not _RES_RE.search(r.name)]
    if not need:
        return {}
    sem = asyncio.Semaphore(6)

    async def one(r: SearchResult):
        async with sem:
            try:
                info = await client.file_info(r.ident)
                return r.ident, int(info.get("height") or 0)
            except (WebshareError, httpx.HTTPError):
                return r.ident, 0

    pairs = await asyncio.gather(*(one(r) for r in need))
    return {ident: h for ident, h in pairs if h}


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
                        ) -> tuple[list[str], str]:
    """Return (search_titles, display_title).

    search_titles: the query, manual-alias titles, and the TMDB original title —
    all searched, and any matching file is accepted.
    display_title: the canonical name used as the release-name prefix, so Sonarr
    shows "The Sleepers" instead of whatever alias/query happened to match.
    """
    titles = [q] + alias_titles(q, settings.aliases)
    display = q
    if settings.tmdb_token:
        kind = _tmdb_kind(t, cat)
        res = None
        if tmdbid or imdbid or tvdbid:
            res = await tmdb_lookup_by_id(settings.tmdb_token, kind, tmdbid, imdbid, tvdbid)
        if not res:
            res = await tmdb_lookup(settings.tmdb_token, kind, q)
        if res:
            disp, orig = res
            if disp:
                display = disp  # prefix releases with the canonical title
            if orig and normalize_text(orig) not in {normalize_text(x) for x in titles}:
                titles.append(orig)
    return titles, display


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


def file_episode(query, name: str) -> int | None:
    """Episode number implied by the file name, read from the first marker
    after the (matched) show title: SxxEyy, 1x05, or a bare "05".

    Webshare fulltext is OR-based, so a "Skvrna 05" query returns every Skvrna
    episode; this keeps only the episode actually asked for.
    """
    ntoks = normalize_text(name).split()
    for title in _as_titles(query):
        series = _series_tokens(title)
        if series and ntoks[:len(series)] != series:
            continue  # this title isn't the one the file starts with
        for tk in ntoks[len(series):]:
            m = re.match(r"^s\d{1,2}e(\d{1,3})$", tk) or re.match(r"^\d{1,2}x(\d{1,3})$", tk)
            if m:
                return int(m.group(1))
            if tk.isdigit() and len(tk) <= 2:  # bare episode number (skip years/1080)
                return int(tk)
        break
    return None


def relevance(queries: list[str], name: str) -> float:
    """Best fraction of a query's tokens present in the file name."""
    ntoks = set(normalize_text(name).split())
    best = 0.0
    for q in queries:
        qt = normalize_text(q).split()
        if qt:
            best = max(best, sum(1 for t in qt if t in ntoks) / len(qt))
    return best


def _render_feed(request: Request, results: list[SearchResult], category: str,
                 *, query: str | None = None, season: str | None = None,
                 ep: str | None = None, heights: dict[str, int] | None = None) -> Response:
    heights = heights or {}
    ET.register_namespace("torznab", TORZNAB_NS)
    ET.register_namespace("newznab", NEWZNAB_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Websharr"
    ET.SubElement(channel, "description").text = "Webshare.cz Newznab bridge"

    base = str(request.base_url).rstrip("/")
    now_rfc2822 = email.utils.formatdate(time.time())

    for r in results:
        item = ET.SubElement(channel, "item")
        title = release_title(query, season, ep, r.name) if query is not None else \
            (r.name.rsplit(".", 1)[0] if "." in r.name else r.name)
        # Label quality from the real video height when the name lacks one,
        # so *arr doesn't reject the release as "Unknown" quality.
        if not _RES_RE.search(title) and heights.get(r.ident):
            title = f"{title} {heights[r.ident]}p"
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f"websharr-{r.ident}"
        # The saved file keeps the raw filename; the folder/title (nzbname) carries
        # the parseable SxxEyy so *arr import works. Pass the title as nzbname so a
        # direct GET of this link (or SAB addurl) names the job correctly.
        link = (
            f"{base}/torznab/nzb/{r.ident}"
            f"?apikey={config.api_key}"
            f"&name={urllib.parse.quote(r.name)}&size={r.size}"
            f"&nzbname={urllib.parse.quote(title)}"
        )
        ET.SubElement(item, "link").text = link
        # Webshare has no upload-date in search results; use "now" so *arr
        # treats results as fresh rather than rejecting them by age.
        ET.SubElement(item, "pubDate").text = now_rfc2822
        ET.SubElement(item, "size").text = str(r.size)
        ET.SubElement(item, "enclosure", {
            "url": link,
            "length": str(r.size),
            "type": "application/x-nzb",
        })
        # Emit attrs in both namespaces so the feed parses whether Sonarr/Radarr
        # treats it as Newznab (usenet — the correct choice) or Torznab.
        for ns in (NEWZNAB_NS, TORZNAB_NS):
            ET.SubElement(item, "{%s}attr" % ns, {"name": "category", "value": category})
            ET.SubElement(item, "{%s}attr" % ns, {"name": "size", "value": str(r.size)})
            ET.SubElement(item, "{%s}attr" % ns,
                          {"name": "grabs", "value": str(r.positive_votes)})

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
    titles, display = await expand_titles(
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
    client = request.app.state.webshare
    seen: set[str] = set()
    merged: list[SearchResult] = []
    for query in queries:
        try:
            results = await client.search(query, limit=limit, offset=offset)
        except (WebshareError, httpx.HTTPError) as exc:
            logger.error("Search '%s' failed: %s", query, exc)
            return _error(900, f"Webshare search failed: {exc}")
        for r in results:
            if r.ident in seen or r.password or not _is_video(r.name):
                continue
            if not matches_query(titles, r.name):
                continue  # drop Webshare's loose non-matching fulltext hits
            if want_ep is not None and file_episode(titles, r.name) != want_ep:
                continue  # OR-based fulltext returns every episode; keep the asked one
            seen.add(r.ident)
            merged.append(r)

    merged.sort(key=lambda r: (-relevance(queries, r.name), -r.size))
    logger.info("Newznab %s q=%r -> %d results", t, q, len(merged))
    shown = merged[:limit]
    heights = await _resolutions(client, shown)
    return _render_feed(request, shown, category, heights=heights,
                        query=display, season=(season if t == "tvsearch" else None), ep=ep)


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
    filename = f"{stem}.nzb"
    # HTTP headers are latin-1; CZ names (Řád, …) aren't. Use RFC 5987 filename*
    # plus an ASCII fallback so the response doesn't 500 on non-latin-1 chars.
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", filename).replace('"', "_")
    encoded = urllib.parse.quote(filename)
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
    return Response(
        content=content,
        media_type="application/x-nzb",
        headers={"Content-Disposition": disposition},
    )
