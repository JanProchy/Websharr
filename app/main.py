"""Websharr — Webshare.cz bridge for the *arr stack.

Exposes a Torznab indexer (/torznab/api), a SABnzbd-compatible download
client (/sabnzbd/api) and a monitoring web UI (/ui) so Sonarr/Radarr can
search and download from a Webshare.cz premium account.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from . import applog
from . import notify
from .config import config
from .downloads import DownloadManager
from .sabnzbd import router as sabnzbd_router
from .settings import settings
from .torznab import router as torznab_router
from .ui import router as ui_router
from .webshare import WebshareClient

ACCOUNT_REFRESH = 3600  # seconds between Webshare account-status refreshes
DAY = 86400

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
        notify=_send_notification,
    )
    manager.ensure_dirs()
    manager.load_state()
    manager.resume_pending()
    app.state.webshare = client
    app.state.downloads = manager
    app.state.account = None
    app.state.account_ts = 0.0
    monitor_task = asyncio.create_task(_account_monitor(app))
    logger.info("Websharr %s started (user=%s)", __version__,
                config.webshare_username or "<not configured>")
    yield
    monitor_task.cancel()
    await asyncio.gather(monitor_task, return_exceptions=True)
    await manager.shutdown()
    await client.close()


async def _send_notification(title: str, body: str) -> None:
    """Best-effort notification to the configured Apprise URLs."""
    await notify.send(settings.notify_urls, title, body)


async def _account_monitor(app: FastAPI) -> None:
    """Refresh Webshare VIP/quota periodically; warn when VIP runs low or the
    account is unreachable. Cached in app.state for /stats and /metrics."""
    client: WebshareClient = app.state.webshare
    while True:
        if config.webshare_username:
            try:
                status = await client.account_status()
                app.state.account = status
                app.state.account_ts = time.time()
                notify.reset_throttle("webshare_down")
                urls = settings.notify_urls
                if not status["vip"]:
                    await notify.send_throttled(
                        urls, "vip", "Websharr: Webshare VIP expired",
                        "Webshare VIP has expired — downloads will fail until it is renewed.", DAY)
                elif status["vip_days"] <= settings.notify_vip_days:
                    await notify.send_throttled(
                        urls, "vip", "Websharr: Webshare VIP expiring soon",
                        f"Webshare VIP expires in {status['vip_days']} day(s) — {status['vip_until']}.", DAY)
                else:
                    notify.reset_throttle("vip")  # healthy again → warn afresh next time
            except Exception as exc:  # background loop must survive anything
                logger.warning("Webshare account status refresh failed: %s", exc)
                await notify.send_throttled(
                    settings.notify_urls, "webshare_down", "Websharr: Webshare unreachable",
                    f"Could not reach Webshare: {exc}", 6 * 3600)
        await asyncio.sleep(ACCOUNT_REFRESH)


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
    acct = getattr(app.state, "account", None) or {}
    vip_days = acct.get("vip_days")
    return {
        "downloading": len(downloading),
        "queued": len(queued),
        "speed": _fmt_speed(speed),
        "speed_bps": int(speed),
        "failed": failed,
        "vip": acct.get("vip"),
        "vip_days": vip_days,
        "vip_until": acct.get("vip_until"),
        # Preformatted for a dashboard: "282 d" or "expired".
        "vip_display": (f"{vip_days} d" if acct.get("vip") and vip_days is not None
                        else ("expired" if acct else "—")),
    }


@app.get("/metrics")
async def metrics(apikey: str = ""):
    """Prometheus exposition of queue/history/VIP gauges (API-key gated)."""
    if apikey != config.api_key:
        return PlainTextResponse("# unauthorized\n", status_code=403)
    manager: DownloadManager = app.state.downloads
    queue = manager.queue_jobs()
    downloading = sum(1 for j in queue if j.status == "downloading")
    waiting = sum(1 for j in queue if j.status in ("queued", "paused"))
    speed = sum(j.speed for j in queue if j.status == "downloading")
    history = manager.history_jobs()
    completed = sum(1 for j in history if j.status == "completed")
    failed = sum(1 for j in history if j.status == "failed")
    acct = getattr(app.state, "account", None) or {}

    lines: list[str] = []

    def gauge(name: str, value, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    gauge("websharr_up", 1, "Websharr is running")
    gauge("websharr_queue_downloading", downloading, "Active downloads")
    gauge("websharr_queue_waiting", waiting, "Queued or paused downloads")
    gauge("websharr_download_speed_bytes", int(speed), "Aggregate download speed (bytes/s)")
    gauge("websharr_history_completed", completed, "Completed downloads in history")
    gauge("websharr_history_failed", failed, "Failed downloads in history")
    if acct:
        gauge("websharr_vip", 1 if acct.get("vip") else 0, "Webshare VIP active (1/0)")
        gauge("websharr_vip_days", acct.get("vip_days", 0), "Days until Webshare VIP expiry")
        gauge("websharr_private_bytes", acct.get("private_bytes", 0), "Webshare private space used (bytes)")
        gauge("websharr_private_space_bytes", acct.get("private_space", 0), "Webshare private space total (bytes)")
    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="text/plain; version=0.0.4; charset=utf-8")
