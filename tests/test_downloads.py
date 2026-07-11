"""Download manager: resume after restart (HTTP Range) and retry of failed jobs."""

import http.server
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import config
from app.main import app
from app.nzb import build_nzb

from .conftest import FakeWebshareClient, wait_for

PAYLOAD = b"websharr" * 50_000  # ~400 kB
FILE_NAME = "Resumed.Movie.2024.mkv"


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


def _seed_interrupted_job(tmp_path, partial: bytes) -> str:
    """Write a state file + partial file as if a download was cut off mid-way."""
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


def test_retry_failed_job(client, fake_webshare, tmp_path):
    # First attempt fails: the default fake link is unreachable.
    nzb = build_nzb("rty1", "Retry.Me.2024.mkv", 100)
    resp = client.post(
        "/sabnzbd/api",
        params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
        files={"nzbfile": ("x.nzb", nzb.encode(), "application/x-nzb")},
    )
    nzo_id = resp.json()["nzo_ids"][0]
    manager = app.state.downloads
    assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "failed")

    # Fix the link, retry via the API, and the job completes.
    payload = b"retry-payload" * 1000
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
