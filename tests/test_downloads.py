"""Download manager: resume after restart (HTTP Range) and retry of failed jobs."""

import dataclasses
import http.server
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.downloads as downloads_module
import app.main as app_main
from app.config import config
from app.main import app
from app.nzb import build_nzb
from app.webshare import WebshareError

from .conftest import FakeWebshareClient, wait_for

PAYLOAD = b"websharr" * 50_000  # ~400 kB
FILE_NAME = "Resumed.Movie.2024.mkv"
# Validators a static file server sends; a partial file is only resumed when
# the server still reports the ones it was downloaded with.
ETAG = '"websharr-v1"'
LAST_MODIFIED = "Wed, 30 Sep 2026 10:00:00 GMT"
CHANGED = "Thu, 01 Oct 2026 10:00:00 GMT"  # Last-Modified after the file changed


def _serve(payload: bytes, support_range: bool):
    """HTTP server for `payload`; records Range headers it receives."""

    class Handler(http.server.BaseHTTPRequestHandler):
        ranges: list = []

        def do_GET(self):
            rng = self.headers.get("Range")
            Handler.ranges.append(rng)
            data = payload
            if rng and support_range:
                start = int(rng.removeprefix("bytes=").split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                data = data[start:]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", ETAG)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, Handler


def _serve_slow(payload: bytes, chunk: int = 8192, delay: float = 0.03):
    """Throttled server (supports Range) so a download can be paused mid-flight."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            rng = self.headers.get("Range")
            data = payload
            if rng:
                start = int(rng.removeprefix("bytes=").split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                data = data[start:]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", ETAG)
            self.end_headers()
            for i in range(0, len(data), chunk):
                try:
                    self.wfile.write(data[i:i + chunk])
                    self.wfile.flush()
                    time.sleep(delay)
                except (BrokenPipeError, ConnectionResetError):
                    return

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_pause_resume(tmp_path, monkeypatch):
    # Larger than CHUNK_SIZE (1 MiB) so the stream yields (and updates progress)
    # several times mid-download, giving a window to pause.
    payload = b"z" * (3 * 1024 * 1024)
    httpd = _serve_slow(payload, chunk=65536, delay=0.03)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.mkv"
        with _start_app(tmp_path, monkeypatch, url) as client:
            nzb = build_nzb("pz1", "Zaklinac.raw.mkv", len(payload))
            nzo_id = client.post(
                "/sabnzbd/api",
                params={"mode": "addfile", "apikey": "testkey", "cat": "tv"},
                files={"nzbfile": ("Zaklinac S01E01.nzb", nzb.encode(), "application/x-nzb")},
            ).json()["nzo_ids"][0]
            manager = app.state.downloads

            # Wait until it is actively downloading (some bytes in), then pause.
            assert wait_for(lambda: manager.get(nzo_id).status == "downloading"
                            and manager.get(nzo_id).downloaded > 0)
            ok = client.get("/sabnzbd/api", params={
                "mode": "queue", "name": "pause", "value": nzo_id,
                "apikey": "testkey"}).json()["status"]
            assert ok is True
            assert wait_for(lambda: manager.get(nzo_id).status == "paused")
            job = manager.get(nzo_id)
            assert 0 < job.downloaded < len(payload)  # partial, not finished
            assert job in manager.queue_jobs()

            client.get("/sabnzbd/api", params={
                "mode": "queue", "name": "resume", "value": nzo_id, "apikey": "testkey"})
            assert wait_for(lambda: manager.get(nzo_id).status == "completed", timeout=10)
            job = manager.get(nzo_id)

        assert (Path(job.storage) / "Zaklinac.raw.mkv").read_bytes() == payload
    finally:
        httpd.shutdown()


def _seed_interrupted_job(tmp_path, partial: bytes, sidecar: str | None = json.dumps({"etag": ETAG})) -> str:
    """Write a state file + partial file (+ its validator sidecar) as if a
    download was cut off mid-way."""
    nzo_id = "SABnzbd_nzo_resume01"
    state = {"jobs": [{
        "nzo_id": nzo_id, "ident": "res1", "name": FILE_NAME,
        "category": "movies", "size": len(PAYLOAD), "status": "downloading",
        "downloaded": len(partial), "error": "", "storage": "",
        "added_ts": 1.0, "completed_ts": 0.0,
    }]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    work = tmp_path / "incomplete" / nzo_id
    work.mkdir(parents=True)
    (work / FILE_NAME).write_bytes(partial)
    if sidecar is not None:
        (work / downloads_module.RESUME_FILE).write_text(sidecar)
    return nzo_id


def _start_app(tmp_path, monkeypatch, file_link_url: str) -> TestClient:
    monkeypatch.setattr(config, "api_key", "testkey")
    monkeypatch.setattr(config, "complete_dir", tmp_path / "complete")
    monkeypatch.setattr(config, "incomplete_dir", tmp_path / "incomplete")
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "settings_file", tmp_path / "settings.json")

    def factory(username, password, password_digest=""):
        fake = FakeWebshareClient(username, password, password_digest)
        fake.file_link_url = file_link_url
        return fake

    monkeypatch.setattr(app_main, "WebshareClient", factory)
    return TestClient(app)


@pytest.mark.parametrize("support_range", [True, False])
def test_restart_resumes_interrupted_download(tmp_path, monkeypatch, support_range):
    partial = PAYLOAD[:120_000]
    nzo_id = _seed_interrupted_job(tmp_path, partial)
    httpd, handler = _serve(PAYLOAD, support_range)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/{FILE_NAME}"
        with _start_app(tmp_path, monkeypatch, url):
            manager = app.state.downloads
            assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "completed")
            job = manager.get(nzo_id)

        final = Path(job.storage) / FILE_NAME
        assert final.read_bytes() == PAYLOAD
        assert f"bytes={len(partial)}-" in handler.ranges
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("sidecar", [None, "", "{not json", "[]", '{"etag": 5}', json.dumps({"etag": ""})],
                         ids=["missing", "empty", "corrupt", "list", "wrong-type", "no-validator"])
def test_restart_does_not_trust_an_unverifiable_partial_file(tmp_path, monkeypatch, sidecar):
    """A partial file without readable validators of its first response (e.g.
    left by an older version) can't be tied to the remote file: download it
    again instead of appending to it - and never crash on the sidecar."""
    nzo_id = _seed_interrupted_job(tmp_path, b"x" * 120_000, sidecar=sidecar)
    httpd, handler = _serve(PAYLOAD, support_range=True)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/{FILE_NAME}"
        with _start_app(tmp_path, monkeypatch, url):
            manager = app.state.downloads
            assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "completed")
            job = manager.get(nzo_id)

        assert (Path(job.storage) / FILE_NAME).read_bytes() == PAYLOAD
        assert handler.ranges == [None]
    finally:
        httpd.shutdown()


def _serve_dropping(payload: bytes, drop_at: int, drops: int = 1):
    """Range-capable server that closes the connection after `drop_at` bytes of
    the body on its first `drops` responses (Content-Length promises it all)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        ranges: list = []
        served = 0

        def do_GET(self):
            rng = self.headers.get("Range")
            Handler.ranges.append(rng)
            start = int(rng.removeprefix("bytes=").split("-")[0]) if rng else 0
            data = payload[start:]
            self.send_response(206 if rng else 200)
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", ETAG)
            self.end_headers()
            Handler.served += 1
            if Handler.served <= drops:
                self.wfile.write(data[:drop_at])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, Handler


def test_dropped_connection_resumes_instead_of_failing(client, fake_webshare, monkeypatch):
    """"peer closed connection without sending complete message body" mid-file:
    continue from the partial file with a fresh link instead of failing the job
    (which made *arr blocklist a good release)."""
    # bigger than the 1 MiB write chunk, so the drops leave real bytes on disk
    payload = bytes(range(256)) * 16_384  # 4 MiB
    httpd, handler = _serve_dropping(payload, drop_at=1_500_000, drops=2)
    try:
        fake_webshare.file_link_url = f"http://127.0.0.1:{httpd.server_address[1]}/{FILE_NAME}"
        nzb = build_nzb("drop1", FILE_NAME, len(payload))
        resp = client.post("/sabnzbd/api", params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
                           files={"nzbfile": ("x.nzb", nzb.encode(), "application/x-nzb")})
        nzo_id = resp.json()["nzo_ids"][0]
        manager = app.state.downloads
        assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "completed")
        job = manager.get(nzo_id)
        (final,) = Path(job.storage).iterdir()  # one finished file, whatever it is named
        assert final.read_bytes() == payload
        # continued from what was on disk each time, not from zero
        assert handler.ranges[0] is None and len(handler.ranges) == 3
        assert all(r and r.startswith("bytes=") and r != "bytes=0-" for r in handler.ranges[1:])
    finally:
        httpd.shutdown()


def test_dropped_connection_fails_after_the_attempts(client, fake_webshare, monkeypatch):
    monkeypatch.setattr(downloads_module, "RESUME_ATTEMPTS", 2)
    httpd, handler = _serve_dropping(PAYLOAD, drop_at=10_000, drops=99)
    try:
        fake_webshare.file_link_url = f"http://127.0.0.1:{httpd.server_address[1]}/{FILE_NAME}"
        nzb = build_nzb("drop2", FILE_NAME, len(PAYLOAD))
        resp = client.post("/sabnzbd/api", params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
                           files={"nzbfile": ("x.nzb", nzb.encode(), "application/x-nzb")})
        nzo_id = resp.json()["nzo_ids"][0]
        manager = app.state.downloads
        assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "failed")
        assert len(handler.ranges) == 3  # first try + 2 resumes
    finally:
        httpd.shutdown()


# -- fault injection: a resume must never splice the wrong bytes together ----

SMALL = bytes(range(64))  # the file the NZB announces (64 bytes)
OTHER = bytes(range(100, 164))  # same size, different content
CUT = 16  # bytes saved before the first connection drops


def _entity(payload: bytes, last_modified: str | None = LAST_MODIFIED, etag: str | None = None,
            cut: int | None = None, honor_if_range: bool = True, content_length: bool = True,
            validators_on_206: bool = False):
    """Step for _serve_script: answer like Webshare's nginx by default - a 200
    with Last-Modified but no ETag, a 206 from the requested offset carrying no
    validator, a 200 instead when If-Range names another version, 416 past the
    end. `cut` drops the connection after that many body bytes."""

    def step(start, if_range):
        headers = {} if content_length else {"Content-Length": None}
        validators = {"ETag": etag, "Last-Modified": last_modified}
        if start is None or (honor_if_range and if_range and if_range not in (etag, last_modified)):
            return 200, {**headers, **validators}, payload, cut
        if start >= len(payload):
            return 416, {"Content-Range": f"bytes */{len(payload)}"}, b"", None
        if validators_on_206:
            headers.update(validators)
        headers["Content-Range"] = f"bytes {start}-{len(payload) - 1}/{len(payload)}"
        return 206, headers, payload[start:], cut

    return step


def _partial(content_range: str | None, body: bytes, last_modified: str | None = None,
             etag: str | None = None):
    """Step for _serve_script: a 206 with the given Content-Range and body."""

    def step(start, if_range):
        headers = {"ETag": etag, "Last-Modified": last_modified}
        if content_range is not None:
            headers["Content-Range"] = content_range
        return 206, headers, body, None

    return step


def _serve_script(*steps):
    """Fault-injection server: request N is answered by steps[N] (the last one
    repeats). A step gets (Range start or None, If-Range) and returns (status,
    headers, body, cut). Records (Range, If-Range) of every request."""

    class Handler(http.server.BaseHTTPRequestHandler):
        requests: list = []

        def do_GET(self):
            rng, if_range = self.headers.get("Range"), self.headers.get("If-Range")
            Handler.requests.append((rng, if_range))
            start = int(rng.removeprefix("bytes=").split("-")[0]) if rng else None
            step = steps[min(len(Handler.requests), len(steps)) - 1]
            status, headers, body, cut = step(start, if_range)
            self.send_response(status)
            for key, value in {"Content-Length": str(len(body)), **headers}.items():
                if value is not None:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body if cut is None else body[:cut])
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, Handler


@pytest.fixture
def faulty(client, fake_webshare, monkeypatch):
    """Run a 64-byte job against a _serve_script server; returns the job.
    Small write chunks so a dropped connection leaves exactly CUT bytes."""
    monkeypatch.setattr(downloads_module, "CHUNK_SIZE", CUT)
    servers = []

    def run(*steps, until=("completed", "failed")):
        httpd, handler = _serve_script(*steps)
        servers.append(httpd)
        fake_webshare.file_link_url = f"http://127.0.0.1:{httpd.server_address[1]}/{FILE_NAME}"
        nzb = build_nzb("fault1", FILE_NAME, len(SMALL))
        nzo_id = client.post("/sabnzbd/api", params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
                             files={"nzbfile": ("x.nzb", nzb.encode(), "application/x-nzb")}).json()["nzo_ids"][0]
        manager = app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status in until)
        return manager.get(nzo_id), handler.requests

    yield run
    for httpd in servers:
        httpd.shutdown()


def _finished(job) -> bytes:
    (final,) = Path(job.storage).iterdir()
    return final.read_bytes()


@pytest.mark.parametrize("first, resume, if_range", [
    # Webshare: Last-Modified on the 200, no validator at all on the 206 - the
    # 206 to an If-Range request is itself the proof that the file is the same
    ({}, {}, LAST_MODIFIED),
    ({}, {"validators_on_206": True}, LAST_MODIFIED),
    ({"etag": ETAG}, {"etag": ETAG}, ETAG),  # a strong ETag is preferred
    ({"etag": ETAG}, {"etag": ETAG, "validators_on_206": True}, ETAG),
], ids=["206-without-validator", "206-repeats-last-modified", "etag-206-without-validator",
        "etag-206-repeats-it"])
def test_resume_appends_a_verified_range(faulty, tmp_path, first, resume, if_range):
    job, requests = faulty(_entity(SMALL, cut=CUT, **first), _entity(SMALL, **resume))
    assert job.status == "completed" and _finished(job) == SMALL  # sidecar not moved along
    # continued from the saved prefix, conditional on the same file
    assert requests == [(None, None), (f"bytes={CUT}-", if_range)]
    assert not any((tmp_path / "complete").rglob(downloads_module.RESUME_FILE))
    assert not (tmp_path / "incomplete" / job.nzo_id).exists()


@pytest.mark.parametrize("content_range, body", [
    ("bytes 0-63/64", SMALL),  # whole file instead of the suffix
    ("bytes 8-63/64", SMALL[8:]),  # starts before the saved prefix ends
    (f"bytes {CUT}-31/64", SMALL[CUT:32]),  # stops short of the end
    (f"bytes {CUT}-79/80", (SMALL + OTHER)[CUT:80]),  # a bigger file
    (f"items {CUT}-63/64", SMALL[CUT:]),  # not bytes
    (f"bytes {CUT}-63/*", SMALL[CUT:]),  # unknown total
    (None, SMALL[CUT:]),  # no Content-Range at all
], ids=["wrong-start", "early-start", "short-end", "wrong-total", "unit", "no-total", "missing"])
def test_mismatched_206_restarts_instead_of_appending(faulty, content_range, body):
    """Reproduced in review: after 16 of 64 bytes, `Range: bytes=16-` got a 206
    `bytes 0-63/64` with the whole body; appending it made an 80-byte file that
    was marked Completed. A range that doesn't continue the partial file is
    discarded and the file downloaded again from byte 0."""
    job, requests = faulty(_entity(SMALL, cut=CUT), _partial(content_range, body), _entity(SMALL))
    assert job.status == "completed" and _finished(job) == SMALL
    assert requests[1] == (f"bytes={CUT}-", LAST_MODIFIED)
    assert requests[2] == (None, None)  # started over, nothing appended


@pytest.mark.parametrize("steps, if_range", [
    # If-Range honoured: the changed file comes back whole as a 200
    ((_entity(SMALL, cut=CUT), _entity(OTHER, last_modified=CHANGED)), LAST_MODIFIED),
    # If-Range ignored, but the 206 carries a validator that differs
    ((_entity(SMALL, cut=CUT),
      _entity(OTHER, last_modified=CHANGED, honor_if_range=False, validators_on_206=True)), LAST_MODIFIED),
    ((_entity(SMALL, cut=CUT, etag=ETAG),
      _entity(OTHER, etag='"websharr-v2"', honor_if_range=False, validators_on_206=True)), ETAG),
    ((_entity(SMALL, cut=CUT), _partial(f"bytes {CUT}-63/64", OTHER[CUT:], last_modified=CHANGED),
      _entity(OTHER, last_modified=CHANGED)), LAST_MODIFIED),
], ids=["if-range-200", "changed-last-modified-206", "changed-etag-206", "changed-partial"])
def test_changed_remote_file_is_not_spliced(faulty, steps, if_range):
    """Reproduced in review: the refreshed link served a correctly positioned
    suffix of a different file (other ETag); saved prefix + new suffix made a
    64-byte Completed file matching neither. The result must be one file."""
    job, requests = faulty(*steps)
    assert job.status == "completed"
    assert _finished(job) == OTHER  # the new file whole, not SMALL[:16] + OTHER[16:]
    assert requests[1] == (f"bytes={CUT}-", if_range)


def test_resume_without_validators_starts_over(faulty):
    """Without ETag/Last-Modified there is nothing to send as If-Range, so
    nothing proves the partial file is part of what the server sends now: it
    is never appended to."""
    job, requests = faulty(_entity(SMALL, last_modified=None, cut=CUT), _entity(SMALL, last_modified=None))
    assert job.status == "completed" and _finished(job) == SMALL
    assert requests == [(None, None), (None, None)]


def test_resume_ignored_range_restarts_from_zero(faulty):
    """A 200 to a Range request is the whole file: it replaces the partial file."""
    ignore_range = _entity(SMALL)
    job, requests = faulty(_entity(SMALL, cut=CUT), lambda start, if_range: ignore_range(None, None))
    assert job.status == "completed" and _finished(job) == SMALL
    assert requests[1] == (f"bytes={CUT}-", LAST_MODIFIED)


def test_resume_416_restarts_from_zero(faulty):
    gone = lambda start, if_range: (416, {"Content-Range": "bytes */64"}, b"", None)  # noqa: E731
    job, requests = faulty(_entity(SMALL, cut=CUT), gone, _entity(SMALL))
    assert job.status == "completed" and _finished(job) == SMALL
    assert requests[1:] == [(f"bytes={CUT}-", LAST_MODIFIED), (None, None)]


def test_short_close_delimited_body_resumes(faulty):
    """A body without Content-Length that ends early isn't complete just because
    the connection closed cleanly: the NZB said 64 bytes."""
    job, requests = faulty(_entity(SMALL, cut=48, content_length=False), _entity(SMALL))
    assert job.status == "completed" and _finished(job) == SMALL
    assert requests[1] == ("bytes=48-", LAST_MODIFIED)


@pytest.mark.parametrize("steps", [
    (_entity(SMALL + OTHER),),  # Content-Length bigger than announced
    (_entity(SMALL[:48]),),  # smaller (used to replace the announced size)
    (_entity(SMALL + OTHER, content_length=False),),  # no length, too much data
    (_entity(SMALL, cut=CUT), _partial(f"bytes {CUT}-79/80", (SMALL + OTHER)[CUT:80]),
     _entity(SMALL + OTHER[:16])),  # resume reveals a bigger file
], ids=["bigger", "smaller", "close-delimited", "after-resume"])
def test_wrong_size_never_completes(faulty, tmp_path, steps):
    """The size from the NZB is the expectation: a response never overrides it,
    and a file of any other size fails instead of being marked Completed."""
    job, _ = faulty(*steps)
    assert job.status == "failed"
    assert "expected 64" in job.error
    assert job.size == len(SMALL)
    assert not any((tmp_path / "complete").rglob(FILE_NAME))


def test_resume_gives_up_after_the_attempts(faulty, monkeypatch):
    monkeypatch.setattr(downloads_module, "RESUME_ATTEMPTS", 2)
    job, requests = faulty(lambda start, if_range: _entity(SMALL, cut=CUT)(start, if_range))
    assert job.status == "failed"
    # first try + 2 verified resumes, each continuing from what was on disk
    assert requests == [(None, None), (f"bytes={CUT}-", LAST_MODIFIED), (f"bytes={2 * CUT}-", LAST_MODIFIED)]
    assert not any(Path(job.storage or "/nonexistent").glob("*"))


@pytest.mark.parametrize("action", ["pause", "delete"])
def test_cancel_while_waiting_to_resume(faulty, client, monkeypatch, tmp_path, action):
    """Pausing (or deleting) a job that waits to resume a dropped connection
    stops it at once; a manual resume then continues the verified prefix."""
    monkeypatch.setattr(downloads_module, "RESUME_DELAYS", (30,))
    job, requests = faulty(_entity(SMALL, cut=CUT), _entity(SMALL), until=("downloading",))
    manager = app.state.downloads
    partial = tmp_path / "incomplete" / job.nzo_id / FILE_NAME
    assert wait_for(lambda: len(requests) == 1 and partial.exists() and partial.stat().st_size == CUT)

    def queue(name):
        return client.get("/sabnzbd/api", params={"mode": "queue", "name": name, "value": job.nzo_id,
                                                  "del_files": "1", "apikey": "testkey"}).json()["status"]

    if action == "delete":
        assert queue("delete") is True
        assert wait_for(lambda: not partial.parent.exists())
        time.sleep(0.2)
        assert manager.get(job.nzo_id) is None and len(requests) == 1
        return
    assert queue("pause") is True
    assert wait_for(lambda: job.nzo_id not in manager._tasks)
    assert job.status == "paused" and partial.stat().st_size == CUT and len(requests) == 1
    sidecar = partial.parent / downloads_module.RESUME_FILE
    assert json.loads(sidecar.read_text())["last_modified"] == LAST_MODIFIED

    assert queue("resume") is True
    assert wait_for(lambda: job.status == "completed")
    assert _finished(job) == SMALL
    assert requests[1] == (f"bytes={CUT}-", LAST_MODIFIED)
    assert not partial.parent.exists()  # sidecar removed with the work dir


# Job fields as of upstream/main. The persisted job records must not gain keys:
# an older version's load_state() does Job(**record) and skips any record with
# an unknown field, so after a rollback every job written by this one vanished.
UPSTREAM_JOB_FIELDS = {
    "nzo_id", "ident", "name", "category", "size", "title", "status", "downloaded",
    "speed", "error", "storage", "added_ts", "completed_ts", "sab_hidden",
}


def test_state_file_stays_loadable_by_older_versions(faulty, client, monkeypatch, tmp_path):
    """Resume validators live in a sidecar in the work dir, not in state.json."""
    monkeypatch.setattr(downloads_module, "RESUME_DELAYS", (30,))
    job, requests = faulty(_entity(SMALL, cut=CUT), _entity(SMALL), until=("downloading",))
    sidecar = tmp_path / "incomplete" / job.nzo_id / downloads_module.RESUME_FILE
    assert wait_for(sidecar.exists)
    client.get("/sabnzbd/api", params={"mode": "queue", "name": "pause", "value": job.nzo_id,
                                       "apikey": "testkey"})
    assert wait_for(lambda: job.status == "paused")

    assert {f.name for f in dataclasses.fields(downloads_module.Job)} == UPSTREAM_JOB_FIELDS
    old_job = dataclasses.make_dataclass("OldJob", sorted(UPSTREAM_JOB_FIELDS))
    records = json.loads((tmp_path / "state.json").read_text())["jobs"]
    assert records and all(set(r) <= UPSTREAM_JOB_FIELDS for r in records)
    for record in records:
        old_job(**record)  # what an older load_state() does; must not raise


def test_retry_failed_job(client, fake_webshare, tmp_path):
    payload = b"retry-payload" * 1000
    # First attempt fails: the default fake link is unreachable.
    nzb = build_nzb("rty1", "Retry.Me.2024.mkv", len(payload))
    resp = client.post(
        "/sabnzbd/api",
        params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
        files={"nzbfile": ("x.nzb", nzb.encode(), "application/x-nzb")},
    )
    nzo_id = resp.json()["nzo_ids"][0]
    manager = app.state.downloads
    assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "failed")

    # Fix the link, retry via the API, and the job completes.
    httpd, _ = _serve(payload, support_range=True)
    try:
        fake_webshare.file_link_url = f"http://127.0.0.1:{httpd.server_address[1]}/f.mkv"
        resp = client.get("/sabnzbd/api", params={
            "mode": "retry", "apikey": "testkey", "value": nzo_id,
        })
        assert resp.json()["status"] is True
        assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "completed")
        job = manager.get(nzo_id)
        assert (Path(job.storage) / "Retry.Me.2024.mkv").read_bytes() == payload
    finally:
        httpd.shutdown()


def test_temporary_file_link_error_fails_immediately(client, fake_webshare, monkeypatch):
    """Webshare's "File temporarily unavailable" never recovers in practice, so
    the job must fail at once — no retries holding a download slot — and let
    Sonarr blocklist the release and grab another one."""
    attempts = 0

    async def dead_file_link(ident: str) -> str:
        nonlocal attempts
        attempts += 1
        raise WebshareError("Webshare /file_link/ failed: File temporarily unavailable.")

    monkeypatch.setattr(fake_webshare, "file_link", dead_file_link)
    nzb = build_nzb("temporary1", "Never.Available.mkv", 1000)
    nzo_id = client.post(
        "/sabnzbd/api",
        params={"mode": "addfile", "apikey": "testkey", "cat": "tv"},
        files={"nzbfile": ("Show S01E01.nzb", nzb.encode(), "application/x-nzb")},
    ).json()["nzo_ids"][0]

    manager = app.state.downloads
    assert wait_for(lambda: manager.get(nzo_id).status == "failed")
    assert attempts == 1
    assert "temporarily unavailable" in manager.get(nzo_id).error


def test_ensure_dirs_creates_category_folders(tmp_path, monkeypatch):
    with _start_app(tmp_path, monkeypatch, "http://127.0.0.1:1/x"):
        assert (tmp_path / "complete" / "tv").is_dir()
        assert (tmp_path / "complete" / "movies").is_dir()
        assert (tmp_path / "incomplete").is_dir()


def test_retry_unknown_job(client):
    resp = client.get("/sabnzbd/api", params={
        "mode": "retry", "apikey": "testkey", "value": "SABnzbd_nzo_nonexistent",
    })
    assert resp.json()["status"] is False


def test_stats_endpoint(client):
    resp = client.get("/stats", params={"apikey": "testkey"})
    assert resp.status_code == 200
    data = resp.json()
    assert {"downloading", "queued", "speed", "failed"}.issubset(data)
    assert isinstance(data["downloading"], int) and isinstance(data["speed"], str)
    # Wrong key must not expose queue details.
    assert client.get("/stats", params={"apikey": "nope"}).status_code == 403


def test_metrics_endpoint(client):
    resp = client.get("/metrics", params={"apikey": "testkey"})
    assert resp.status_code == 200
    body = resp.text
    assert "websharr_up 1" in body
    assert "# TYPE websharr_queue_downloading gauge" in body
    assert "websharr_history_failed" in body
    assert client.get("/metrics", params={"apikey": "nope"}).status_code == 403


def _set_max(client, n):
    resp = client.post("/ui/api/settings", params={"apikey": "testkey"},
                       json={"max_concurrent": n})
    assert resp.status_code == 200
    return resp


def _add_slow_jobs(client, payload_len, count):
    ids = []
    for i in range(count):
        nzb = build_nzb(f"c{i}", f"File{i}.mkv", payload_len)
        nzo = client.post(
            "/sabnzbd/api",
            params={"mode": "addfile", "apikey": "testkey", "cat": "tv"},
            files={"nzbfile": (f"Show S01E0{i}.nzb", nzb.encode(), "application/x-nzb")},
        ).json()["nzo_ids"][0]
        ids.append(nzo)
    return ids


def test_max_concurrent_limits_active_downloads(tmp_path, monkeypatch):
    payload = b"z" * (4 * 1024 * 1024)
    httpd = _serve_slow(payload, chunk=65536, delay=0.03)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.mkv"
        with _start_app(tmp_path, monkeypatch, url) as client:
            manager = app.state.downloads
            _set_max(client, 1)
            ids = _add_slow_jobs(client, len(payload), 3)

            def active():
                return sum(1 for j in manager.queue_jobs() if j.status == "downloading")

            assert wait_for(lambda: active() == 1)
            # Never exceed the limit while several jobs wait.
            for _ in range(6):
                assert active() <= 1
                assert manager.get(ids[1]).status == "queued"
                time.sleep(0.05)

            # Raising the limit immediately starts more waiting jobs.
            _set_max(client, 3)
            assert wait_for(lambda: active() == 3)
    finally:
        httpd.shutdown()


def test_reorder_changes_queue_order(tmp_path, monkeypatch):
    payload = b"z" * (4 * 1024 * 1024)
    httpd = _serve_slow(payload, chunk=65536, delay=0.03)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.mkv"
        with _start_app(tmp_path, monkeypatch, url) as client:
            manager = app.state.downloads
            _set_max(client, 1)
            ids = _add_slow_jobs(client, len(payload), 3)
            assert wait_for(lambda: manager.get(ids[0]).status == "downloading")

            # Move the last queued job ahead of the other queued one.
            resp = client.post("/ui/api/queue/reorder", params={"apikey": "testkey"},
                               json={"order": [ids[0], ids[2], ids[1]]})
            assert resp.json()["ok"] is True
            assert [j.nzo_id for j in manager.queue_jobs()] == [ids[0], ids[2], ids[1]]
            # The reorder must not have started a second download.
            assert manager.get(ids[2]).status == "queued"
            assert manager.get(ids[1]).status == "queued"
    finally:
        httpd.shutdown()


def test_error_text_never_empty():
    """httpx.ReadTimeout and friends have no message: the failure must still say what happened."""
    import httpx
    from app.downloads import describe_error
    assert describe_error(httpx.ReadTimeout("")) == "ReadTimeout"
    assert describe_error(WebshareError("File temporarily unavailable.")) == "File temporarily unavailable."
