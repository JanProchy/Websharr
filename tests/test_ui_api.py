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
    assert __version__ == "0.3.12"
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


def test_settings_rejects_invalid_categories(client):
    for bad in ("tv,Kids Shows", "tv,../x", "tv,tv", "a" * 41, 5, None, [5], ["tv", None], [["tv"]]):
        resp = client.post("/ui/api/settings", params={"apikey": "testkey"}, json={"categories": bad})
        assert resp.status_code == 400, bad
        assert resp.json()["error"]
    # Nothing was applied: the defaults stay in place.
    assert _ui(client, "settings").json()["categories"] == ["tv", "movies"]


def _settings_snapshot(client, tmp_path):
    """Everything a settings save can change: UI view, live manager, SAB view,
    the settings file and the category folders on disk."""
    from app.main import app
    settings_file = tmp_path / "settings.json"
    return {
        "ui": _ui(client, "settings").json(),
        "live": (app.state.downloads.max_concurrent, app.state.downloads.categories),
        "sab": client.get("/sabnzbd/api", params={
            "mode": "get_cats", "apikey": "testkey", "output": "json"}).json(),
        "file": settings_file.read_text() if settings_file.is_file() else None,
        "dirs": sorted(p.name for p in (tmp_path / "complete").iterdir()),
    }


def _spy_schedule(monkeypatch):
    from app.main import app
    calls = []
    manager = app.state.downloads
    original = manager._schedule
    monkeypatch.setattr(manager, "_schedule", lambda: (calls.append(1), original())[1])
    return calls


def test_rejected_multi_field_save_changes_nothing(client, tmp_path, monkeypatch):
    # Any invalid field rejects the whole save: the valid ones submitted with it
    # (max_concurrent 2 -> 9, a new category, ...) must not be applied either,
    # and the download queue must not be rescheduled with the new limit.
    before = _settings_snapshot(client, tmp_path)
    assert before["live"] == (2, ["tv", "movies"])
    calls = _spy_schedule(monkeypatch)
    valid = {"max_concurrent": 9, "categories": "tv,movies,tv-kids", "notify_vip_days": 3,
             "notify_urls": "json://example", "tmdb_token": "tok", "theme": "osaka-jade"}
    for bad in ({"categories": "tv,Kids Shows"}, {"max_concurrent": "lots"},
                {"notify_vip_days": "soon"}, {"theme": "nope"},
                {"webshare_username": "someone-else"}):
        resp = client.post("/ui/api/settings", params={"apikey": "testkey"},
                           json={**valid, "regenerate_api_key": True, **bad})
        assert resp.status_code == 400, bad
        assert resp.json()["error"]
        assert _settings_snapshot(client, tmp_path) == before, bad
        assert not (tmp_path / "complete" / "tv-kids").exists()
    assert calls == []

    # The same values without the bad field are applied everywhere.
    resp = client.post("/ui/api/settings", params={"apikey": "testkey"}, json=valid)
    assert resp.status_code == 200
    after = _settings_snapshot(client, tmp_path)
    assert after["live"] == (9, ["tv", "movies", "tv-kids"])
    assert after["ui"]["categories"] == ["tv", "movies", "tv-kids"]
    assert after["ui"]["notify_vip_days"] == 3
    assert calls == [1]


def test_category_folder_collision_rejects_save(client, tmp_path, monkeypatch):
    # A regular file where a category folder should go: the save fails with a
    # controlled error and SAB, UI, the settings file and the disk keep the
    # previous state (no half-created folders left behind).
    client.post("/ui/api/settings", params={"apikey": "testkey"}, json={"notify_vip_days": 5})
    (tmp_path / "complete" / "collision").write_text("not a folder")
    before = _settings_snapshot(client, tmp_path)
    calls = _spy_schedule(monkeypatch)

    resp = client.post("/ui/api/settings", params={"apikey": "testkey"},
                       json={"categories": "tv,newcat,collision", "max_concurrent": 6})
    assert resp.status_code == 400
    assert "collision" in resp.json()["error"]
    assert _settings_snapshot(client, tmp_path) == before
    assert not (tmp_path / "complete" / "newcat").exists()
    assert (tmp_path / "complete" / "collision").is_file()
    assert calls == []


def test_settings_write_failure_keeps_previous_state(client, tmp_path, monkeypatch):
    # settings.json cannot be written (here: a directory in its place). The
    # in-memory settings are restored and the download manager is untouched.
    from app.config import config
    from app.main import app
    from app.settings import settings
    blocked = tmp_path / "blocked.json"
    blocked.mkdir()
    monkeypatch.setattr(config, "settings_file", blocked)
    calls = _spy_schedule(monkeypatch)

    resp = client.post("/ui/api/settings", params={"apikey": "testkey"},
                       json={"categories": "tv,tv-kids", "max_concurrent": 7, "theme": "space-grey"})
    assert resp.status_code == 500
    assert resp.json()["error"]
    assert (settings.categories, settings.max_concurrent, settings.theme) == (
        ["tv", "movies"], 2, "websharr-blue")
    assert (app.state.downloads.max_concurrent, app.state.downloads.categories) == (2, ["tv", "movies"])
    assert calls == []


def test_empty_category_list_recreates_complete_root(client, tmp_path):
    # `categories=[]` leaves only the implicit "*", whose folder is COMPLETE_DIR
    # itself (get_config dir ""); saving it must (re)create that root.
    import shutil
    shutil.rmtree(tmp_path / "complete")
    resp = client.post("/ui/api/settings", params={"apikey": "testkey"}, json={"categories": []})
    assert resp.status_code == 200
    assert (tmp_path / "complete").is_dir()
    assert client.get("/sabnzbd/api", params={
        "mode": "get_cats", "apikey": "testkey", "output": "json"}).json()["categories"] == ["*"]


def test_grab_form_offers_configured_categories(client):
    # The manual grab form lists the SABnzbd categories (get_cats), so a custom
    # one like tv-kids can be picked instead of only Shows/Movies.
    client.post("/ui/api/setup", json={"username": "u", "password": "pass1234"})
    html = client.get("/ui").text
    assert 'class="g-cat"' in html
    assert 'sab({ mode: "get_cats" })' in html
    assert "const cat = catSel.value;" in html


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
