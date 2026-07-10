from app.main import app
from app.nzb import build_nzb

from .conftest import wait_for


def _api(client, **params):
    params.setdefault("apikey", "testkey")
    params.setdefault("output", "json")
    return client.get("/sabnzbd/api", params=params)


def test_version_and_config(client):
    assert "version" in _api(client, mode="version").json()
    cfg = _api(client, mode="get_config").json()["config"]
    assert any(c["name"] == "tv" for c in cfg["categories"])
    assert cfg["misc"]["complete_dir"]


def test_bad_apikey(client):
    resp = client.get("/sabnzbd/api", params={"mode": "version", "apikey": "nope"})
    assert resp.status_code == 403


def test_addfile_download_lifecycle(client, fake_webshare, tmp_path, httpserver=None):
    # Serve a real file over HTTP so the download pipeline runs end-to-end.
    import http.server
    import threading

    payload = b"x" * 300_000
    served = tmp_path / "srv"
    served.mkdir()
    (served / "file.mkv").write_bytes(payload)

    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(served), **kw)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        fake_webshare.file_link_url = f"http://127.0.0.1:{httpd.server_address[1]}/file.mkv"

        nzb = build_nzb("id9", "zaklinac.raw.file.mkv", len(payload))
        # Sonarr uploads the NZB named after the release title; that becomes the
        # job folder (so it carries SxxEyy), while the file keeps its raw name.
        resp = client.post(
            "/sabnzbd/api",
            params={"mode": "addfile", "apikey": "testkey", "cat": "tv"},
            files={"nzbfile": ("Zaklinac S01E05 1080p.nzb", nzb.encode(), "application/x-nzb")},
        )
        body = resp.json()
        assert body["status"] is True
        nzo_id = body["nzo_ids"][0]

        manager = app.state.downloads
        assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "completed")

        hist = _api(client, mode="history").json()["history"]["slots"]
        assert len(hist) == 1
        slot = hist[0]
        assert slot["nzo_id"] == nzo_id
        assert slot["status"] == "Completed"
        assert slot["category"] == "tv"
        assert slot["storage"].endswith("tv/Zaklinac S01E05 1080p")

        from pathlib import Path
        final = Path(slot["storage"]) / "zaklinac.raw.file.mkv"
        assert final.read_bytes() == payload
    finally:
        httpd.shutdown()


def test_failed_download_lands_in_history(client, fake_webshare):
    # Default fake file_link points at an unreachable address -> download fails.
    nzb = build_nzb("bad1", "Broken.Movie.2024.mkv", 1000)
    resp = client.post(
        "/sabnzbd/api",
        params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
        files={"nzbfile": ("x.nzb", nzb.encode(), "application/x-nzb")},
    )
    nzo_id = resp.json()["nzo_ids"][0]

    manager = app.state.downloads
    assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "failed")

    slot = _api(client, mode="history").json()["history"]["slots"][0]
    assert slot["status"] == "Failed"
    assert slot["fail_message"]


def test_addurl(client, fake_webshare):
    url = "http://websharr:9797/torznab/nzb/idX?apikey=testkey&name=Film.2024.mkv&size=1234"
    resp = client.post("/sabnzbd/api", params={
        "mode": "addurl", "apikey": "testkey", "name": url, "cat": "movies",
    })
    body = resp.json()
    assert body["status"] is True
    job = app.state.downloads.get(body["nzo_ids"][0])
    assert job.ident == "idX"
    assert job.name == "Film.2024.mkv"


def test_queue_delete(client, fake_webshare):
    nzb = build_nzb("del1", "ToDelete.mkv", 1000)
    resp = client.post(
        "/sabnzbd/api",
        params={"mode": "addfile", "apikey": "testkey"},
        files={"nzbfile": ("x.nzb", nzb.encode(), "application/x-nzb")},
    )
    nzo_id = resp.json()["nzo_ids"][0]
    resp = _api(client, mode="queue", name="delete", value=nzo_id)
    assert resp.json()["status"] is True
    assert app.state.downloads.get(nzo_id) is None
