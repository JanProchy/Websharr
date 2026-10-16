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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import httpx

from .nzb import sanitize_filename
from .webshare import WebshareClient, WebshareError

logger = logging.getLogger("websharr.downloads")

CHUNK_SIZE = 1024 * 1024
HISTORY_CAP = 500  # keep this many completed/failed records in the UI history


def describe_error(exc: BaseException) -> str:
    """Error text for the history and *arr's fail message. Several httpx errors
    (ReadTimeout, PoolTimeout…) carry no message, which left "failed: " with
    nothing to go on; then the exception type is said instead."""
    return str(exc).strip() or type(exc).__name__


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


# Duplicate fallback (opt-in, settings.duplicate_fallback): a grab may name up
# to five copies of its file (torznab.find_copies) to download instead when the
# file itself is gone. They are kept in a sidecar in the job's incomplete
# folder, never on the Job: an older Websharr skips a state.json record with a
# field it doesn't know, so a new field would lose the queue and history on a
# rollback. The folder goes away with the job (completion, delete), and the
# grab itself — ident, nzo_id, name — never changes; a substitution is logged.
FALLBACK_FILE = ".websharr-fallback.json"  # job names never start with a dot
MAX_COPIES = 5
COPY_LENGTH_TOLERANCE = 1  # s
_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # also safe as a file name part

# The retry/error contract of the fallback. A copy is tried only when the
# grabbed file itself is gone: /file_link/ says so (allowlist below), or the
# download answers 404/410 before a byte is written. Everything else — login,
# VIP, credit, rate limits (HTTP 403/429), network or server errors, unknown
# messages — is not the file's fault: the job fails as without copies.
_LINK_GONE = re.compile(r"temporarily unavailable|not found|deleted|removed|does not exist", re.I)
_ACCOUNT = re.compile(r"login|logged|password|credential|vip|credit|quota|limit|premium|token", re.I)
_GONE_STATUS = (404, 410)


def link_gone(exc: BaseException) -> bool:
    """True when file_link failed because this one file is unavailable, not
    because of the account or the connection (see the contract above)."""
    text = str(exc) if isinstance(exc, WebshareError) else ""
    if "/file_link/" not in text:
        return False  # login/salt errors, an empty link, HTTP errors of the API
    reason = text.split("/file_link/", 1)[1]
    return bool(_LINK_GONE.search(reason)) and not _ACCOUNT.search(reason)


class _Gone(Exception):
    """The file being tried is gone (per-file error); its cause says why."""


class _CopyRejected(Exception):
    """A copy failed its re-check; it is skipped without writing a byte."""


def _has_bytes(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


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


_JOB_FIELDS = {f.name for f in fields(Job)}


class DownloadManager:
    def __init__(self, client: WebshareClient, complete_dir: Path, incomplete_dir: Path,
                 state_file: Path, max_concurrent: int = 2, notify=None,
                 fallback: Callable[[], bool] | None = None):
        self._client = client
        # Whether the duplicate fallback is on (read at grab and download time).
        self._fallback = fallback or (lambda: False)
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
            # Fields this version doesn't know (a newer or experimental build's,
            # e.g. the `alternates` an early duplicate-fallback draft wrote) are
            # dropped rather than losing the whole job.
            item = {k: v for k, v in item.items() if k in _JOB_FIELDS}
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

    def add(self, ident: str, name: str, size: int, category: str, title: str = "",
            alternates: list[str] | None = None, length: int = 0) -> Job:
        job = Job(
            nzo_id=f"SABnzbd_nzo_{uuid.uuid4().hex[:12]}",
            ident=ident,
            name=sanitize_filename(name),
            category=category or "*",
            size=size,
            title=sanitize_filename(title) if title else "",
        )
        # Copies are only usable with the size and length they were matched on.
        copies = list(dict.fromkeys(
            a for a in alternates or [] if a != ident and _IDENT_RE.match(a)))[:MAX_COPIES]
        if copies and size > 0 and length > 0 and self._fallback():
            self._write_fallback(job, {"ident": ident, "size": size, "length": length,
                                       "alternates": copies})
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
        work_dir = self._incomplete_path(job)
        target = work_dir / job.name
        fallback = self._read_fallback(job) if self._fallback() else None
        if fallback is None:
            # No retry on a link error: Webshare's "File temporarily unavailable"
            # does not recover in practice. Failing right away frees the slot and
            # lets *arr blocklist the release (see torznab._pub_date) and grab
            # another one.
            url = await self._client.file_link(job.ident)
            work_dir.mkdir(parents=True, exist_ok=True)
            await self._fetch(job, url, target)
        else:
            target = await self._download_or_copy(job, fallback, target)

        final_dir = self._complete_dir / job.category / job.job_name
        final_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), final_dir / job.name)
        shutil.rmtree(work_dir, ignore_errors=True)
        job.storage = str(final_dir)
        job.size = max(job.size, job.downloaded)

    async def _fetch(self, job: Job, url: str, target: Path, expect_size: int = 0) -> None:
        """Download `url` into `target`, resuming what it already holds. With
        `expect_size`, a response that doesn't announce exactly that size is
        refused (_CopyRejected) before a byte is written."""
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
                    if expect_size and total != expect_size:
                        raise _CopyRejected(f"download is {total} bytes, expected {expect_size}")
                    if total:
                        job.size = total
                    if offset:
                        logger.info("Resuming %s from %d bytes", job.nzo_id, offset)
                    await self._stream_to_file(job, resp, target, offset)
                break

    # -- duplicate fallback (see FALLBACK_FILE) ------------------------------

    def _fallback_path(self, job: Job) -> Path:
        return self._incomplete_path(job) / FALLBACK_FILE

    def _write_fallback(self, job: Job, data: dict) -> None:
        path = self._fallback_path(job)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Could not write the copies of %s: %s", job.nzo_id, exc)

    def _read_fallback(self, job: Job) -> dict | None:
        """The job's copies, or None when it has none (or they don't fit it)."""
        try:
            data = json.loads(self._fallback_path(job).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("ident") != job.ident:
            return None
        copies = data.get("alternates")
        if not (isinstance(copies, list) and copies
                and all(isinstance(a, str) and _IDENT_RE.match(a) for a in copies)):
            return None
        if not all(isinstance(data.get(k), int) and data[k] > 0 for k in ("size", "length")):
            return None
        return data

    @staticmethod
    def _copy_part(work_dir: Path, ident: str) -> Path:
        # A copy's bytes never go into the job's own file name, so neither this
        # code nor an older Websharr can resume one upload with another's bytes.
        return work_dir / f".websharr-copy-{ident}.part"

    async def _download_or_copy(self, job: Job, fallback: dict, target: Path) -> Path:
        """Download the grabbed file, or — when it is gone and nothing is on
        disk yet — the first copy that passes a re-check. Each copy is tried at
        most once per attempt. Returns the file holding the download."""
        work_dir = target.parent
        # Bytes on disk are only ever continued from the upload they came from.
        if _has_bytes(target):
            url = await self._client.file_link(job.ident)
            await self._fetch(job, url, target)
            return target
        using = fallback.get("using", "")
        if using and _has_bytes(part := self._copy_part(work_dir, using)):
            url = await self._client.file_link(using)
            try:
                await self._fetch(job, url, part, expect_size=fallback["size"])
            except _CopyRejected as exc:
                raise WebshareError(f"Copy {using} no longer matches: {exc}") from exc
            return part

        first: BaseException | None = None  # why the grabbed file is gone
        copies = fallback["alternates"]
        for ident in (job.ident, *copies):
            is_copy = ident != job.ident
            dest = self._copy_part(work_dir, ident) if is_copy else target
            try:
                if is_copy:
                    await self._check_copy(ident, fallback)
                try:
                    url = await self._client.file_link(ident)
                except WebshareError as exc:
                    if not link_gone(exc):
                        raise  # the account or the connection, not the file
                    raise _Gone() from exc
                work_dir.mkdir(parents=True, exist_ok=True)
                if is_copy:
                    # Before the first byte, so a restart resumes this copy only.
                    self._write_fallback(job, {**fallback, "using": ident})
                try:
                    await self._fetch(job, url, dest, expect_size=fallback["size"] if is_copy else 0)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in _GONE_STATUS or _has_bytes(dest):
                        raise
                    raise _Gone() from exc
            except _Gone as gone:
                reason = describe_error(gone.__cause__)
                logger.warning("%s: %s %s is gone (%s)", job.nzo_id,
                               "copy" if is_copy else "grabbed file", ident, reason)
                first = first or gone.__cause__
                continue
            except _CopyRejected as exc:
                logger.warning("%s: copy %s not used: %s", job.nzo_id, ident, exc)
                continue
            if is_copy:
                logger.warning("%s: grabbed file %s is gone (%s); downloaded identical copy %s "
                               "instead", job.nzo_id, job.ident, describe_error(first), ident)
            return dest
        raise WebshareError(f"{describe_error(first)} ({len(copies)} identical copies tried)") from first

    async def _check_copy(self, ident: str, fallback: dict) -> None:
        """Re-check a copy right before using it: its length must still be the
        one it was matched on, and its size (when file_info reports one) the
        grab's. The download's announced size is checked again in _fetch."""
        try:
            info = await self._client.file_info(ident)
        except (WebshareError, httpx.HTTPError) as exc:
            raise _CopyRejected(f"file_info failed: {describe_error(exc)}") from exc
        length = int(info.get("length") or 0)
        if not length or abs(length - fallback["length"]) > COPY_LENGTH_TOLERANCE:
            raise _CopyRejected(f"length {length} s, expected {fallback['length']} s")
        size = int(info.get("size") or 0)
        if size and size != fallback["size"]:
            raise _CopyRejected(f"size {size} bytes, expected {fallback['size']}")

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
