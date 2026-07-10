"""Web UI for manual testing and monitoring.

Serves a single-page interface at GET /ui (no build step, one static HTML
file) and thin JSON endpoints under /ui/api/* that the page polls. The
Torznab/SABnzbd endpoints are untouched — the UI's Grab button talks to
/sabnzbd/api exactly like Sonarr would.

The API key is rendered into the page server-side (placeholder replacement),
so it never needs to be hard-coded in JS.
"""

import asyncio
import logging
import secrets
import unicodedata
import urllib.parse
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .config import config
from .downloads import DownloadManager, Job
from .settings import SESSION_TTL, hash_password, settings, verify_password
from .torznab import VIDEO_EXTENSIONS, build_queries
from .webshare import SearchResult, WebshareError

logger = logging.getLogger("websharr.ui")

router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"
ASSETS_DIR = Path(__file__).parent.parent / "assets"
ALLOWED_ASSETS = {"logo.svg", "logo-wordmark.svg", "favicon.svg"}
SESSION_COOKIE = "websharr_session"


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "API Key Incorrect"}, status_code=403)


def _check_key(request: Request) -> bool:
    return request.query_params.get("apikey") == config.api_key


def _session_ok(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    return settings.configured and bool(token) and settings.verify_session(token)


def _authorized(request: Request) -> bool:
    """Sonarr-style: the API key always works, a logged-in session too."""
    return _check_key(request) or _session_ok(request)


def _set_session(resp: JSONResponse) -> None:
    resp.set_cookie(SESSION_COOKIE, settings.issue_session(),
                    max_age=SESSION_TTL, httponly=True, samesite="lax")


def _job_json(job: Job) -> dict:
    data = asdict(job)
    data["job_name"] = job.job_name
    return data


@router.get("/ui", response_class=HTMLResponse)
async def ui_index(request: Request):
    if not settings.configured:
        html = (STATIC_DIR / "setup.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__WEBSHARE_USER__", config.webshare_username))
    if not _session_ok(request):
        return HTMLResponse((STATIC_DIR / "login.html").read_text(encoding="utf-8"))
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__WEBSHARR_API_KEY__", config.api_key)
    html = html.replace("__WEBSHARR_VERSION__", __version__)
    return HTMLResponse(html)


@router.post("/ui/api/setup")
async def ui_setup(request: Request):
    """First-run: create the UI account and store Webshare credentials."""
    if settings.configured:
        return JSONResponse({"error": "Already configured"}, status_code=403)
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 4:
        return JSONResponse(
            {"error": "Username and a password of at least 4 characters are required"},
            status_code=400)

    settings.auth_username = username
    settings.auth_password_hash = hash_password(password)
    settings.webshare_username = (body.get("webshare_username") or "").strip()
    settings.webshare_password = body.get("webshare_password") or ""
    # Keep an explicitly configured key; otherwise generate one (shown in Settings).
    settings.api_key = config.api_key if config.api_key != "websharr" else secrets.token_hex(16)
    settings.save()
    settings.apply()
    request.app.state.webshare.set_credentials(config.webshare_username, config.webshare_password)
    logger.info("Initial setup completed (user=%s)", username)

    resp = JSONResponse({"ok": True})
    _set_session(resp)
    return resp


@router.post("/ui/api/login")
async def ui_login(request: Request):
    if not settings.configured:
        return JSONResponse({"error": "Not configured yet"}, status_code=400)
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if username != settings.auth_username or not verify_password(password, settings.auth_password_hash):
        await asyncio.sleep(0.4)  # blunt brute-force brake
        return JSONResponse({"error": "Invalid username or password"}, status_code=403)
    resp = JSONResponse({"ok": True})
    _set_session(resp)
    return resp


@router.post("/ui/api/logout")
async def ui_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/ui/api/settings")
async def ui_settings_get(request: Request):
    if not _authorized(request):
        return _unauthorized()
    return {
        "ui_username": settings.auth_username,
        "webshare_username": config.webshare_username,
        "webshare_password_set": bool(config.webshare_password),
        "api_key": config.api_key,
        "complete_dir": str(config.complete_dir),
        "incomplete_dir": str(config.incomplete_dir),
    }


@router.post("/ui/api/settings")
async def ui_settings_post(request: Request):
    if not _authorized(request):
        return _unauthorized()
    body = await request.json()

    if "webshare_username" in body:
        settings.webshare_username = (body.get("webshare_username") or "").strip()
        new_pass = body.get("webshare_password") or ""
        # Empty password field means "keep the current one".
        settings.webshare_password = new_pass or settings.webshare_password or config.webshare_password

    if body.get("new_password"):
        if not verify_password(body.get("current_password") or "", settings.auth_password_hash):
            return JSONResponse({"error": "Current password is incorrect"}, status_code=403)
        if len(body["new_password"]) < 4:
            return JSONResponse({"error": "Password must have at least 4 characters"}, status_code=400)
        settings.auth_password_hash = hash_password(body["new_password"])

    if body.get("regenerate_api_key"):
        settings.api_key = secrets.token_hex(16)

    settings.save()
    settings.apply()
    request.app.state.webshare.set_credentials(config.webshare_username, config.webshare_password)
    return {"ok": True, "api_key": config.api_key}


@router.post("/ui/api/test-webshare")
async def ui_test_webshare(request: Request):
    """Try logging in to Webshare with the given (or saved) credentials.

    Open during first-run setup; requires auth once configured.
    """
    if settings.configured and not _authorized(request):
        return _unauthorized()
    body = await request.json()
    username = (body.get("username") or "").strip() or config.webshare_username
    password = body.get("password") or config.webshare_password
    if not username or not password:
        return {"ok": False, "error": "Username and password are required"}

    client_cls = type(request.app.state.webshare)
    tmp = client_cls(username, password)
    try:
        user = await tmp.check_login()
        return {"ok": True, "username": user}
    except (WebshareError, httpx.HTTPError) as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await tmp.close()


@router.get("/ui/assets/{name}")
async def ui_asset(name: str):
    if name not in ALLOWED_ASSETS:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(ASSETS_DIR / name, media_type="image/svg+xml")


@router.get("/ui/api/status")
async def ui_status(request: Request):
    if not _authorized(request):
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
    if not _authorized(request):
        return _unauthorized()
    manager: DownloadManager = request.app.state.downloads
    return {"jobs": [_job_json(j) for j in manager.queue_jobs()]}


@router.get("/ui/api/history")
async def ui_history(request: Request):
    if not _authorized(request):
        return _unauthorized()
    manager: DownloadManager = request.app.state.downloads
    return {"jobs": [_job_json(j) for j in manager.history_jobs()]}


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTENSIONS)


def _normalize(text: str) -> str:
    """Lowercase, strip diacritics, collapse punctuation to spaces."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in text)


def _relevance(queries: list[str], name: str) -> float:
    """Fraction of query tokens present in the file name (best query wins).

    Webshare's fulltext search is fuzzy — for "zaklinac s01e03" it happily
    returns other episodes too, so size alone is a bad ranking.
    """
    name_tokens = set(_normalize(name).split())
    best = 0.0
    for query in queries:
        q_tokens = _normalize(query).split()
        if not q_tokens:
            continue
        score = sum(1 for t in q_tokens if t in name_tokens) / len(q_tokens)
        best = max(best, score)
    return best


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
    if not _authorized(request):
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

    merged.sort(key=lambda r: (-_relevance(queries, r.name), -r.size))
    return {
        "results": [_result_json(request, r) for r in merged[:limit]],
        "queries": queries,
    }
