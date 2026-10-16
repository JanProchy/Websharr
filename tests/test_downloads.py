"""Download manager: resume after restart (HTTP Range) and retry of failed jobs."""

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


def _serve_idents(payloads: dict[str, bytes], fake_webshare):
    """Serve a distinct payload per Webshare ident, so tests can tell apart
    which job's bytes ended up in which file."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = payloads[self.path.lstrip("/")]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async def file_link(ident: str) -> str:
        return f"http://127.0.0.1:{httpd.server_address[1]}/{ident}"

    fake_webshare.file_link = file_link
    return httpd


def _addfile(client, nzb: str, upload_name: str) -> str:
    resp = client.post("/sabnzbd/api", params={"mode": "addfile", "apikey": "testkey", "cat": "tv"},
                       files={"nzbfile": (upload_name, nzb.encode(), "application/x-nzb")})
    return resp.json()["nzo_ids"][0]


def _addurl(client, ident: str, name: str, nzbname: str = "") -> str:
    url = f"http://websharr:9797/torznab/nzb/{ident}?apikey=testkey&name={name}&size=0"
    params = {"mode": "addurl", "apikey": "testkey", "name": url, "cat": "tv"}
    if nzbname:
        params["nzbname"] = nzbname
    return client.post("/sabnzbd/api", params=params).json()["nzo_ids"][0]


def _finished(manager, *nzo_ids) -> list[Path]:
    assert wait_for(lambda: all(manager.get(n).status == "completed" for n in nzo_ids))
    return [Path(manager.get(n).storage) for n in nzo_ids]


def test_finished_file_is_named_after_the_release(client, fake_webshare):
    """A raw Czech name ("Kaceri pribehy S01E18 ...") doesn't map to the show in
    Sonarr, which parses the file before the folder. An NZB from our feed carries
    the release title it was advertised under; Sonarr uploads it and the file
    takes that title, keeping its extension. A UI grab's nzbname does the same."""
    release = "DuckTales S01E18 - Kaceri pribehy Kachnosaurus CZ"
    httpd = _serve_idents({"k18": b"k18" * 1000, "k20": b"k20" * 1000}, fake_webshare)
    try:
        nzb = client.get("/torznab/nzb/k18", params={
            "apikey": "testkey", "name": "Kaceri pribehy S01E18 Kachnosaurus.mkv",
            "size": "3000", "nzbname": release}).text
        sonarr = _addfile(client, nzb, f"{release}.nzb")
        ui = _addurl(client, "k20", "Kaceri pribehy S01E20.avi", nzbname="DuckTales S01E20 - Kaceri CZ")
        manager = app.state.downloads
        s_dir, u_dir = _finished(manager, sonarr, ui)
        assert [f.name for f in s_dir.iterdir()] == [f"{release}.mkv"]
        assert (s_dir / f"{release}.mkv").read_bytes() == b"k18" * 1000
        assert (u_dir / "DuckTales S01E20 - Kaceri CZ.avi").read_bytes() == b"k20" * 1000
    finally:
        httpd.shutdown()


def test_without_release_title_the_raw_name_stays(client, fake_webshare):
    """A direct addurl (no nzbname) and an NZB without an embedded title keep
    the raw Webshare name, as before."""
    httpd = _serve_idents({"r1": b"r1" * 500, "r2": b"r2" * 500}, fake_webshare)
    try:
        direct = _addurl(client, "r1", "Kaceri pribehy S01E19.avi")
        bare = _addfile(client, build_nzb("r2", "Kaceri pribehy S01E21.mkv", 1000), ".nzb")
        d_dir, b_dir = _finished(app.state.downloads, direct, bare)
        assert (d_dir / "Kaceri pribehy S01E19.avi").read_bytes() == b"r1" * 500
        assert (b_dir / "Kaceri pribehy S01E21.mkv").read_bytes() == b"r2" * 500
    finally:
        httpd.shutdown()


def test_generic_upload_names_keep_the_media_names(client, fake_webshare):
    """Two manual uploads both named "download.nzb" share the folder; each file
    keeps its embedded name, so neither replaces the other."""
    httpd = _serve_idents({"f1": b"first" * 400, "s2": b"second" * 400}, fake_webshare)
    try:
        first = _addfile(client, build_nzb("f1", "First.S01E01.mkv", 2000), "download.nzb")
        second = _addfile(client, build_nzb("s2", "Second.S02E02.mkv", 2400), "download.nzb")
        f_dir, s_dir = _finished(app.state.downloads, first, second)
        assert f_dir == s_dir and f_dir.name == "download"
        assert (f_dir / "First.S01E01.mkv").read_bytes() == b"first" * 400
        assert (f_dir / "Second.S02E02.mkv").read_bytes() == b"second" * 400
    finally:
        httpd.shutdown()


def test_upload_name_is_not_a_release_title(client, fake_webshare):
    """An upload filename only names the folder: "download.nzb" with
    Kaceri.S01E18.mkv inside stays Kaceri.S01E18.mkv, and an upload already
    named after the file ("Kaceri.S01E18.mkv.nzb") doesn't double the suffix."""
    httpd = _serve_idents({"g1": b"g1" * 500, "g2": b"g2" * 500}, fake_webshare)
    try:
        generic = _addfile(client, build_nzb("g1", "Kaceri.S01E18.mkv", 1000), "download.nzb")
        suffixed = _addfile(client, build_nzb("g2", "Kaceri.S01E19.mkv", 1000), "Kaceri.S01E19.mkv.nzb")
        g_dir, s_dir = _finished(app.state.downloads, generic, suffixed)
        assert [f.name for f in g_dir.iterdir()] == ["Kaceri.S01E18.mkv"]
        assert [f.name for f in s_dir.iterdir()] == ["Kaceri.S01E19.mkv"]
    finally:
        httpd.shutdown()


def test_release_title_with_the_extension_is_not_doubled(client, fake_webshare):
    httpd = _serve_idents({"d1": b"d1" * 500}, fake_webshare)
    try:
        nzo_id = _addurl(client, "d1", "kaceri.raw.mkv", nzbname="Kaceri.S01E18.MKV")
        (folder,) = _finished(app.state.downloads, nzo_id)
        assert [f.name for f in folder.iterdir()] == ["Kaceri.S01E18.mkv"]
    finally:
        httpd.shutdown()


def test_same_release_twice_never_replaces_a_finished_file(client, fake_webshare):
    """Two grabs of one release title land in one folder under one name: the
    second gets a counter instead of replacing the first."""
    httpd = _serve_idents({"a1": b"one" * 500, "a2": b"two" * 500}, fake_webshare)
    try:
        manager = app.state.downloads
        one = _addurl(client, "a1", "x.mkv", nzbname="Show S01E01 CZ")
        (folder,) = _finished(manager, one)
        two = _addurl(client, "a2", "y.mkv", nzbname="Show S01E01 CZ")
        _finished(manager, two)
        assert (folder / "Show S01E01 CZ.mkv").read_bytes() == b"one" * 500
        assert (folder / "Show S01E01 CZ (2).mkv").read_bytes() == b"two" * 500
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("title", ["T" * 252, "Kačeří příběhy " * 20])
def test_long_release_title_is_cut_to_the_filesystem_limit(client, fake_webshare, title):
    """A title that fits as a folder can be too long once ".mkv" is added
    (ENAMETOOLONG after the whole download). The name is cut to 255 UTF-8
    bytes when the job is added, keeping the extension and whole characters."""
    httpd = _serve_idents({"l1": b"l1" * 500, "l2": b"l2" * 500}, fake_webshare)
    try:
        manager = app.state.downloads
        titled = _addurl(client, "l1", "raw.mkv", nzbname=title)
        name = manager.get(titled).name  # fixed before anything is downloaded
        assert len(name.encode()) <= downloads_module.NAME_MAX and name.endswith(".mkv")
        assert title.startswith(name[:-len(".mkv")])
        upload = _addfile(client, build_nzb("l2", "Short.S01E01.mkv", 1000), f"{title}.nzb")
        t_dir, u_dir = _finished(manager, titled, upload)
        assert (t_dir / name).read_bytes() == b"l1" * 500
        assert (u_dir / "Short.S01E01.mkv").read_bytes() == b"l2" * 500
        assert all(len(d.name.encode()) <= downloads_module.NAME_MAX for d in (t_dir, u_dir))
    finally:
        httpd.shutdown()


def test_release_named_job_resumes_after_restart(tmp_path, monkeypatch):
    """The release-titled file name lives in the persisted `name` (no new state
    field an older version would choke on), so a resume finds the partial."""
    partial = PAYLOAD[:120_000]
    httpd, handler = _serve(PAYLOAD, support_range=True)
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/f.mkv"
        with _start_app(tmp_path, monkeypatch, "http://127.0.0.1:1/x") as client:
            job = app.state.downloads.get(_addurl(client, "res2", "raw.mkv", nzbname="Show S01E02 CZ"))
            assert wait_for(lambda: job.status == "failed")  # unreachable link
        assert job.name == "Show S01E02 CZ.mkv"
        state = json.loads((tmp_path / "state.json").read_text())
        assert set(state["jobs"][0]) == {
            "nzo_id", "ident", "name", "category", "size", "title", "status", "downloaded",
            "speed", "error", "storage", "added_ts", "completed_ts", "sab_hidden"}
        state["jobs"][0].update(status="downloading", error="", completed_ts=0.0)
        (tmp_path / "state.json").write_text(json.dumps(state))
        (tmp_path / "incomplete" / job.nzo_id / job.name).write_bytes(partial)

        with _start_app(tmp_path, monkeypatch, url):
            manager = app.state.downloads
            assert wait_for(lambda: manager.get(job.nzo_id).status == "completed")
            done = manager.get(job.nzo_id)
        assert (Path(done.storage) / "Show S01E02 CZ.mkv").read_bytes() == PAYLOAD
        assert f"bytes={len(partial)}-" in handler.ranges
    finally:
        httpd.shutdown()


def test_naming_helpers():
    fit, rel, NAME_MAX = downloads_module.fit_name, downloads_module.release_file_name, \
        downloads_module.NAME_MAX
    assert fit("Short.mkv") == "Short.mkv"
    assert fit("Film 1080p. Dabing") == "Film 1080p. Dabing"  # not an extension
    cut = fit("ř" * 200 + ".mkv")  # 400 bytes of 2-byte characters
    assert cut == "ř" * 125 + ".mkv" and len(cut.encode()) == 254
    assert rel("Show S01E01", "raw.avi") == "Show S01E01.avi"
    assert rel("Show.S01E01.avi", "raw.avi") == "Show.S01E01.avi"
    assert rel("Show S01E01", "no extension") == "Show S01E01"
    assert len(rel("T" * 300, "raw.m2ts").encode()) == NAME_MAX


def test_free_path_never_reuses_a_taken_name(tmp_path):
    free = downloads_module.free_path
    assert free(tmp_path, "A.mkv") == tmp_path / "A.mkv"
    (tmp_path / "A.mkv").write_bytes(b"1")
    (tmp_path / "A (2).mkv").mkdir()
    assert free(tmp_path, "A.mkv") == tmp_path / "A (3).mkv"
    long = "B" * 251 + ".mkv"
    (tmp_path / long).write_bytes(b"1")
    other = free(tmp_path, long)
    assert other.name.endswith(" (2).mkv") and len(other.name.encode()) == 255


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
