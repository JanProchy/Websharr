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
    searching = ET.SubElement(caps, "searching")
    ET.SubElement(searching, "search", {"available": "yes", "supportedParams": "q"})
    ET.SubElement(searching, "tv-search", {"available": "yes", "supportedParams": "q,season,ep"})
    ET.SubElement(searching, "movie-search", {"available": "yes", "supportedParams": "q"})
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


def release_title(query: str, season: str | None, ep: str | None, name: str) -> str:
    """Sonarr/Radarr-parseable release name.

    Webshare filenames often lack SxxEyy (esp. CZ uploads like "Skvrna 01 -
    Pohřeb"), which breaks *arr's import parser. For a tvsearch we prepend the
    requested "<series> SxxEyy" so the folder/release name parses, then keep the
    original stem for quality tokens and human recognition.
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if season is None:
        return stem
    try:
        s = int(season)
    except (TypeError, ValueError):
        return stem
    q = (query or "").strip()
    if ep is not None:
        try:
            prefix = f"{q} S{s:02d}E{int(ep):02d}"
        except (TypeError, ValueError):
            prefix = f"{q} S{s:02d}"
    else:
        prefix = f"{q} S{s:02d}"
    return f"{prefix} - {stem}".strip()


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTENSIONS)


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


def matches_query(query: str, name: str) -> bool:
    """True when the file name *starts with* the query's title words.

    Webshare's fulltext is loose — a "Skvrna S01E05" search also returns any
    file merely containing "S01E05"/"05" (WWE, football, Our Planet...), and
    when the show name is a common word ("skvrna" = stain) also unrelated
    titles that happen to include it ("Lidská skvrna", "...A slepá skvrna").
    Requiring the name to *begin* with the title tokens keeps "Skvrna 05 -
    Bestie" while dropping those.
    """
    tokens = _series_tokens(query)
    if not tokens:
        return True
    ntoks = normalize_text(name).split()
    return ntoks[:len(tokens)] == tokens


def file_episode(query: str, name: str) -> int | None:
    """Episode number implied by the file name, read from the first marker
    after the show title: SxxEyy, 1x05, or a bare "05". None if none found.

    Webshare fulltext is OR-based, so a "Skvrna 05" query returns every Skvrna
    episode; this lets the search keep only the episode actually asked for
    instead of mislabeling "Skvrna 01 - Pohřeb" as S01E05.
    """
    series = _series_tokens(query)
    toks = normalize_text(name).split()
    for tk in toks[len(series):]:
        m = re.match(r"^s\d{1,2}e(\d{1,3})$", tk) or re.match(r"^\d{1,2}x(\d{1,3})$", tk)
        if m:
            return int(m.group(1))
        if tk.isdigit() and len(tk) <= 2:  # bare episode number (skip years/1080)
            return int(tk)
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
                 ep: str | None = None) -> Response:
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
    queries = build_queries(t, q, season, ep)
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
            if not matches_query(q, r.name):
                continue  # drop Webshare's loose non-matching fulltext hits
            if want_ep is not None and file_episode(q, r.name) != want_ep:
                continue  # OR-based fulltext returns every episode; keep the asked one
            seen.add(r.ident)
            merged.append(r)

    merged.sort(key=lambda r: (-relevance(queries, r.name), -r.size))
    logger.info("Newznab %s q=%r -> %d results", t, q, len(merged))
    return _render_feed(request, merged[:limit], category,
                        query=q, season=(season if t == "tvsearch" else None), ep=ep)


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
    filename = (name.rsplit(".", 1)[0] if "." in name else name) + ".nzb"
    return Response(
        content=content,
        media_type="application/x-nzb",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
