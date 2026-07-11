"""Websharr — Webshare.cz bridge for the *arr stack.

Exposes a Torznab indexer (/torznab/api), a SABnzbd-compatible download
client (/sabnzbd/api) and a monitoring web UI (/ui) so Sonarr/Radarr can
search and download from a Webshare.cz premium account.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from . import applog
from .config import config
from .downloads import DownloadManager
from .sabnzbd import router as sabnzbd_router
from .settings import settings
from .torznab import router as torznab_router
from .ui import router as ui_router
from .webshare import WebshareClient

logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("websharr")


@asynccontextmanager
async def lifespan(app: FastAPI):
    applog.install()  # start capturing logs for the UI Log tab
    settings.load()
    settings.ensure_api_key()
    settings.apply()
    client = WebshareClient(config.webshare_username, config.webshare_password,
                            config.webshare_password_digest)

    # Legacy settings.json stored the Webshare password in plaintext —
    # convert it to the login digest so the real password leaves the disk.
    if settings.webshare_password and settings.webshare_username:
        try:
            digest = await client.compute_digest(
                settings.webshare_username, settings.webshare_password)
            settings.webshare_password = ""
            settings.webshare_password_digest = digest
            settings.save()
            settings.apply()
            client.set_credentials(config.webshare_username,
                                   password_digest=config.webshare_password_digest)
            logger.info("Migrated stored Webshare password to a login digest")
        except Exception as exc:
            logger.warning("Webshare password not migrated to digest yet: %s", exc)
    manager = DownloadManager(
        client=client,
        complete_dir=config.complete_dir,
        incomplete_dir=config.incomplete_dir,
        state_file=config.state_file,
        max_concurrent=settings.max_concurrent,
    )
    manager.ensure_dirs()
    manager.load_state()
    manager.resume_pending()
    app.state.webshare = client
    app.state.downloads = manager
    logger.info("Websharr %s started (user=%s)", __version__,
                config.webshare_username or "<not configured>")
    yield
    await manager.shutdown()
    await client.close()


app = FastAPI(title="Websharr", version=__version__, lifespan=lifespan)
app.include_router(torznab_router)
app.include_router(sabnzbd_router)
app.include_router(ui_router)


@app.get("/status")
async def status():
    manager: DownloadManager = app.state.downloads
    return {
        "app": "websharr",
        "version": __version__,
        "webshare_user": config.webshare_username or None,
        "queue": len(manager.queue_jobs()),
        "history": len(manager.history_jobs()),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


def _fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "0 KB/s"
    mb = bps / (1024 * 1024)
    return f"{mb:.1f} MB/s" if mb >= 1 else f"{bps / 1024:.0f} KB/s"


@app.get("/stats")
async def stats(apikey: str = ""):
    """Compact metrics for a Homepage (or similar) dashboard widget.

    Guarded by the same API key as the indexer/client so queue details aren't
    public. Speed is a preformatted string; a raw bytes/s value is included too.
    """
    if apikey != config.api_key:
        return JSONResponse({"error": "API Key Incorrect"}, status_code=403)
    manager: DownloadManager = app.state.downloads
    queue = manager.queue_jobs()
    downloading = [j for j in queue if j.status == "downloading"]
    queued = [j for j in queue if j.status in ("queued", "paused")]
    speed = sum(j.speed for j in downloading)
    failed = sum(1 for j in manager.history_jobs() if j.status == "failed")
    return {
        "downloading": len(downloading),
        "queued": len(queued),
        "speed": _fmt_speed(speed),
        "speed_bps": int(speed),
        "failed": failed,
    }
