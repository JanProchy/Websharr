"""Download manager: resume after restart (HTTP Range) and retry of failed jobs."""

import http.server
import json
import threading
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

    def factory(username, password):
        fake = FakeWebshareClient(username, password)
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


def test_retry_unknown_job(client):
    resp = client.get("/sabnzbd/api", params={
        "mode": "retry", "apikey": "testkey", "value": "SABnzbd_nzo_nonexistent",
    })
    assert resp.json()["status"] is False
