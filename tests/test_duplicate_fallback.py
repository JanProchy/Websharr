"""Duplicate fallback (opt-in): copies of a listed file ride along with a grab
and are downloaded instead only when the grabbed file itself is gone."""

import dataclasses
import http.server
import json
import logging
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

import app.downloads as downloads_module
from app.config import config
from app.main import app
from app.nzb import build_nzb, parse_nzb
from app.settings import settings
from app.torznab import content_identity, episode_identity, movie_identity
from app.webshare import SearchResult, WebshareError

from .conftest import FakeWebshareClient, wait_for
from .test_downloads import _start_app

GB = 1_000_000_000
PAYLOAD = b"websharr" * 50_000  # ~400 kB
FILE_NAME = "Vlny.2024.1080p.mkv"

# Job's fields on upstream main. A state record with any other field is lost
# when Websharr is rolled back: main's loader skips records Job(**item) rejects.
MAIN_JOB_FIELDS = ["nzo_id", "ident", "name", "category", "size", "title", "status",
                   "downloaded", "speed", "error", "storage", "added_ts", "completed_ts",
                   "sab_hidden"]


def _feed_items(client, **params) -> dict[str, ET.Element]:
    resp = client.get("/torznab/api", params={"apikey": "testkey", **params})
    return {i.findtext("guid"): i for i in ET.fromstring(resp.content).findall("channel/item")}


def _copies(item: ET.Element) -> list[str]:
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(item.findtext("link")).query)
    return qs["alt"][0].split(",") if "alt" in qs else []


def _plain_search(monkeypatch, fake_webshare, results, infos=None):
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.fuzzy = True
    fake_webshare.results = results
    fake_webshare.file_infos = infos or {}


# -- content identity -------------------------------------------------------

def test_content_identity_reads_each_name_itself():
    assert episode_identity("Futurama S08E04 1080p.mkv") == ((8, 4),)
    assert episode_identity("Futurama.8x04.mkv") == ((8, 4),)
    assert episode_identity("Show S01E01E02.mkv") == ((1, 1), (1, 2))
    assert episode_identity("Show S01E01-03.mkv") == ((1, 1), (1, 3))
    # A bare number, a resolution or a year is no episode marker.
    assert episode_identity("Skvrna 01 - Pohreb 1920x1080 2023.mkv") == ()
    assert movie_identity("Vlny (2024) CZ dabing 1080p.mkv") == ("vlny", 2024)
    assert movie_identity("The Film.2001.mkv") == ("film", 2001)
    assert movie_identity("2001 A Space Odyssey 1968.mkv") == ("2001 a space odyssey", 1968)
    assert movie_identity("Vlny 1080p.mkv") is None                  # no year
    # TV needs a marker; a movie needs title + year and no episode marker.
    assert content_identity("Skvrna 2023 01.mkv", "tv") is None
    assert content_identity("Vlny S01E01 2024.mkv", "movie") is None
    assert content_identity("Vlny 2024.mkv", "movie") == ("movie", ("vlny", 2024))


# -- feed ------------------------------------------------------------------

def test_off_by_default_feed_and_nzb_as_without_the_feature(client, fake_webshare, monkeypatch):
    assert settings.duplicate_fallback is False
    _plain_search(monkeypatch, fake_webshare, [
        SearchResult("u1", "Vlny.2024.1080p.mkv", 4 * GB),
        SearchResult("u2", "Vlny 2024 CZ dabing 1080p.mkv", 4 * GB),
    ])
    items = _feed_items(client, t="movie", q="Vlny 2024", cat="2000")
    assert set(items) == {"websharr-u1", "websharr-u2"}
    assert all("alt=" not in i.findtext("link") for i in items.values())
    # A link carrying copies (made while it was on) gets a plain NZB.
    resp = client.get("/torznab/nzb/u1", params={
        "apikey": "testkey", "name": FILE_NAME, "size": "4000000000",
        "alt": "u2", "altlen": "3600"})
    assert resp.content.decode() == build_nzb("u1", FILE_NAME, 4 * GB)


def test_every_file_stays_its_own_release_with_its_copies(client, fake_webshare, monkeypatch):
    """Nothing is merged: each upload is listed with its own guid, and only its
    link names the other uploads that are the same content."""
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    _plain_search(monkeypatch, fake_webshare, [
        SearchResult("u1", "Vlny.2024.1080p.mkv", 4 * GB, positive_votes=2),
        SearchResult("u2", "Vlny 2024 CZ dabing 1080p WEB-DL.mkv", 4 * GB, positive_votes=5),
        SearchResult("u3", "Vlny (2024) 1080p.mkv", 4 * GB),
        SearchResult("mp4", "Vlny.2024.1080p.mp4", 4 * GB),       # other container
        SearchResult("big", "Vlny.2024.2160p.mkv", 4 * GB + 1),   # other size
    ])
    items = _feed_items(client, t="movie", q="Vlny 2024", cat="2000")
    assert set(items) == {f"websharr-{i}" for i in ("u1", "u2", "u3", "mp4", "big")}
    assert sorted(_copies(items["websharr-u1"])) == ["u2", "u3"]
    assert sorted(_copies(items["websharr-u2"])) == ["u1", "u3"]
    assert "altlen=3600" in items["websharr-u1"].findtext("link")
    assert _copies(items["websharr-mp4"]) == [] and _copies(items["websharr-big"]) == []
    # Votes stay each file's own.
    grabs = {a.get("name"): a.get("value") for a in items["websharr-u2"].iter()
             if a.get("name") == "grabs"}
    assert grabs == {"grabs": "5"}


def test_show_search_without_season_never_pairs_two_episodes(client, fake_webshare, monkeypatch):
    """Review repro: equal-sized S01E01 and S01E02 of a show-level search were
    one release with E02 as E01's fallback. Episode identity comes from each
    name, whatever the query looked like."""
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    _plain_search(monkeypatch, fake_webshare, [
        SearchResult("e1", "Futurama S08E01 1080p.mkv", 2 * GB),
        SearchResult("e2", "Futurama S08E02 1080p.mkv", 2 * GB),
        SearchResult("b1", "Futurama 01 Bahnem 1080p.mkv", 2 * GB),   # bare numbers:
        SearchResult("b2", "Futurama 02 Bahnem 1080p.mkv", 2 * GB),   # no identity
        SearchResult("e4a", "Futurama s08e04 1080p.mkv", 3 * GB),
        SearchResult("e4b", "Futurama 8x04 Cesta k parazitum 1080p.mkv", 3 * GB),
    ])
    for params in ({"q": "Futurama"}, {"q": "Futurama", "season": "8"}):
        items = _feed_items(client, t="tvsearch", **params)
        assert all(_copies(items[f"websharr-{i}"]) == [] for i in ("e1", "e2")), params
        assert _copies(items["websharr-e4a"]) == ["e4b"], params
    items = _feed_items(client, t="tvsearch", q="Futurama")
    assert _copies(items["websharr-b1"]) == [] and _copies(items["websharr-b2"]) == []


def _enable_runtime_check(monkeypatch, minutes):
    from app import torznab
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")
    monkeypatch.setattr(settings, "runtime_check", True)

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        return "Vlny", "", "cs", (), 2024

    async def by_name(token, kind, q):
        return None

    async def runtime(*args, **kwargs):
        return minutes, True

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    monkeypatch.setattr(torznab, "tmdb_runtime", runtime)


def test_a_file_failing_a_filter_is_never_a_copy(client, fake_webshare, monkeypatch):
    """Review repro: a 10-minute file next to a 100-minute one (100-minute
    reference) stayed downloadable as an alternate. Only listed files are
    copies, so a copy passed every filter its file did; the length must match
    too."""
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    _enable_runtime_check(monkeypatch, 100)
    fake_webshare.fuzzy = True
    info = {"width": 1920, "height": 1080}
    fake_webshare.results = [
        SearchResult("full", "Vlny 2024 1080p.mkv", 4 * GB),
        SearchResult("short", "Vlny.2024.1080p.mkv", 4 * GB),          # runtime check
        SearchResult("locked", "Vlny (2024) 1080p.mkv", 4 * GB, password=True),
    ]
    fake_webshare.file_infos = {"full": {**info, "length": 6000},
                                "short": {**info, "length": 600},
                                "locked": {**info, "length": 6000}}
    for order in (list(fake_webshare.results), list(reversed(fake_webshare.results))):
        fake_webshare.results = order
        items = _feed_items(client, t="movie", tmdbid="1", cat="2000")
        assert set(items) == {"websharr-full"}
        assert _copies(items["websharr-full"]) == []


def test_copies_need_the_same_measured_length(client, fake_webshare, monkeypatch):
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    info = {"width": 1920, "height": 1080}
    _plain_search(monkeypatch, fake_webshare, [
        SearchResult("a", "Vlny.2024.1080p.mkv", 4 * GB),
        SearchResult("b", "Vlny 2024 1080p CZ.mkv", 4 * GB),     # 100 s longer
        SearchResult("c", "Vlny (2024) 1080p.mkv", 4 * GB),      # length unknown
        SearchResult("d", "Vlny 2024 WEB 1080p.mkv", 4 * GB),    # 1 s off: rounding
    ], {"a": {**info, "length": 6000}, "b": {**info, "length": 6100},
        "c": {**info, "length": 0}, "d": {**info, "length": 6001}})
    items = _feed_items(client, t="movie", q="Vlny 2024", cat="2000")
    assert len(items) == 4                                   # all listed
    assert _copies(items["websharr-a"]) == ["d"]
    assert _copies(items["websharr-b"]) == [] and _copies(items["websharr-c"]) == []
    assert "altlen=6000" in items["websharr-a"].findtext("link")


def test_order_and_new_uploads_never_change_a_files_identity(client, fake_webshare, monkeypatch):
    """Review repro: a richer-named upload changed the representative ident and
    with it guid and pubDate. Now guid, pubDate and title are the file's own,
    with any other uploads present, in any order."""
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    plain = SearchResult("aaa", "Vlny.2024.mkv", 4 * GB)
    rich = SearchResult("zzz", "Vlny 2024 CZ dabing 1080p WEB-DL.mkv", 4 * GB)
    _plain_search(monkeypatch, fake_webshare, [plain])

    def identity(items, guid):
        i = items[guid]
        return i.findtext("guid"), i.findtext("pubDate"), i.findtext("title")

    alone = _feed_items(client, t="movie", q="Vlny 2024", cat="2000")
    for order in ([plain, rich], [rich, plain]):
        fake_webshare.results = order
        items = _feed_items(client, t="movie", q="Vlny 2024", cat="2000")
        assert set(items) == {"websharr-aaa", "websharr-zzz"}
        assert identity(items, "websharr-aaa") == identity(alone, "websharr-aaa")
        assert _copies(items["websharr-aaa"]) == ["zzz"]
        assert _copies(items["websharr-zzz"]) == ["aaa"]


def test_feed_with_fallback_on_differs_only_in_links(client, fake_webshare, monkeypatch):
    _plain_search(monkeypatch, fake_webshare, [
        SearchResult("u1", "Vlny.2024.1080p.mkv", 4 * GB),
        SearchResult("u2", "Vlny 2024 CZ.mkv", 4 * GB),
        SearchResult("u3", "Vlny 2024 720p.mkv", 2 * GB),
    ])

    def strip(body: bytes) -> bytes:
        import re
        return re.sub(rb"&amp;alt=[^&\"<]*&amp;altlen=\d+", b"", body)

    off = client.get("/torznab/api", params={
        "apikey": "testkey", "t": "movie", "q": "Vlny 2024", "cat": "2000"}).content
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    on = client.get("/torznab/api", params={
        "apikey": "testkey", "t": "movie", "q": "Vlny 2024", "cat": "2000"}).content
    assert on != off and strip(on) == off


def test_nzb_carries_copies_only_with_the_fallback_on(client, monkeypatch):
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    resp = client.get("/torznab/nzb/u1", params={
        "apikey": "testkey", "name": FILE_NAME, "size": "4000000000",
        "alt": "u2,u3", "altlen": "6000"})
    payload = parse_nzb(resp.content)
    assert (payload.ident, payload.alternates, payload.length) == ("u1", ["u2", "u3"], 6000)
    # A link without copies gives the NZB as before the feature.
    resp = client.get("/torznab/nzb/u1", params={
        "apikey": "testkey", "name": FILE_NAME, "size": "4000000000"})
    assert resp.content.decode() == build_nzb("u1", FILE_NAME, 4 * GB)
    assert "websharr_alt" not in build_nzb("u1", FILE_NAME, 1)


# -- download client --------------------------------------------------------

def _serve(routes: dict):
    """HTTP server: path -> (status, body); body may be a callable(handler) for
    custom behaviour. Records the requested paths with their Range header."""

    class Handler(http.server.BaseHTTPRequestHandler):
        requests: list = []

        def do_GET(self):
            rng = self.headers.get("Range")
            Handler.requests.append((self.path, rng))
            status, body = routes.get(self.path, (404, b""))
            if callable(body):
                return body(self)
            if rng and status == 200:
                start = int(rng.removeprefix("bytes=").split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                body = body[start:]
            else:
                self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, Handler


class _Webshare:
    """Per-ident file_link/file_info behaviour patched onto FakeWebshareClient;
    records every call."""

    def __init__(self, monkeypatch, base: str, links: dict | None = None,
                 lengths: dict | None = None, sizes: dict | None = None):
        self.links = links or {}          # ident -> Exception to raise
        self.lengths = lengths or {}      # ident -> file_info length
        self.sizes = sizes or {}
        self.asked: list[str] = []
        self.probed: list[str] = []
        outer = self

        async def file_link(self, ident):
            outer.asked.append(ident)
            if ident in outer.links:
                raise outer.links[ident]
            return f"{base}/{ident}"

        async def file_info(self, ident):
            outer.probed.append(ident)
            return {"length": outer.lengths.get(ident, 6000), "size": outer.sizes.get(ident, 0)}

        monkeypatch.setattr(FakeWebshareClient, "file_link", file_link)
        monkeypatch.setattr(FakeWebshareClient, "file_info", file_info)


GONE = WebshareError("Webshare /file_link/ failed: File temporarily unavailable.")


def _grab(client, ident="g1", copies=("c1", "c2"), length=6000, size=len(PAYLOAD)):
    nzb = build_nzb(ident, FILE_NAME, size, list(copies), length)
    return client.post(
        "/sabnzbd/api", params={"mode": "addfile", "apikey": "testkey", "cat": "movies"},
        files={"nzbfile": ("Vlny 2024 1080p.nzb", nzb.encode(), "application/x-nzb")},
    ).json()["nzo_ids"][0]


@pytest.fixture
def fallback_on(client, monkeypatch):
    monkeypatch.setattr(settings, "duplicate_fallback", True)
    return client


def _state_records(path: Path) -> list[dict]:
    return json.loads(path.read_text())["jobs"]


def _assert_main_compatible(records: list[dict]) -> None:
    """Every record has exactly main's Job fields and loads with a constructor
    as strict as main's (unknown fields raise TypeError)."""
    MainJob = dataclasses.make_dataclass("MainJob", MAIN_JOB_FIELDS)
    for record in records:
        assert sorted(record) == sorted(MAIN_JOB_FIELDS)
        MainJob(**record)


def test_dead_link_downloads_a_verified_copy(fallback_on, tmp_path, monkeypatch, caplog):
    httpd, handler = _serve({"/c1": (200, PAYLOAD)})
    try:
        ws = _Webshare(monkeypatch, f"http://127.0.0.1:{httpd.server_address[1]}",
                       links={"g1": GONE})
        caplog.set_level(logging.INFO, logger="websharr.downloads")
        nzo_id = _grab(fallback_on)
        manager = app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "completed")
        job = manager.get(nzo_id)
        assert ws.asked == ["g1", "c1"] and ws.probed == ["c1"]
        # The grab keeps its identity; the copy lands under the grabbed name.
        assert (job.ident, job.name, job.error) == ("g1", FILE_NAME, "")
        assert (Path(job.storage) / FILE_NAME).read_bytes() == PAYLOAD
        assert not (tmp_path / "incomplete" / nzo_id).exists()   # sidecar gone too
        assert "downloaded identical copy c1 instead" in caplog.text
        _assert_main_compatible(_state_records(tmp_path / "state.json"))
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("error", [
    WebshareError("Webshare /file_link/ failed: Not logged in.", code="LOGIN_FATAL_1"),
    WebshareError("Webshare /login/ failed: Invalid credentials"),
    WebshareError("Webshare /salt/ failed: User not found."),
    WebshareError("Webshare /file_link/ failed: Not enough credit."),
    WebshareError("Webshare /file_link/ failed: VIP expired, file not found in quota."),
    WebshareError("Webshare /file_link/ failed: Some new message."),
    WebshareError("Webshare returned no download link"),
    httpx.HTTPStatusError("403", request=httpx.Request("POST", "https://webshare.cz/api/file_link/"),
                          response=httpx.Response(403)),
    httpx.ConnectError("network down"),
])
def test_account_or_network_errors_never_touch_the_copies(fallback_on, monkeypatch, error):
    ws = _Webshare(monkeypatch, "http://127.0.0.1:1", links={"g1": error})
    nzo_id = _grab(fallback_on)
    manager = app.state.downloads
    assert wait_for(lambda: manager.get(nzo_id).status == "failed")
    assert ws.asked == ["g1"] and ws.probed == []
    assert "copies tried" not in manager.get(nzo_id).error


@pytest.mark.parametrize("status", [404, 410])
def test_gone_at_download_start_falls_back(fallback_on, monkeypatch, status):
    httpd, handler = _serve({"/g1": (status, b""), "/c1": (200, PAYLOAD)})
    try:
        ws = _Webshare(monkeypatch, f"http://127.0.0.1:{httpd.server_address[1]}")
        nzo_id = _grab(fallback_on)
        manager = app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "completed")
        assert ws.asked == ["g1", "c1"]
        assert (Path(manager.get(nzo_id).storage) / FILE_NAME).read_bytes() == PAYLOAD
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_other_http_errors_at_start_do_not_fall_back(fallback_on, monkeypatch, status):
    httpd, handler = _serve({"/g1": (status, b""), "/c1": (200, PAYLOAD)})
    try:
        ws = _Webshare(monkeypatch, f"http://127.0.0.1:{httpd.server_address[1]}")
        nzo_id = _grab(fallback_on)
        manager = app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "failed")
        assert ws.asked == ["g1"] and ws.probed == []
        assert str(status) in manager.get(nzo_id).error
    finally:
        httpd.shutdown()


def test_stream_error_after_bytes_never_splices(fallback_on, tmp_path, monkeypatch):
    """Bytes of the grabbed file on disk: a later error never switches to a copy,
    neither mid-stream nor on a retry whose link is gone."""
    payload = bytes(range(256)) * (3 * 4096)        # 3 MiB: some chunks get written
    half = len(payload) // 2

    def cut_off(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload[:half])
        handler.wfile.flush()
        handler.close_connection = True

    httpd, handler = _serve({"/g1": (200, cut_off), "/c1": (200, payload)})
    try:
        ws = _Webshare(monkeypatch, f"http://127.0.0.1:{httpd.server_address[1]}")
        nzo_id = _grab(fallback_on, size=len(payload))
        manager = app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "failed")
        assert ws.asked == ["g1"] and ws.probed == []
        partial = tmp_path / "incomplete" / nzo_id / FILE_NAME
        written = partial.read_bytes()
        assert written and payload.startswith(written)

        ws.links["g1"] = GONE
        fallback_on.get("/sabnzbd/api", params={"mode": "retry", "apikey": "testkey",
                                                "value": nzo_id})
        assert wait_for(lambda: manager.get(nzo_id).status == "failed" and len(ws.asked) == 2)
        assert ws.asked == ["g1", "g1"] and ws.probed == []
        assert partial.read_bytes() == written
        assert [p for p, _ in handler.requests] == ["/g1"]
    finally:
        httpd.shutdown()


def test_copies_failing_the_recheck_are_skipped(fallback_on, tmp_path, monkeypatch):
    """Each copy is re-checked right before use: its length (file_info), its
    size (file_info when reported, and the download's announced size before a
    byte is written). Each is tried once; then the job fails."""
    httpd, handler = _serve({"/c3": (200, PAYLOAD + b"x"), "/c4": (200, PAYLOAD)})
    try:
        ws = _Webshare(monkeypatch, f"http://127.0.0.1:{httpd.server_address[1]}",
                       links={"g1": GONE}, lengths={"c1": 600}, sizes={"c2": len(PAYLOAD) - 1})
        nzo_id = _grab(fallback_on, copies=("c1", "c2", "c3"))
        manager = app.state.downloads
        assert wait_for(lambda: manager.get(nzo_id).status == "failed")
        assert ws.probed == ["c1", "c2", "c3"]
        assert ws.asked == ["g1", "c3"]                 # c1, c2 rejected before a link
        assert manager.get(nzo_id).error == \
            "Webshare /file_link/ failed: File temporarily unavailable. (3 identical copies tried)"
        work = tmp_path / "incomplete" / nzo_id
        assert not any(p.suffix == ".part" for p in work.iterdir())   # c3: nothing written

        # One good copy left: retried, it is used.
        nzo_id = _grab(fallback_on, ident="g1", copies=("c1", "c4"))
        assert wait_for(lambda: manager.get(nzo_id).status == "completed")
        assert (Path(manager.get(nzo_id).storage) / FILE_NAME).read_bytes() == PAYLOAD
    finally:
        httpd.shutdown()


def test_each_copy_is_tried_once(fallback_on, monkeypatch):
    ws = _Webshare(monkeypatch, "http://127.0.0.1:1",
                   links={"g1": GONE, "c1": GONE, "c2": GONE})
    nzo_id = _grab(fallback_on, copies=("c1", "c2", "c1", "g1"))
    manager = app.state.downloads
    assert wait_for(lambda: manager.get(nzo_id).status == "failed")
    assert ws.asked == ["g1", "c1", "c2"]
    assert manager.get(nzo_id).error.endswith("(2 identical copies tried)")


def test_copy_account_error_stops_the_fallback(fallback_on, monkeypatch):
    error = WebshareError("Webshare /file_link/ failed: Not enough credit.")
    ws = _Webshare(monkeypatch, "http://127.0.0.1:1", links={"g1": GONE, "c1": error})
    nzo_id = _grab(fallback_on)
    manager = app.state.downloads
    assert wait_for(lambda: manager.get(nzo_id).status == "failed")
    assert ws.asked == ["g1", "c1"]
    assert manager.get(nzo_id).error == str(error)


def _seed(tmp_path, sidecar: dict | None, files: dict) -> str:
    nzo_id = "SABnzbd_nzo_copy01"
    state = {"jobs": [{
        "nzo_id": nzo_id, "ident": "g1", "name": FILE_NAME, "category": "movies",
        "size": len(PAYLOAD), "status": "downloading", "downloaded": 0, "error": "",
        "storage": "", "added_ts": 1.0, "completed_ts": 0.0,
    }]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    work = tmp_path / "incomplete" / nzo_id
    work.mkdir(parents=True)
    if sidecar is not None:
        (work / downloads_module.FALLBACK_FILE).write_text(json.dumps(sidecar))
    for name, data in files.items():
        (work / name).write_bytes(data)
    return nzo_id


_SIDECAR = {"ident": "g1", "size": len(PAYLOAD), "length": 6000, "alternates": ["c1", "c2"]}


def test_restart_resumes_a_copy_from_that_copy_only(tmp_path, monkeypatch):
    nzo_id = _seed(tmp_path, {**_SIDECAR, "using": "c1"},
                   {".websharr-copy-c1.part": PAYLOAD[:120_000]})
    httpd, handler = _serve({"/c1": (200, PAYLOAD), "/g1": (200, PAYLOAD)})
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        ws = _Webshare(monkeypatch, base)
        monkeypatch.setattr(config, "duplicate_fallback", True)
        with _start_app(tmp_path, monkeypatch, base):
            manager = app.state.downloads
            assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "completed")
            job = manager.get(nzo_id)
        assert ws.asked == ["c1"]
        assert handler.requests == [("/c1", "bytes=120000-")]
        assert (Path(job.storage) / FILE_NAME).read_bytes() == PAYLOAD
    finally:
        httpd.shutdown()


def test_turned_off_the_job_downloads_as_without_copies(tmp_path, monkeypatch):
    """A copy's partial bytes live under their own name: with the fallback off
    (or after a rollback), the grabbed file starts clean, nothing is mixed."""
    nzo_id = _seed(tmp_path, {**_SIDECAR, "using": "c1"},
                   {".websharr-copy-c1.part": b"other upload's bytes"})
    httpd, handler = _serve({"/g1": (200, PAYLOAD)})
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        ws = _Webshare(monkeypatch, base)
        with _start_app(tmp_path, monkeypatch, base):
            manager = app.state.downloads
            assert wait_for(lambda: (j := manager.get(nzo_id)) and j.status == "completed")
            job = manager.get(nzo_id)
        assert ws.asked == ["g1"] and handler.requests == [("/g1", None)]
        assert (Path(job.storage) / FILE_NAME).read_bytes() == PAYLOAD
    finally:
        httpd.shutdown()


def test_grab_with_fallback_off_keeps_no_copies(client, tmp_path, monkeypatch):
    ws = _Webshare(monkeypatch, "http://127.0.0.1:1", links={"g1": GONE})
    nzo_id = _grab(client)
    manager = app.state.downloads
    assert wait_for(lambda: manager.get(nzo_id).status == "failed")
    assert ws.asked == ["g1"]
    assert not (tmp_path / "incomplete" / nzo_id / downloads_module.FALLBACK_FILE).exists()


def test_copies_live_beside_the_job_not_in_state(fallback_on, tmp_path, monkeypatch):
    """Rollback safety: state.json records keep exactly main's Job fields, so an
    older Websharr loads every job; the copies sit in the job's incomplete
    folder, which goes with the job."""
    error = WebshareError("Webshare /file_link/ failed: Not logged in.")
    _Webshare(monkeypatch, "http://127.0.0.1:1", links={"g1": GONE, "q1": error, "c1": error})
    manager = app.state.downloads
    # addurl: the copies come from the feed link; an unsafe ident is dropped.
    link = ("http://websharr/torznab/nzb/q1?apikey=testkey&name=Other.2024.mkv"
            "&size=5000000000&alt=q2%2C..%2Fx&altlen=6000")
    other = fallback_on.post("/sabnzbd/api", params={
        "mode": "addurl", "apikey": "testkey", "name": link, "cat": "movies"}).json()["nzo_ids"][0]
    sidecar = tmp_path / "incomplete" / other / downloads_module.FALLBACK_FILE
    assert json.loads(sidecar.read_text()) == {
        "ident": "q1", "size": 5 * GB, "length": 6000, "alternates": ["q2"]}
    failed = _grab(fallback_on)
    assert wait_for(lambda: manager.get(failed).status == "failed"
                    and manager.get(other).status == "failed")
    _assert_main_compatible(_state_records(tmp_path / "state.json"))

    fallback_on.get("/sabnzbd/api", params={"mode": "history", "name": "delete",
                                            "apikey": "testkey", "value": other})
    assert sidecar.exists()            # *arr hiding it keeps the record and files
    assert manager.delete(other)
    assert not sidecar.parent.exists()


def test_state_with_the_drafts_alternates_key_still_loads(tmp_path):
    """An early draft of this feature wrote `alternates` on every job (as in
    some live state files); those records load, the key is ignored."""
    state = {"jobs": [{
        "nzo_id": "SABnzbd_nzo_draft1", "ident": "d1", "name": FILE_NAME,
        "category": "movies", "size": 1, "title": "", "status": "completed",
        "downloaded": 1, "speed": 0.0, "error": "", "storage": "/x", "added_ts": 1.0,
        "completed_ts": 2.0, "sab_hidden": False, "alternates": ["d2", "d3"],
    }]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    manager = downloads_module.DownloadManager(
        FakeWebshareClient(), tmp_path / "complete", tmp_path / "incomplete",
        tmp_path / "state.json")
    manager.load_state()
    job = manager.get("SABnzbd_nzo_draft1")
    assert job is not None and job.ident == "d1" and job.status == "completed"
    manager._save_state()
    _assert_main_compatible(_state_records(tmp_path / "state.json"))


def test_link_gone_contract():
    from app.downloads import link_gone
    assert link_gone(GONE)
    assert link_gone(WebshareError("Webshare /file_link/ failed: File not found."))
    assert not link_gone(WebshareError("Webshare /file_link/ failed: Not logged in."))
    assert not link_gone(WebshareError("Webshare /salt/ failed: User not found."))
    assert not link_gone(WebshareError("Webshare /file_link/ failed: Daily limit reached, not found"))
    assert not link_gone(httpx.ConnectError("not found"))
