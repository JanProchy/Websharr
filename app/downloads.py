"""Download manager: queues Webshare downloads and tracks SABnzbd-style state.

Jobs persist to a JSON state file so history survives restarts; jobs that were
still downloading are re-queued on startup and resume from the partial file in
the incomplete dir via an HTTP Range request.
"""

import asyncio
import json
import logging
import re
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
# A connection dropped mid-file is resumed from the partial file this many
# times (waiting RESUME_DELAYS seconds) before the job fails.
RESUME_ATTEMPTS = 5
RESUME_DELAYS = (5, 15, 30, 60, 120)
RESUME_FILE = ".resume.json"  # validators of a partial file, next to it


def describe_error(exc: BaseException) -> str:
    """Error text for the history and *arr's fail message. Several httpx errors
    (ReadTimeout, PoolTimeout…) carry no message, which left "failed: " with
    nothing to go on; then the exception type is said instead."""
    return str(exc).strip() or type(exc).__name__


class DownloadIntegrityError(Exception):
    """The server's answer can't be the file the job announced (wrong size)."""


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


def _content_range(resp: httpx.Response) -> tuple[int, int, int] | None:
    """(start, end, total) of a Content-Range header; None if missing/invalid."""
    m = _CONTENT_RANGE.fullmatch(resp.headers.get("content-range", "").strip())
    if not m:
        return None
    start, end, total = (int(g) for g in m.groups())
    return (start, end, total) if start <= end < total else None


def _content_length(resp: httpx.Response) -> int | None:
    try:
        return int(resp.headers["content-length"])
    except (KeyError, ValueError):
        return None


@dataclass
class _Validators:
    """ETag / Last-Modified of the response a partial download was started
    with. A resume sends one as If-Range, so the server only answers 206 while
    it is still that file.

    Kept in a sidecar file next to the partial data, not in the job state: an
    older version skips state records with unknown fields, so a rollback would
    lose every job. The file goes away with the partial file (restart, delete,
    completion); a missing or unreadable one means "start over"."""

    path: Path
    etag: str = ""
    last_modified: str = ""

    @classmethod
    def load(cls, path: Path) -> "_Validators":
        try:
            data = json.loads(path.read_text())
            etag, last_modified = data.get("etag", ""), data.get("last_modified", "")
        except (OSError, ValueError, AttributeError):
            return cls(path)
        if not isinstance(etag, str) or not isinstance(last_modified, str):
            return cls(path)
        return cls(path, etag, last_modified)

    def save(self, resp: httpx.Response) -> None:
        self.etag = resp.headers.get("etag", "")
        self.last_modified = resp.headers.get("last-modified", "")
        try:
            self.path.write_text(json.dumps({"etag": self.etag, "last_modified": self.last_modified}))
        except OSError as exc:  # only costs the resume after a restart
            logger.warning("Could not write %s: %s", self.path, exc)

    def clear(self) -> None:
        self.etag = self.last_modified = ""
        self.path.unlink(missing_ok=True)

    @property
    def if_range(self) -> str:
        """Validator sent as If-Range on a resume; "" when nothing ties the
        partial file to the remote one (then it can't be resumed safely). A
        weak ETag may match a file with different bytes, so it doesn't count."""
        if self.etag and not self.etag.startswith("W/"):
            return self.etag
        return self.last_modified


def _resume_problem(seen: _Validators, resp: httpx.Response, offset: int, expected: int) -> str:
    """Why a 206 answer to `Range: bytes=<offset>-` must not be appended to the
    partial file ("" if it may). The range has to start exactly at the end of
    the partial file and run to the end of a file of the expected size.

    The request carried If-Range, so a 206 already means the server found the
    validator unchanged (RFC 9110 13.1.5); a changed file gets a 200. Webshare's
    nginx doesn't repeat Last-Modified on a 206, so a missing validator is fine,
    but one the 206 does carry must still be the one the partial file has."""
    rng = _content_range(resp)
    if rng is None:
        return f"invalid Content-Range {resp.headers.get('content-range', '')!r}"
    start, end, total = rng
    if start != offset:
        return f"range starts at {start}, not {offset}"
    if end != total - 1:
        return f"range ends at {end}, before the end of the {total}-byte file"
    if expected and total != expected:
        return f"remote file is {total} bytes, expected {expected}"
    length = _content_length(resp)
    if length is not None and length != end - start + 1:
        return f"Content-Length {length} doesn't match the range"
    etag, last_modified = resp.headers.get("etag"), resp.headers.get("last-modified")
    if seen.etag and etag is not None and etag != seen.etag:
        return "ETag changed"
    if seen.last_modified and last_modified is not None and last_modified != seen.last_modified:
        return "Last-Modified changed"
    return ""


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
                 state_file: Path, max_concurrent: int = 2, notify=None):
        self._client = client
        self._complete_dir = complete_dir
        self._incomplete_dir = incomplete_dir
        self._state_file = state_file
        self._max_concurrent = max(1, int(max_concurrent))
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        # Optional async callback (title, body) for failure notifications.
        self._notify = notify

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
        except (WebshareError, httpx.HTTPError, DownloadIntegrityError, OSError) as exc:
            job.status = "failed"
            job.error = describe_error(exc)
            job.completed_ts = time.time()
            # Keep the partial file so a retry can resume; delete() cleans it up.
            logger.error("Download %s failed: %s", job.nzo_id, job.error,
                         exc_info=not str(exc))  # no text: the traceback is all there is
            if self._notify:
                try:
                    await self._notify("Websharr: download failed", f"{job.job_name}\n{exc}")
                except Exception:  # notifications must never break the manager
                    logger.debug("failure notification errored", exc_info=True)
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
        # No retry on a link error: Webshare's "File temporarily unavailable" does
        # not recover in practice. Failing right away frees the slot and lets *arr
        # blocklist the release (see torznab._pub_date) and grab another one.
        url = await self._client.file_link(job.ident)

        work_dir = self._incomplete_path(job)
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / job.name
        # Sidecar next to the partial file; job names never start with a dot
        # (sanitize_filename), and only `target` is moved to the complete dir.
        seen = _Validators.load(work_dir / RESUME_FILE)

        # Size announced by the NZB (exact Webshare bytes). A response never
        # overrides it; only when the NZB had none does the first one set it.
        expected = job.size
        offset = target.stat().st_size if target.exists() else 0

        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as http:
            interruptions = 0
            while True:
                if offset and (not seen.if_range or (expected and offset >= expected)):
                    # Nothing proves the partial file is part of the remote one
                    # (no validator from its first response), or it can't be a
                    # prefix of it: appending could splice two different files.
                    logger.info("Partial file of %s can't be resumed safely; starting over", job.nzo_id)
                    offset = self._restart(job, target, seen)
                headers = {"Range": f"bytes={offset}-", "If-Range": seen.if_range} if offset else {}
                try:
                    async with http.stream("GET", url, headers=headers) as resp:
                        if offset and resp.status_code == 416:
                            # Partial file no longer matches the remote; start over.
                            offset = self._restart(job, target, seen)
                            continue
                        if offset and resp.status_code == 206:
                            problem = _resume_problem(seen, resp, offset, expected)
                            if problem:
                                # Never append what may be another file or another
                                # part of it; download the whole file again.
                                logger.warning("Download %s can't resume at %d bytes (%s); starting over",
                                               job.nzo_id, offset, problem)
                                offset = self._restart(job, target, seen)
                                continue
                            if not expected:
                                expected = job.size = _content_range(resp)[2]
                        elif offset:
                            # Server ignored the Range header, or If-Range saw a
                            # changed file: this is the whole file from byte 0.
                            offset = 0
                        resp.raise_for_status()
                        if not offset:
                            if resp.status_code != 200:
                                raise DownloadIntegrityError(
                                    f"unexpected HTTP {resp.status_code} for the whole file")
                            length = _content_length(resp)
                            if expected and length is not None and length != expected:
                                raise DownloadIntegrityError(
                                    f"server sends {length} bytes, expected {expected}")
                            if not expected and length:
                                expected = job.size = length
                            seen.save(resp)  # a later resume (pause, restart) checks them
                        else:
                            logger.info("Resuming %s from %d bytes", job.nzo_id, offset)
                        await self._stream_to_file(job, resp, target, offset, expected)
                    received = target.stat().st_size
                    if expected and received < expected:
                        raise httpx.RemoteProtocolError(
                            f"stream ended at {received} of {expected} bytes")
                except httpx.TransportError as exc:
                    # A dropped connection mid-file ("peer closed connection without
                    # sending complete message body") is common on multi-GB files.
                    # Failing it made *arr blocklist a good release and throw the
                    # partial file away; continue from what is on disk instead, with
                    # a fresh link (the old one may have expired).
                    interruptions += 1
                    if interruptions > RESUME_ATTEMPTS:
                        raise
                    offset = target.stat().st_size if target.exists() else 0
                    logger.warning("Download %s interrupted at %d bytes (%s); resuming, attempt %d/%d",
                                   job.nzo_id, offset, exc, interruptions, RESUME_ATTEMPTS)
                    await asyncio.sleep(RESUME_DELAYS[min(interruptions, len(RESUME_DELAYS)) - 1])
                    url = await self._client.file_link(job.ident)
                    continue
                break

        received = target.stat().st_size
        if expected and received != expected:
            raise DownloadIntegrityError(f"downloaded {received} bytes, expected {expected}")
        final_dir = self._complete_dir / job.category / job.job_name
        final_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), final_dir / job.name)
        shutil.rmtree(work_dir, ignore_errors=True)
        job.storage = str(final_dir)
        job.size = job.size or received

    @staticmethod
    def _restart(job: Job, target: Path, seen: _Validators) -> int:
        """Drop the partial file and its validators; the download starts at 0."""
        target.unlink(missing_ok=True)
        seen.clear()
        job.downloaded = 0
        return 0

    @staticmethod
    async def _stream_to_file(job: Job, resp: httpx.Response, target: Path, offset: int,
                              expected: int) -> None:
        job.downloaded = offset
        last_t = time.monotonic()
        last_bytes = offset
        with target.open("ab" if offset else "wb") as fh:
            async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                if expected and job.downloaded + len(chunk) > expected:
                    # Body without (or beyond) a Content-Length: never let the
                    # file grow past the size the job announced.
                    raise DownloadIntegrityError(f"server sent more than the expected {expected} bytes")
                fh.write(chunk)
                job.downloaded += len(chunk)
                now = time.monotonic()
                if now - last_t >= 2.0:
                    job.speed = (job.downloaded - last_bytes) / (now - last_t)
                    last_t = now
                    last_bytes = job.downloaded
