import xml.etree.ElementTree as ET

from app.torznab import (
    alias_titles,
    build_queries,
    file_episode,
    matches_query,
    parse_query,
    release_title,
)
from app.webshare import SearchResult

TZNS = "{http://torznab.com/schemas/2015/feed}"
NZNS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"


def test_build_queries():
    # Season 1 adds a bare-episode-number variant for CZ "Series 01" naming.
    assert build_queries("tvsearch", "Zaklinac", "1", "5") == \
        ["Zaklinac S01E05", "Zaklinac 1x05", "Zaklinac 05"]
    # Later seasons omit the bare number (would collide across seasons).
    assert build_queries("tvsearch", "Zaklinac", "2", "5") == ["Zaklinac S02E05", "Zaklinac 2x05"]
    assert build_queries("tvsearch", "Zaklinac", "2", None) == ["Zaklinac S02"]
    assert build_queries("movie", "Vlny 2024", None, None) == ["Vlny 2024"]
    assert build_queries("search", "  ", None, None) == []


def test_release_title_normalizes_for_tvsearch():
    # CZ filename without SxxEyy gets a parseable prefix, original kept for quality.
    assert release_title("Skvrna", "1", "1", "Skvrna 01 - Pohreb (Cajda).mp4") == \
        "Skvrna S01E01 - Skvrna 01 - Pohreb (Cajda)"
    # Season-only search.
    assert release_title("Skvrna", "2", None, "whatever.mkv") == "Skvrna S02 - whatever"
    # Non-tv search leaves the name (stem) untouched.
    assert release_title("Vlny 2024", None, None, "Vlny.2024.1080p.mkv") == "Vlny.2024.1080p"


def test_parse_query_extracts_episode_from_text():
    # Typing "skvrna s01e05" into the box (no season/ep fields) is parsed out.
    assert parse_query("tvsearch", "skvrna s01e05", None, None) == ("tvsearch", "skvrna", "01", "05")
    assert parse_query("search", "Skvrna 1x05", None, None) == ("tvsearch", "Skvrna", "1", "05")
    # Explicit season/ep from the caller win untouched.
    assert parse_query("tvsearch", "skvrna", "2", "3") == ("tvsearch", "skvrna", "2", "3")
    # No episode marker -> unchanged.
    assert parse_query("movie", "Vlny 2024", None, None) == ("movie", "Vlny 2024", None, None)


def test_matches_query_requires_name_to_start_with_title():
    # The name must begin with the show title; episode markers aren't required.
    assert matches_query("Skvrna S01E05", "Skvrna 05 - Bestie (Cajda).mp4")
    assert matches_query("Skvrna S01E05", "Skvrna S01E05 1080p CZ.mkv")
    # Junk that merely shares "S01E05"/"05" is rejected.
    assert not matches_query("Skvrna S01E05", "Our.Planet.2019.S01E05.2160p.mkv")
    assert not matches_query("Skvrna S01E05", "WWE Monday Night Raw S34E01.mkv")
    # Unrelated titles that merely contain the common word "skvrna" — rejected
    # because they don't *start* with it (the real false positives we hit).
    assert not matches_query("Skvrna S01E05", "2 Socky S01e15 FHD 1080p CZ A slepá skvrna.mkv")
    assert not matches_query("Skvrna", "Lidská skvrna (2003) en+Cz dabing.mkv")
    assert not matches_query("Skvrna", "TO - Vítejte v Derry 2025 CZ - Černá skvrna.mkv")
    # Diacritics-insensitive, multi-word titles need every word in order.
    assert matches_query("Zaklinac", "Zaklínač.S01E03.1080p.mkv")
    assert matches_query("House of the Dragon", "House.of.the.Dragon.S01E05.mkv")
    assert not matches_query("House of the Dragon", "The Dragon Prince S01E05.mkv")


def test_alias_titles_and_multi_title_matching():
    aliases = [{"from": "The Sleepers", "to": "Bez vědomí"}]
    assert alias_titles("The Sleepers", aliases) == ["Bez vědomí"]
    # Sonarr drops the leading article: "The Sleepers" is searched as "Sleepers".
    assert alias_titles("Sleepers", aliases) == ["Bez vědomí"]
    assert alias_titles("Something Else", aliases) == []
    # A CZ file matches via the alias title even though the query is English.
    titles = ["The Sleepers"] + alias_titles("The Sleepers", aliases)
    assert matches_query(titles, "Bez.vedomi.S01E01.2019.CZ.mkv")
    assert not matches_query(["The Sleepers"], "Bez.vedomi.S01E01.2019.CZ.mkv")
    # Episode detected after the matched (Czech) title.
    assert file_episode(titles, "Bez.vedomi.S01E01.2019.CZ.mkv") == 1


def test_expand_titles_uses_tmdb(monkeypatch):
    import asyncio

    from app import torznab
    from app.settings import settings

    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        return ("The Sleepers", "Bez vědomí") if tvdbid == "358583" else None

    async def by_name(token, kind, q):
        return ("The Sleepers", "Bez vědomí") if "sleepers" in q.lower() else None

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)

    # exact id lookup wins; the original title is added and display is canonical
    titles, display = asyncio.run(
        torznab.expand_titles("tvsearch", "Sleepers", "5000", tvdbid="358583"))
    assert "Bez vědomí" in titles and display == "The Sleepers"
    # fuzzy name lookup when no id
    titles, display = asyncio.run(torznab.expand_titles("tvsearch", "Sleepers", "5000"))
    assert "Bez vědomí" in titles and display == "The Sleepers"
    # no token -> just the query
    monkeypatch.setattr(settings, "tmdb_token", "")
    assert asyncio.run(torznab.expand_titles("tvsearch", "Sleepers", "5000")) == (["Sleepers"], "Sleepers")


def test_release_title_asciified():
    # diacritics transliterated so Prowlarr's download header stays latin-1 safe.
    assert release_title("The Sleepers", "1", "2", "Bez vědomí.S01E02.mkv") == \
        "The Sleepers S01E02 - Bez vedomi"


def test_file_episode():
    assert file_episode("Skvrna", "Skvrna 05 - Bestie (Cajda).mp4") == 5
    assert file_episode("Skvrna", "Skvrna 01 - Pohreb.mkv") == 1
    assert file_episode("Zaklinac", "Zaklinac.S01E03.1080p.mkv") == 3
    assert file_episode("Zaklinac", "Zaklinac 1x07 dabing.avi") == 7
    # 1080/2160 must not be mistaken for an episode.
    assert file_episode("Skvrna", "Skvrna - Bestie 1080p.mkv") is None


def test_search_filters_garbage_and_wrong_episode(client, fake_webshare):
    fake_webshare.fuzzy = True  # Webshare returns everything, like real fulltext
    fake_webshare.results = [
        SearchResult("good", "Skvrna 05 - Bestie.mkv", 800_000_000),
        SearchResult("otherep", "Skvrna 01 - Pohreb.mkv", 900_000_000),  # wrong episode
        SearchResult("wwe", "WWE.Monday.Night.Raw.S34E01.2160p.mkv", 5_000_000_000),
        SearchResult("planet", "Our.Planet.2019.S01E05.2160p.mkv", 4_000_000_000),
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Skvrna", "season": "1", "ep": "5",
    })
    root = ET.fromstring(resp.content)
    titles = [i.findtext("title") for i in root.findall("channel/item")]
    # Garbage AND the wrong episode dropped; only the real S01E05 survives
    # (resolution appended from file_info, fake default 1080).
    assert titles == ["Skvrna S01E05 - Skvrna 05 - Bestie 1080p"]


def test_caps(client):
    resp = client.get("/torznab/api", params={"t": "caps", "apikey": "testkey"})
    assert resp.status_code == 200
    root = ET.fromstring(resp.content)
    assert root.tag == "caps"
    tv = root.find("searching/tv-search")
    assert tv.get("available") == "yes"
    assert "season" in tv.get("supportedParams")


def test_invalid_apikey(client):
    resp = client.get("/torznab/api", params={"t": "caps", "apikey": "wrong"})
    root = ET.fromstring(resp.content)
    assert root.tag == "error"
    assert root.get("code") == "100"


def test_tvsearch_returns_items(client, fake_webshare):
    fake_webshare.results = [
        SearchResult("id1", "Zaklinac.S01E05.1080p.CZ.mkv", 4_000_000_000),
        SearchResult("id2", "Zaklinac 1x05 dabing.avi", 1_500_000_000),
        SearchResult("id3", "Zaklinac.S01E05.titulky.srt", 50_000),  # not video
        SearchResult("id4", "Zaklinac.S01E05.locked.mkv", 3_000_000_000, password=True),
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Zaklinac", "season": "1", "ep": "5",
    })
    assert resp.status_code == 200
    root = ET.fromstring(resp.content)
    items = root.findall("channel/item")
    titles = [i.findtext("title") for i in items]
    # Titles are normalized with the requested SxxEyy so *arr can parse them; the
    # filename's own episode marker is stripped to avoid a duplicate, and the one
    # without a resolution gets one appended from file_info (fake=1080).
    assert titles == [
        "Zaklinac S01E05 - Zaklinac.1080p.CZ",
        "Zaklinac S01E05 - Zaklinac dabing 1080p",
    ]

    item = items[0]
    assert item.findtext("size") == "4000000000"
    enclosure = item.find("enclosure")
    assert "/torznab/nzb/id1" in enclosure.get("url")
    assert "apikey=testkey" in enclosure.get("url")
    # nzbname carries the normalized title so the download folder gets SxxEyy.
    assert "nzbname=Zaklinac" in enclosure.get("url")
    cats = {a.get("name"): a.get("value") for a in item.findall(f"{TZNS}attr")}
    assert cats["category"] == "5000"
    # Newznab namespace attrs must be present too (indexer is added as Newznab).
    ncats = {a.get("name"): a.get("value") for a in item.findall(f"{NZNS}attr")}
    assert ncats["category"] == "5000"


def test_empty_query_returns_placeholder_in_requested_category(client):
    """Sonarr's indexer test sends an empty RSS query and rejects zero results.
    We return one unparseable placeholder in the requested category instead."""
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "cat": "5000,5040",
    })
    root = ET.fromstring(resp.content)
    items = root.findall("channel/item")
    assert len(items) == 1
    ncats = {a.get("name"): a.get("value") for a in items[0].findall(f"{NZNS}attr")}
    assert ncats["category"] == "5000"
    # Title carries no SxxExx / year, so the *arr parser can never match it.
    title = items[0].findtext("title")
    assert "S0" not in title and "x0" not in title


def test_feed_download_url_is_ascii(client, fake_webshare):
    """The download URL must carry no encoded diacritics: Prowlarr proxies via a
    302 and re-emits the decoded URL raw in the Location header, which rejects
    non-latin-1 chars ("Invalid non-ASCII in header 0x011B")."""
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("z1", "Bez vědomí.S01E02.mkv", 500)]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Bez vedomi", "season": "1", "ep": "2",
    })
    url = ET.fromstring(resp.content).find("channel/item/enclosure").get("url")
    assert "%C4%9B" not in url and "vedomi" in url  # transliterated, not encoded


def test_search_labels_quality_from_fileinfo(client, fake_webshare):
    """A CZ file with no resolution in its name gets one appended from
    file_info's height, so *arr can detect the quality."""
    fake_webshare.results = [SearchResult("q1", "Skvrna 05 - Bestie.mp4", 500_000_000)]
    fake_webshare.file_infos = {"q1": {"length": 2600, "width": 1920, "height": 1080,
                                       "format": "H264", "type": "mp4"}}
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Skvrna", "season": "1", "ep": "5",
    })
    title = ET.fromstring(resp.content).findtext("channel/item/title")
    assert title == "Skvrna S01E05 - Skvrna 05 - Bestie 1080p"


def test_search_keeps_existing_resolution(client, fake_webshare):
    # Name already has a resolution -> no file_info lookup, left as-is.
    fake_webshare.results = [SearchResult("q2", "Skvrna 05 - Bestie 720p.mkv", 500_000_000)]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Skvrna", "season": "1", "ep": "5",
    })
    title = ET.fromstring(resp.content).findtext("channel/item/title")
    assert title == "Skvrna S01E05 - Skvrna 05 - Bestie 720p"


def test_nzb_download(client):
    resp = client.get("/torznab/nzb/id1", params={
        "apikey": "testkey", "name": "Zaklinac.S01E05.1080p.CZ.mkv", "size": "4000000000",
    })
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-nzb")
    assert b"websharr_ident" in resp.content
    assert b"id1" in resp.content


def test_nzb_download_non_ascii_name(client):
    # CZ names ("Řád") must be transliterated to pure ASCII in the header —
    # Prowlarr chokes on a diacritic filename* (RFC 5987), so we don't send one.
    resp = client.get("/torznab/nzb/epk1", params={
        "apikey": "testkey",
        "name": "Skvrna 06 - Řád (Cajda).mp4",
        "size": "578013708",
        "nzbname": "Skvrna S01E06 - Skvrna 06 - Řád (Cajda) 1080p",
    })
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert "filename*" not in cd                    # no RFC 5987 form
    assert "Rad" in cd and "Řád" not in cd          # transliterated
    cd.encode("ascii")                              # pure ASCII, header-safe
