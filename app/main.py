"""Websharr — Webshare.cz bridge for the *arr stack.

Exposes a Torznab indexer (/torznab/api), a SABnzbd-compatible download
client (/sabnzbd/api) and a monitoring web UI (/ui) so Sonarr/Radarr can
search and download from a Webshare.cz premium account.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
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
    settings.load()
    settings.apply()
    client = WebshareClient(config.webshare_username, config.webshare_password)
    manager = DownloadManager(
        client=client,
        complete_dir=config.complete_dir,
        incomplete_dir=config.incomplete_dir,
        state_file=config.state_file,
        max_concurrent=config.max_concurrent,
    )
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


@app.get("/")
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
