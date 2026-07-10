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
    status: str = "queued"  # queued | downloading | completed | failed
    downloaded: int = 0
    speed: float = 0.0  # bytes/s, not persisted meaningfully
    error: str = ""
    storage: str = ""  # final job directory (what Sonarr imports from)
    added_ts: float = field(default_factory=time.time)
    completed_ts: float = 0.0

    @property
    def job_name(self) -> str:
        stem = self.name.rsplit(".", 1)[0] if "." in self.name else self.name
        return stem


class DownloadManager:
    def __init__(self, client: WebshareClient, complete_dir: Path, incomplete_dir: Path,
                 state_file: Path, max_concurrent: int = 2):
        self._client = client
        self._complete_dir = complete_dir
        self._incomplete_dir = incomplete_dir
        self._state_file = state_file
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

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
                self._start(job)
                logger.info("Re-queued unfinished job %s (%s)", job.nzo_id, job.name)

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

    def add(self, ident: str, name: str, size: int, category: str) -> Job:
        job = Job(
            nzo_id=f"SABnzbd_nzo_{uuid.uuid4().hex[:12]}",
            ident=ident,
            name=sanitize_filename(name),
            category=category or "*",
            size=size,
        )
        self._jobs[job.nzo_id] = job
        self._start(job)
        self._save_state()
        logger.info("Queued download %s -> %s (cat=%s)", ident, job.name, job.category)
        return job

    def get(self, nzo_id: str) -> Job | None:
        return self._jobs.get(nzo_id)

    def queue_jobs(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in ("queued", "downloading")]

    def history_jobs(self) -> list[Job]:
        jobs = [j for j in self._jobs.values() if j.status in ("completed", "failed")]
        return sorted(jobs, key=lambda j: j.completed_ts, reverse=True)

    def retry(self, nzo_id: str) -> Job | None:
        """Re-queue a failed history job; resumes from its partial file if any."""
        job = self._jobs.get(nzo_id)
        if job is None or job.status != "failed":
            return None
        job.status = "queued"
        job.error = ""
        job.completed_ts = 0.0
        self._start(job)
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

    def _start(self, job: Job) -> None:
        self._tasks[job.nzo_id] = asyncio.create_task(self._run(job))

    async def _run(self, job: Job) -> None:
        try:
            async with self._semaphore:
                if job.nzo_id not in self._jobs:
                    return  # deleted while queued
                job.status = "downloading"
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
                self._save_state()

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
