"""Download manager: queues Webshare downloads and tracks SABnzbd-style state.

Jobs persist to a JSON state file so history survives restarts; jobs that were
still downloading are re-queued on startup and resume from the partial file in
the incomplete dir via an HTTP Range request.
"""

import asyncio
import json
import logging
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .nzb import sanitize_filename
from .webshare import WebshareClient, WebshareError

logger = logging.getLogger("websharr.downloads")

CHUNK_SIZE = 1024 * 1024
HISTORY_CAP = 500  # keep this many completed/failed records in the UI history


def _total_size(resp: httpx.Response, offset: int) -> int:
    """Full file size implied by the response (0 if unknown)."""
    if resp.status_code == 206:
        content_range = resp.headers.get("content-range", "")  # bytes 100-999/1000
        if "/" in content_range:
            try:
                return int(content_range.rsplit("/", 1)[1])
            except ValueError:
                pass
        return offset + int(resp.headers.get("content-length", "0") or 0)
    return int(resp.headers.get("content-length", "0") or 0)


@dataclass
class Job:
    nzo_id: str
    ident: str
    name: str  # sanitized file name incl. extension
    category: str
    size: int
    # Release/display name (e.g. "Skvrna S01E01 - ...") used for the job folder
    # and reported to Sonarr/Radarr, so import parses the episode even when the
    # raw Webshare filename lacks SxxExx. Falls back to the filename stem.
    title: str = ""
    status: str = "queued"  # queued | downloading | paused | completed | failed
    downloaded: int = 0
    speed: float = 0.0  # bytes/s, not persisted meaningfully
    error: str = ""
    storage: str = ""  # final job directory (what Sonarr imports from)
    added_ts: float = field(default_factory=time.time)
    completed_ts: float = 0.0
    # Sonarr removes a download from the SABnzbd history once it has imported it;
    # we honor that for the SABnzbd view but keep the record in the Websharr UI
    # history. This flag hides it from Sonarr only.
    sab_hidden: bool = False

    @property
    def job_name(self) -> str:
        """Name shown to *arr and used for the download folder."""
        if self.title:
            return self.title
        return self.name.rsplit(".", 1)[0] if "." in self.name else self.name


class DownloadManager:
    def __init__(self, client: WebshareClient, complete_dir: Path, incomplete_dir: Path,
                 state_file: Path, max_concurrent: int = 2):
        self._client = client
        self._complete_dir = complete_dir
        self._incomplete_dir = incomplete_dir
        self._state_file = state_file
        self._max_concurrent = max(1, int(max_concurrent))
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def set_max_concurrent(self, value: int) -> int:
        """Change the concurrency limit at runtime. Running downloads keep their
        slot; a higher limit immediately starts more queued jobs, a lower one
        just applies as active downloads finish."""
        self._max_concurrent = max(1, min(int(value), 10))
        self._schedule()
        return self._max_concurrent

    def ensure_dirs(self) -> None:
        """Pre-create the category folders so *arr's download-client health
        check ("directory does not exist inside the container") passes before
        the first download of that category completes."""
        self._incomplete_dir.mkdir(parents=True, exist_ok=True)
        for category in ("tv", "movies"):
            (self._complete_dir / category).mkdir(parents=True, exist_ok=True)

    # -- persistence -------------------------------------------------------

    def load_state(self) -> None:
        if not self._state_file.exists():
            return
        try:
            raw = json.loads(self._state_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read state file: %s", exc)
            return
        for item in raw.get("jobs", []):
            item.pop("speed", None)
            try:
                job = Job(**item)
            except TypeError:
                continue
            self._jobs[job.nzo_id] = job

    def resume_pending(self) -> None:
        for job in self._jobs.values():
            if job.status in ("queued", "downloading"):
                job.status = "queued"
                logger.info("Re-queued unfinished job %s (%s)", job.nzo_id, job.name)
        self._schedule()

    def _save_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"jobs": [asdict(j) for j in self._jobs.values()]}
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._state_file)
        except OSError as exc:
            logger.warning("Could not write state file: %s", exc)

    # -- public API --------------------------------------------------------

    def add(self, ident: str, name: str, size: int, category: str, title: str = "") -> Job:
        job = Job(
            nzo_id=f"SABnzbd_nzo_{uuid.uuid4().hex[:12]}",
            ident=ident,
            name=sanitize_filename(name),
            category=category or "*",
            size=size,
            title=sanitize_filename(title) if title else "",
        )
        self._jobs[job.nzo_id] = job
        self._save_state()
        logger.info("Queued download %s -> %s (cat=%s)", ident, job.name, job.category)
        self._schedule()
        return job

    def get(self, nzo_id: str) -> Job | None:
        return self._jobs.get(nzo_id)

    _QUEUE_STATES = ("queued", "downloading", "paused")

    def queue_jobs(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in self._QUEUE_STATES]

    def reorder(self, order: list[str]) -> bool:
        """Rearrange the queue to match the given list of nzo_ids. Only the order
        in which queued jobs start is affected — jobs already downloading keep
        their slot. Unlisted queue jobs keep their relative order at the end."""
        rank = {nzo: i for i, nzo in enumerate(order)}
        queue = [j for j in self._jobs.values() if j.status in self._QUEUE_STATES]
        rest = [j for j in self._jobs.values() if j.status not in self._QUEUE_STATES]
        queue.sort(key=lambda j: rank.get(j.nzo_id, len(rank)))
        self._jobs = {j.nzo_id: j for j in queue}
        for job in rest:
            self._jobs[job.nzo_id] = job
        self._save_state()
        self._schedule()
        return True

    def pause(self, nzo_id: str) -> bool:
        """Stop an active/queued download but keep its partial file for resume."""
        job = self._jobs.get(nzo_id)
        if job is None or job.status not in ("queued", "downloading"):
            return False
        job.status = "paused"
        job.speed = 0.0
        task = self._tasks.pop(nzo_id, None)
        if task is not None and not task.done():
            task.cancel()  # CancelledError unwinds; status stays "paused"
        self._save_state()
        self._schedule()  # let a waiting job take the freed slot
        logger.info("Paused %s (%s)", nzo_id, job.name)
        return True

    def resume(self, nzo_id: str) -> bool:
        """Re-queue a paused job; the worker continues from the partial file."""
        job = self._jobs.get(nzo_id)
        if job is None or job.status != "paused":
            return False
        job.status = "queued"
        self._schedule()
        self._save_state()
        logger.info("Resumed %s (%s)", nzo_id, job.name)
        return True

    def history_jobs(self, include_hidden: bool = True) -> list[Job]:
        """Completed/failed jobs, newest first. `include_hidden=False` drops the
        ones Sonarr already removed from its own history (the SABnzbd view)."""
        jobs = [j for j in self._jobs.values()
                if j.status in ("completed", "failed")
                and (include_hidden or not j.sab_hidden)]
        return sorted(jobs, key=lambda j: j.completed_ts, reverse=True)

    def hide_from_sab(self, nzo_id: str, del_files: bool = False) -> bool:
        """Sonarr imported & removed this download: hide it from the SABnzbd
        history but keep the record for the Websharr UI. Files may be dropped."""
        job = self._jobs.get(nzo_id)
        if job is None:
            return False
        job.sab_hidden = True
        if del_files and job.storage:
            shutil.rmtree(job.storage, ignore_errors=True)
        self._save_state()
        return True

    def clear_history(self) -> int:
        """Remove all completed/failed records from the Websharr history."""
        gone = [j.nzo_id for j in self._jobs.values() if j.status in ("completed", "failed")]
        for nzo_id in gone:
            self.delete(nzo_id)
        return len(gone)

    def retry(self, nzo_id: str) -> Job | None:
        """Re-queue a failed history job; resumes from its partial file if any."""
        job = self._jobs.get(nzo_id)
        if job is None or job.status != "failed":
            return None
        job.status = "queued"
        job.error = ""
        job.completed_ts = 0.0
        self._schedule()
        self._save_state()
        logger.info("Retrying job %s (%s)", job.nzo_id, job.name)
        return job

    def delete(self, nzo_id: str, del_files: bool = False) -> bool:
        job = self._jobs.pop(nzo_id, None)
        if job is None:
            return False
        task = self._tasks.pop(nzo_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._cleanup_incomplete(job)
        if del_files and job.storage:
            shutil.rmtree(job.storage, ignore_errors=True)
        self._save_state()
        return True

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._save_state()

    # -- internals ---------------------------------------------------------

    def _incomplete_path(self, job: Job) -> Path:
        return self._incomplete_dir / job.nzo_id

    def _cleanup_incomplete(self, job: Job) -> None:
        shutil.rmtree(self._incomplete_path(job), ignore_errors=True)

    def _schedule(self) -> None:
        """Start queued jobs in queue order until the concurrency limit is hit.

        Replaces a fixed semaphore so the limit can change at runtime and the
        queue order (drag-and-drop) decides which job runs next. A job's slot is
        claimed synchronously (status -> downloading) before its task is created,
        so repeated calls in the same tick never over-schedule.
        """
        active = sum(1 for j in self._jobs.values() if j.status == "downloading")
        for job in self._jobs.values():
            if active >= self._max_concurrent:
                break
            if job.status == "queued" and job.nzo_id not in self._tasks:
                job.status = "downloading"
                self._tasks[job.nzo_id] = asyncio.create_task(self._run(job))
                active += 1

    async def _run(self, job: Job) -> None:
        try:
            if job.nzo_id not in self._jobs:
                return  # deleted before the task got to run
            self._save_state()
            await self._download(job)
            job.status = "completed"
            job.completed_ts = time.time()
            logger.info("Completed %s -> %s", job.nzo_id, job.storage)
        except asyncio.CancelledError:
            logger.info("Cancelled %s", job.nzo_id)
            raise
        except (WebshareError, httpx.HTTPError, OSError) as exc:
            job.status = "failed"
            job.error = str(exc)
            job.completed_ts = time.time()
            # Keep the partial file so a retry can resume; delete() cleans it up.
            logger.error("Download %s failed: %s", job.nzo_id, exc)
        finally:
            self._tasks.pop(job.nzo_id, None)
            if job.nzo_id in self._jobs:
                self._prune_history()
                self._save_state()
            self._schedule()  # a slot freed — start the next queued job

    def _prune_history(self) -> None:
        """Cap the retained history so state.json can't grow without bound."""
        history = sorted(
            (j for j in self._jobs.values() if j.status in ("completed", "failed")),
            key=lambda j: j.completed_ts, reverse=True,
        )
        for job in history[HISTORY_CAP:]:
            self.delete(job.nzo_id)

    async def _download(self, job: Job) -> None:
        url = await self._client.file_link(job.ident)

        work_dir = self._incomplete_path(job)
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / job.name

        offset = target.stat().st_size if target.exists() else 0

        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as http:
            while True:
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                async with http.stream("GET", url, headers=headers) as resp:
                    if offset and resp.status_code == 416:
                        # Partial file no longer matches the remote; start over.
                        target.unlink(missing_ok=True)
                        offset = 0
                        continue
                    if offset and resp.status_code != 206:
                        offset = 0  # server ignored the Range header
                    resp.raise_for_status()
                    total = _total_size(resp, offset)
                    if total:
                        job.size = total
                    if offset:
                        logger.info("Resuming %s from %d bytes", job.nzo_id, offset)
                    await self._stream_to_file(job, resp, target, offset)
                break

        final_dir = self._complete_dir / job.category / job.job_name
        final_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), final_dir / job.name)
        shutil.rmtree(work_dir, ignore_errors=True)
        job.storage = str(final_dir)
        job.size = max(job.size, job.downloaded)

    @staticmethod
    async def _stream_to_file(job: Job, resp: httpx.Response, target: Path, offset: int) -> None:
        job.downloaded = offset
        last_t = time.monotonic()
        last_bytes = offset
        with target.open("ab" if offset else "wb") as fh:
            async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                fh.write(chunk)
                job.downloaded += len(chunk)
                now = time.monotonic()
                if now - last_t >= 2.0:
                    job.speed = (job.downloaded - last_bytes) / (now - last_t)
                    last_t = now
                    last_bytes = job.downloaded
