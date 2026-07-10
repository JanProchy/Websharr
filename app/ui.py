"""Web UI for manual testing and monitoring.

Serves a single-page interface at GET /ui (no build step, one static HTML
file) and thin JSON endpoints under /ui/api/* that the page polls. The
Torznab/SABnzbd endpoints are untouched — the UI's Grab button talks to
/sabnzbd/api exactly like Sonarr would.

The API key is rendered into the page server-side (placeholder replacement),
so it never needs to be hard-coded in JS.
"""

import logging
import urllib.parse
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .config import config
from .downloads import DownloadManager, Job
from .torznab import VIDEO_EXTENSIONS, build_queries
from .webshare import SearchResult, WebshareError

logger = logging.getLogger("websharr.ui")

router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"
ASSETS_DIR = Path(__file__).parent.parent / "assets"
ALLOWED_ASSETS = {"logo.svg", "logo-wordmark.svg", "favicon.svg"}


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "API Key Incorrect"}, status_code=403)


def _check_key(request: Request) -> bool:
    return request.query_params.get("apikey") == config.api_key


def _job_json(job: Job) -> dict:
    data = asdict(job)
    data["job_name"] = job.job_name
    return data


@router.get("/ui", response_class=HTMLResponse)
async def ui_index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__WEBSHARR_API_KEY__", config.api_key)
    html = html.replace("__WEBSHARR_VERSION__", __version__)
    return HTMLResponse(html)


@router.get("/ui/assets/{name}")
async def ui_asset(name: str):
    if name not in ALLOWED_ASSETS:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(ASSETS_DIR / name, media_type="image/svg+xml")


@router.get("/ui/api/status")
async def ui_status(request: Request):
    if not _check_key(request):
        return _unauthorized()
    manager: DownloadManager = request.app.state.downloads
    return {
        "app": "websharr",
        "version": __version__,
        "webshare_user": config.webshare_username or None,
        "queue": len(manager.queue_jobs()),
        "history": len(manager.history_jobs()),
    }


@router.get("/ui/api/queue")
async def ui_queue(request: Request):
    if not _check_key(request):
        return _unauthorized()
    manager: DownloadManager = request.app.state.downloads
    return {"jobs": [_job_json(j) for j in manager.queue_jobs()]}


@router.get("/ui/api/history")
async def ui_history(request: Request):
    if not _check_key(request):
        return _unauthorized()
    manager: DownloadManager = request.app.state.downloads
    return {"jobs": [_job_json(j) for j in manager.history_jobs()]}


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTENSIONS)


def _result_json(request: Request, r: SearchResult) -> dict:
    base = str(request.base_url).rstrip("/")
    nzb_url = (
        f"{base}/torznab/nzb/{r.ident}"
        f"?apikey={config.api_key}"
        f"&name={urllib.parse.quote(r.name)}&size={r.size}"
    )
    title = r.name.rsplit(".", 1)[0] if "." in r.name else r.name
    return {
        "ident": r.ident,
        "name": r.name,
        "title": title,
        "size": r.size,
        "positive_votes": r.positive_votes,
        "nzb_url": nzb_url,
    }


@router.get("/ui/api/search")
async def ui_search(request: Request):
    """JSON search — same query building as /torznab/api, without the XML."""
    if not _check_key(request):
        return _unauthorized()

    params = request.query_params
    t = params.get("t", "search")
    if t not in ("search", "tvsearch", "movie"):
        return JSONResponse({"error": f"Unknown search type '{t}'"}, status_code=400)

    q = params.get("q", "")
    queries = build_queries(t, q, params.get("season"), params.get("ep"))
    if not queries:
        return {"results": [], "queries": []}

    limit = min(int(params.get("limit", str(config.search_limit)) or config.search_limit), 100)

    client = request.app.state.webshare
    seen: set[str] = set()
    merged: list[SearchResult] = []
    for query in queries:
        try:
            results = await client.search(query, limit=limit)
        except (WebshareError, httpx.HTTPError) as exc:
            logger.error("UI search '%s' failed: %s", query, exc)
            return JSONResponse({"error": f"Webshare search failed: {exc}"}, status_code=502)
        for r in results:
            if r.ident in seen or r.password or not _is_video(r.name):
                continue
            seen.add(r.ident)
            merged.append(r)

    merged.sort(key=lambda r: r.size, reverse=True)
    return {
        "results": [_result_json(request, r) for r in merged[:limit]],
        "queries": queries,
    }
