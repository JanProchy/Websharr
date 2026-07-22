"""Tests for the /ui and /ui/api/* endpoints."""

from app import __version__
from app.webshare import SearchResult

from .conftest import wait_for


def _ui(client, path, **params):
    params.setdefault("apikey", "testkey")
    return client.get(f"/ui/api/{path}", params=params)


def test_ui_page_served_with_key_injected(client):
    # /ui serves the app only after first-run setup (which also signs us in).
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "testkey" in resp.text
    assert "__WEBSHARR_API_KEY__" not in resp.text


def test_app_version_is_current_and_rendered(client):
    assert __version__ == "0.3.1"
    assert _ui(client, "status").json()["version"] == __version__
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    assert f'<span class="version">v{__version__}</span>' in client.get("/ui").text


def test_tab_paths_serve_the_app(client):
    # Each tab has its own clean path (/search, /history, …) serving the SPA,
    # so a refresh stays put; the bare domain serves it too.
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    for path in ("/", "/queue", "/history", "/search", "/log", "/help", "/settings", "/ui"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert 'id="view-queue"' in resp.text, path


def test_ui_assets(client):
    resp = client.get("/ui/assets/favicon.svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]
    assert client.get("/ui/assets/../config.py").status_code in (403, 404)
    assert client.get("/ui/assets/nope.svg").status_code == 404


def test_ui_api_requires_key(client):
    for path in ("status", "queue", "history", "search"):
        resp = client.get(f"/ui/api/{path}")
        assert resp.status_code == 403, path
        resp = client.get(f"/ui/api/{path}", params={"apikey": "wrong"})
        assert resp.status_code == 403, path


def test_ui_status(client):
    body = _ui(client, "status").json()
    assert body["app"] == "websharr"
    assert "version" in body
    assert body["queue"] == 0
    assert body["history"] == 0


def test_ui_search_returns_json(client, fake_webshare):
    fake_webshare.results = [
        SearchResult("id1", "Zaklinac.S01E05.1080p.CZ.mkv", 4_000_000_000),
        SearchResult("id2", "Zaklinac.S01E05.2160p.CZ.mkv", 12_000_000_000),
        SearchResult("id3", "Zaklinac.S01E05.readme.txt", 1_000),  # not video
        SearchResult("id4", "Zaklinac.S01E05.720p.mkv", 1_000_000_000, password=True),
    ]
    body = _ui(client, "search", q="Zaklinac", t="tvsearch", season="1", ep="5").json()

    assert body["queries"] == ["Zaklinac S01E05", "Zaklinac 1x05", "Zaklinac 05"]
    results = body["results"]
    # txt and password-protected filtered out; sorted by size desc
    assert [r["ident"] for r in results] == ["id2", "id1"]
    assert results[0]["title"] == "Zaklinac.S01E05.2160p.CZ"
    # release name (nzbname) carries the requested SxxEyy for parseable import
    assert results[0]["release"] == "Zaklinac S01E05 - Zaklinac.2160p.CZ"
    assert results[0]["format"] == "mkv"
    assert results[0]["size"] == 12_000_000_000
    assert "/torznab/nzb/id2" in results[0]["nzb_url"]
    assert "nzbname=Zaklinac" in results[0]["nzb_url"]
    assert "apikey=testkey" in results[0]["nzb_url"]


def test_ui_search_filters_wrong_episode(client, fake_webshare):
    """Webshare fulltext is OR-based and returns every episode; a search for a
    specific episode must drop the others and rank the rest biggest-first."""
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("wrong-ep", "Zaklínač.S04E02.2160p.mkv", 20_000_000_000),
        SearchResult("right-small", "Zaklinac.S01E03.720p.mkv", 700_000_000),
        SearchResult("right-big", "Zaklínač S01E03 1080p CZ.mkv", 3_000_000_000),
    ]
    # Episode typed into the box is parsed out; only S01E03 files survive.
    body = _ui(client, "search", q="zaklinac s01e03", t="tvsearch").json()
    assert [r["ident"] for r in body["results"]] == ["right-big", "right-small"]


def test_ui_fileinfo(client, fake_webshare):
    fake_webshare.file_infos = {
        "a": {"length": 3663, "width": 1920, "height": 1080, "format": "H264", "type": "mkv"},
    }
    body = _ui(client, "fileinfo", idents="a,b").json()
    assert body["info"]["a"]["length"] == 3663
    assert body["info"]["a"]["height"] == 1080
    # 'b' falls back to the fake's default metadata (still present)
    assert "b" in body["info"]
    assert _ui(client, "fileinfo", idents="").json() == {"info": {}}


def test_ui_log_records(client, fake_webshare):
    import logging
    logging.getLogger("websharr.test").info("hello-from-test-42")
    recs = _ui(client, "log").json()["records"]
    assert any(r["message"] == "hello-from-test-42" for r in recs)
    # Incremental fetch returns only strictly-newer records, never repeats.
    newest = max(r["seq"] for r in recs)
    again = _ui(client, "log", after=newest).json()["records"]
    assert all(r["seq"] > newest for r in again)
    assert not any(r["message"] == "hello-from-test-42" for r in again)


def test_ui_grab_sets_parseable_folder_title(client, fake_webshare):
    """A UI grab with nzbname names the download folder with the SxxEyy title,
    so *arr import can parse it even though the raw file has none."""
    fake_webshare.results = [SearchResult("epX", "Skvrna 01 - Pohreb.mkv", 1000)]
    r = _ui(client, "search", q="Skvrna", t="tvsearch", season="1", ep="1").json()["results"][0]
    assert r["release"] == "Skvrna S01E01 - Skvrna 01 - Pohreb"

    resp = client.post("/sabnzbd/api", params={
        "mode": "addurl", "apikey": "testkey", "name": r["nzb_url"],
        "nzbname": r["release"], "cat": "tv",
    })
    nzo_id = resp.json()["nzo_ids"][0]

    from app.downloads import Job
    from app.nzb import sanitize_filename
    job: Job = client.app.state.downloads.get(nzo_id)
    assert job.title == sanitize_filename(r["release"])
    # job_name (folder + what Sonarr reads) carries the parseable SxxEyy.
    assert job.job_name.startswith("Skvrna S01E01")


def test_ui_search_empty_query(client):
    body = _ui(client, "search", q="", t="search").json()
    assert body["results"] == []


def test_ui_search_bad_type(client):
    resp = _ui(client, "search", q="x", t="bogus")
    assert resp.status_code == 400


def test_ui_grab_flow_lands_in_queue_json(client, fake_webshare):
    """Search via /ui/api/search, grab via SABnzbd addurl (what the UI does),
    then the job shows up in /ui/api/queue or /ui/api/history."""
    fake_webshare.results = [SearchResult("idX", "Film.2024.1080p.CZ.mkv", 1234)]
    result = _ui(client, "search", q="Film 2024", t="movie").json()["results"][0]

    resp = client.post("/sabnzbd/api", params={
        "mode": "addurl", "apikey": "testkey", "name": result["nzb_url"], "cat": "movies",
    })
    body = resp.json()
    assert body["status"] is True
    nzo_id = body["nzo_ids"][0]

    def in_ui_api():
        jobs = (_ui(client, "queue").json()["jobs"]
                + _ui(client, "history").json()["jobs"])
        return any(j["nzo_id"] == nzo_id for j in jobs)

    assert wait_for(in_ui_api)

    # Job JSON carries the documented fields.
    jobs = _ui(client, "queue").json()["jobs"] + _ui(client, "history").json()["jobs"]
    job = next(j for j in jobs if j["nzo_id"] == nzo_id)
    for field in ("nzo_id", "ident", "name", "category", "size", "status",
                  "downloaded", "speed", "error", "storage", "added_ts", "completed_ts"):
        assert field in job
    assert job["ident"] == "idX"
    assert job["name"] == "Film.2024.1080p.CZ.mkv"
    assert job["category"] == "movies"


def test_ui_history_shows_failed_error(client, fake_webshare):
    # Default fake file_link is unreachable -> download fails.
    fake_webshare.results = [SearchResult("bad1", "Broken.Movie.2024.mkv", 1000)]
    result = _ui(client, "search", q="Broken Movie", t="movie").json()["results"][0]
    nzo_id = client.post("/sabnzbd/api", params={
        "mode": "addurl", "apikey": "testkey", "name": result["nzb_url"], "cat": "movies",
    }).json()["nzo_ids"][0]

    assert wait_for(lambda: any(
        j["nzo_id"] == nzo_id and j["status"] == "failed"
        for j in _ui(client, "history").json()["jobs"]))

    job = next(j for j in _ui(client, "history").json()["jobs"] if j["nzo_id"] == nzo_id)
    assert job["error"]
