"""SABnzbd API emulation, just enough for Sonarr/Radarr download client support.

Mounted at /sabnzbd/api (configure the download client with URL base /sabnzbd).
Implements: version, get_config, get_cats, fullstatus, queue (list/delete),
history (list/delete), retry, addfile, addurl.
"""

import logging
import re
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import config
from .downloads import DownloadManager, Job
from .nzb import parse_nzb

logger = logging.getLogger("websharr.sabnzbd")

router = APIRouter()

SAB_VERSION = "4.3.3"


def _err(message: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"status": False, "error": message}, status_code=status_code)


def _fmt_mb(num_bytes: float) -> str:
    return f"{num_bytes / (1024 * 1024):.2f}"


def _fmt_timeleft(job: Job) -> str:
    if job.speed <= 0 or job.size <= 0:
        return "0:00:00"
    secs = int(max(job.size - job.downloaded, 0) / job.speed)
    return f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


def _queue_slot(job: Job, index: int) -> dict:
    status = "Downloading" if job.status == "downloading" else "Queued"
    pct = int(job.downloaded * 100 / job.size) if job.size else 0
    return {
        "index": index,
        "nzo_id": job.nzo_id,
        "filename": job.job_name,
        "cat": job.category,
        "priority": "Normal",
        "status": status,
        "percentage": str(pct),
        "mb": _fmt_mb(job.size),
        "mbleft": _fmt_mb(max(job.size - job.downloaded, 0)),
        "size": f"{job.size / (1024 ** 3):.2f} GB",
        "sizeleft": f"{max(job.size - job.downloaded, 0) / (1024 ** 3):.2f} GB",
        "timeleft": _fmt_timeleft(job),
    }


def _history_slot(job: Job) -> dict:
    return {
        "nzo_id": job.nzo_id,
        "name": job.job_name,
        "nzb_name": f"{job.job_name}.nzb",
        "category": job.category,
        "bytes": job.size,
        "size": f"{job.size / (1024 ** 3):.2f} GB",
        "storage": job.storage,
        "path": job.storage,
        "status": "Completed" if job.status == "completed" else "Failed",
        "fail_message": job.error,
        "completed": int(job.completed_ts),
        "download_time": max(int(job.completed_ts - job.added_ts), 0),
        "postproc_time": 0,
        "stage_log": [],
    }


def _get_config_payload() -> dict:
    return {
        "config": {
            "misc": {
                "complete_dir": str(config.complete_dir),
                "download_dir": str(config.incomplete_dir),
                "pre_check": 0,
                "history_retention": "",
                "history_retention_option": "all",
                "history_retention_number": 0,
                "enable_tv_sorting": 0,
                "tv_categories": [],
                "enable_movie_sorting": 0,
                "movie_categories": [],
                "enable_date_sorting": 0,
                "date_categories": [],
            },
            "categories": [
                {"name": "*", "pp": "3", "script": "None", "dir": "", "priority": 0},
                {"name": "tv", "pp": "3", "script": "None", "dir": "tv", "priority": 0},
                {"name": "movies", "pp": "3", "script": "None", "dir": "movies", "priority": 0},
            ],
            "servers": [{"name": "websharr", "host": "webshare.cz", "connections": 4}],
            "sorters": [],
        }
    }


async def _extract_nzb_payload(request: Request, params) -> tuple[str, str, int] | None:
    """Return (ident, name, size) from an addfile upload or addurl link."""
    mode = params.get("mode")
    if mode == "addurl":
        url = params.get("name", "")
        m = re.search(r"/torznab/nzb/([^/?#]+)", url)
        if not m:
            return None
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        name = qs.get("name", [m.group(1)])[0]
        try:
            size = int(qs.get("size", ["0"])[0])
        except ValueError:
            size = 0
        return m.group(1), name, size

    form = await request.form()
    for key in ("nzbfile", "name"):
        upload = form.get(key)
        if upload is not None and hasattr(upload, "read"):
            payload = parse_nzb(await upload.read())
            if payload is not None:
                return payload.ident, payload.name, payload.size
    return None


@router.api_route("/sabnzbd/api", methods=["GET", "POST"])
@router.api_route("/api", methods=["GET", "POST"])
async def sabnzbd_api(request: Request):
    # SAB clients send parameters in the query string even for POST (addfile
    # sends only the file in the body), but accept form fields too.
    params = dict(request.query_params)
    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if "form" in content_type:
            form = await request.form()
            for k, v in form.items():
                if isinstance(v, str) and k not in params:
                    params[k] = v

    if params.get("apikey") != config.api_key:
        return _err("API Key Incorrect", status_code=403)

    mode = params.get("mode", "")
    manager: DownloadManager = request.app.state.downloads

    if mode == "version":
        return JSONResponse({"version": SAB_VERSION})

    if mode == "get_config":
        return JSONResponse(_get_config_payload())

    if mode == "get_cats":
        return JSONResponse({"categories": ["*", "tv", "movies"]})

    if mode == "fullstatus":
        return JSONResponse({"status": {"version": SAB_VERSION, "uptime": "1h",
                                        "diskspace1": "1000.0", "diskspace2": "1000.0"}})

    if mode == "queue":
        if params.get("name") == "delete":
            ok = all(
                manager.delete(nzo_id, del_files=params.get("del_files") == "1")
                for nzo_id in params.get("value", "").split(",") if nzo_id
            )
            return JSONResponse({"status": ok})
        if params.get("name") in ("pause", "resume"):
            return JSONResponse({"status": True})
        slots = [_queue_slot(j, i) for i, j in enumerate(manager.queue_jobs())]
        return JSONResponse({"queue": {
            "paused": False,
            "slots": slots,
            "noofslots": len(slots),
            "version": SAB_VERSION,
        }})

    if mode == "history":
        if params.get("name") == "delete":
            value = params.get("value", "")
            if value == "all":
                for job in list(manager.history_jobs()):
                    manager.delete(job.nzo_id)
                return JSONResponse({"status": True})
            ok = all(
                manager.delete(nzo_id, del_files=params.get("del_files") == "1")
                for nzo_id in value.split(",") if nzo_id
            )
            return JSONResponse({"status": ok})
        slots = [_history_slot(j) for j in manager.history_jobs()]
        return JSONResponse({"history": {
            "slots": slots,
            "noofslots": len(slots),
            "version": SAB_VERSION,
        }})

    if mode == "retry":
        job = manager.retry(params.get("value", ""))
        if job is None:
            return _err("No failed job with that nzo_id")
        return JSONResponse({"status": True, "nzo_ids": [job.nzo_id]})

    if mode in ("addfile", "addurl"):
        extracted = await _extract_nzb_payload(request, params)
        if extracted is None:
            return _err("Could not extract Webshare ident from NZB")
        ident, name, size = extracted
        category = params.get("cat", "*")
        job = manager.add(ident=ident, name=name, size=size, category=category)
        return JSONResponse({"status": True, "nzo_ids": [job.nzo_id]})

    logger.warning("Unhandled SABnzbd mode: %s", mode)
    return _err(f"Unknown mode '{mode}'")
