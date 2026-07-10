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
import urllib.parse
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .applog import records as log_records
from .config import config
from .downloads import DownloadManager, Job
from .settings import SESSION_TTL, hash_password, settings, verify_password
from .torznab import (
    VIDEO_EXTENSIONS,
    build_queries,
    expand_titles,
    file_episode,
    matches_query,
    parse_query,
    relevance,
    release_title,
)
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


# The SPA is served at the bare domain and at each tab's own path, so the URL
# reads /search, /history, … (no /ui, no #hash) and survives a refresh. The
# JSON API and assets stay under /ui/api and /ui/assets.
UI_PATHS = ["/", "/ui", "/queue", "/history", "/search", "/log", "/help", "/settings"]


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


for _p in UI_PATHS:
    router.add_api_route(_p, ui_index, methods=["GET"], response_class=HTMLResponse,
                         include_in_schema=False)


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

    ws_user = (body.get("webshare_username") or "").strip()
    ws_pass = body.get("webshare_password") or ""
    digest = ""
    if ws_user and ws_pass:
        # Store the login digest instead of the plaintext password.
        try:
            digest = await request.app.state.webshare.compute_digest(ws_user, ws_pass)
        except (WebshareError, httpx.HTTPError) as exc:
            return JSONResponse(
                {"error": f"Could not reach Webshare.cz to store the password securely: {exc}"},
                status_code=502)

    settings.auth_username = username
    settings.auth_password_hash = hash_password(password)
    settings.webshare_username = ws_user
    settings.webshare_password = ""
    settings.webshare_password_digest = digest
    # Keep an explicitly configured key; otherwise generate one (shown in Settings).
    settings.api_key = config.api_key if config.api_key != "websharr" else secrets.token_hex(16)
    settings.save()
    settings.apply()
    request.app.state.webshare.set_credentials(
        config.webshare_username, config.webshare_password, config.webshare_password_digest)
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
        "aliases": settings.aliases,
        "tmdb_token_set": bool(settings.tmdb_token),
    }


@router.post("/ui/api/aliases")
async def ui_aliases_post(request: Request):
    """Replace the search alias map (*arr title -> Webshare/CZ title)."""
    if not _authorized(request):
        return _unauthorized()
    body = await request.json()
    clean = []
    for a in body.get("aliases", []):
        frm, to = (a.get("from") or "").strip(), (a.get("to") or "").strip()
        if frm and to:
            clean.append({"from": frm, "to": to})
    settings.aliases = clean
    settings.save()
    return {"ok": True, "aliases": settings.aliases}


@router.post("/ui/api/settings")
async def ui_settings_post(request: Request):
    if not _authorized(request):
        return _unauthorized()
    body = await request.json()

    if "webshare_username" in body:
        ws_user = (body.get("webshare_username") or "").strip()
        new_pass = body.get("webshare_password") or ""
        if new_pass:
            try:
                digest = await request.app.state.webshare.compute_digest(ws_user, new_pass)
            except (WebshareError, httpx.HTTPError) as exc:
                return JSONResponse(
                    {"error": f"Could not reach Webshare.cz to store the password securely: {exc}"},
                    status_code=502)
            settings.webshare_password = ""
            settings.webshare_password_digest = digest
        elif ws_user != config.webshare_username:
            # The digest is salted per account, so a new account needs its password.
            return JSONResponse(
                {"error": "Enter the Webshare password when changing the account"},
                status_code=400)
        elif not settings.webshare_password_digest and not settings.webshare_password:
            # Credentials so far came from env vars only; carry the password over
            # (it gets converted to a digest on the next startup).
            settings.webshare_password = config.webshare_password
        settings.webshare_username = ws_user

    if body.get("new_password"):
        if not verify_password(body.get("current_password") or "", settings.auth_password_hash):
            return JSONResponse({"error": "Current password is incorrect"}, status_code=403)
        if len(body["new_password"]) < 4:
            return JSONResponse({"error": "Password must have at least 4 characters"}, status_code=400)
        settings.auth_password_hash = hash_password(body["new_password"])

    if body.get("regenerate_api_key"):
        settings.api_key = secrets.token_hex(16)

    if "tmdb_token" in body:
        # Empty string clears it; a value sets it.
        settings.tmdb_token = (body.get("tmdb_token") or "").strip()

    settings.save()
    settings.apply()
    request.app.state.webshare.set_credentials(
        config.webshare_username, config.webshare_password, config.webshare_password_digest)
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
    digest = ""
    if not password and username == config.webshare_username:
        digest = config.webshare_password_digest  # test the stored credentials
    if not username or not (password or digest):
        return {"ok": False, "error": "Username and password are required"}

    client_cls = type(request.app.state.webshare)
    tmp = client_cls(username, password, digest)
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


@router.get("/ui/api/log")
async def ui_log(request: Request):
    if not _authorized(request):
        return _unauthorized()
    try:
        after = int(request.query_params.get("after", "0") or 0)
    except ValueError:
        after = 0
    return {"records": log_records(after=after)}


@router.get("/ui/api/status")
async def ui_status(request: Request):
    if not _authorized(request):
        return _unauthorized()
    manager: DownloadManager = request.app.state.downloads
    return {
        "app": "websharr",
        "version": __version__,
        "user": settings.auth_username or None,       # the signed-in Websharr account
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


@router.post("/ui/api/history/delete")
async def ui_history_delete(request: Request):
    """Really remove a Websharr history record (unlike Sonarr's hide)."""
    if not _authorized(request):
        return _unauthorized()
    body = await request.json()
    nzo_id = body.get("nzo_id", "")
    ok = request.app.state.downloads.delete(nzo_id, del_files=bool(body.get("del_files")))
    return {"ok": ok}


@router.post("/ui/api/history/clear")
async def ui_history_clear(request: Request):
    if not _authorized(request):
        return _unauthorized()
    removed = request.app.state.downloads.clear_history()
    return {"ok": True, "removed": removed}


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTENSIONS)


def _result_json(request: Request, r: SearchResult, release: str) -> dict:
    base = str(request.base_url).rstrip("/")
    nzb_url = (
        f"{base}/torznab/nzb/{r.ident}"
        f"?apikey={config.api_key}"
        f"&name={urllib.parse.quote(r.name)}&size={r.size}"
        f"&nzbname={urllib.parse.quote(release)}"
    )
    return {
        "ident": r.ident,
        "name": r.name,
        "title": r.name.rsplit(".", 1)[0] if "." in r.name else r.name,
        # release name carrying SxxEyy (for tvsearch); sent as nzbname on grab.
        "release": release,
        "format": (r.type or (r.name.rsplit(".", 1)[-1] if "." in r.name else "")).lower(),
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

    t, q, season, ep = parse_query(t, params.get("q", ""), params.get("season"), params.get("ep"))
    titles = await expand_titles(
        t, q, params.get("cat"), tvdbid=params.get("tvdbid"),
        imdbid=params.get("imdbid"), tmdbid=params.get("tmdbid"))
    queries = []
    for title in titles:
        for v in build_queries(t, title, season, ep):
            if v not in queries:
                queries.append(v)
    if not queries:
        return {"results": [], "queries": []}

    limit = min(int(params.get("limit", str(config.search_limit)) or config.search_limit), 100)

    want_ep = int(ep) if (t == "tvsearch" and ep and str(ep).isdigit()) else None
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
            if not matches_query(titles, r.name):
                continue  # drop Webshare's loose non-matching fulltext hits
            if want_ep is not None and file_episode(titles, r.name) != want_ep:
                continue  # OR-based fulltext returns every episode; keep the asked one
            seen.add(r.ident)
            merged.append(r)

    merged.sort(key=lambda r: (-relevance(queries, r.name), -r.size))
    rel_season = season if t == "tvsearch" else None
    rel_ep = ep if t == "tvsearch" else None
    return {
        "results": [
            _result_json(request, r, release_title(q, rel_season, rel_ep, r.name))
            for r in merged[:limit]
        ],
        "queries": queries,
    }


@router.get("/ui/api/fileinfo")
async def ui_fileinfo(request: Request):
    """Per-file metadata (duration, resolution, codec) for the given idents.

    Fetched lazily by the search UI so the results table renders immediately;
    one Webshare call per ident, run concurrently and best-effort.
    """
    if not _authorized(request):
        return _unauthorized()
    idents = [i for i in (request.query_params.get("idents", "").split(",")) if i][:60]
    if not idents:
        return {"info": {}}

    client = request.app.state.webshare
    sem = asyncio.Semaphore(8)

    async def one(ident: str):
        async with sem:
            try:
                return ident, await client.file_info(ident)
            except (WebshareError, httpx.HTTPError) as exc:
                logger.debug("file_info %s failed: %s", ident, exc)
                return ident, None

    pairs = await asyncio.gather(*(one(i) for i in idents))
    return {"info": {ident: data for ident, data in pairs if data}}
