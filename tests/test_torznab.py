import re
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
        return ("The Sleepers", "Bez vědomí", "cs", (), 2019) if tvdbid == "358583" else None

    async def by_name(token, kind, q):
        return ("The Sleepers", "Bez vědomí", "cs", (), 2019) if "sleepers" in q.lower() else None

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)

    # exact id lookup wins; the original title is added, display is canonical,
    # and the original language maps to the *arr language name.
    titles, display, language, czech, year = asyncio.run(
        torznab.expand_titles("tvsearch", "Sleepers", "5000", tvdbid="358583"))
    assert "Bez vědomí" in titles and display == "The Sleepers" and language == "Czech"
    # fuzzy name lookup when no id
    titles, display, language, czech, year = asyncio.run(
        torznab.expand_titles("tvsearch", "Sleepers", "5000"))
    assert "Bez vědomí" in titles and display == "The Sleepers" and language == "Czech"
    # no token -> just the query, no language
    monkeypatch.setattr(settings, "tmdb_token", "")
    assert asyncio.run(torznab.expand_titles("tvsearch", "Sleepers", "5000")) == \
        (["Sleepers"], "Sleepers", "", [], 0)


def test_expand_titles_id_only_uses_canonical(monkeypatch):
    """An automatic ID search carries no q; the canonical (here Czech) title from
    the TMDB id lookup must still become the search term, with no empty query."""
    import asyncio
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        # CZ-origin show: name == original_name, so original comes back empty.
        return ("Devadesátky", "", "cs", (), 2022) if tvdbid == "414489" else None

    async def by_name(token, kind, q):
        return None

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    titles, display, language, czech, year = asyncio.run(
        torznab.expand_titles("tvsearch", "", "5000", tvdbid="414489", imdbid="16532444"))
    assert titles == ["Devadesátky"]        # canonical name searched, no empty string
    assert display == "Devadesátky" and language == "Czech" and czech == []
    assert year == 2022


def test_expand_titles_adds_czech_alt_titles(monkeypatch):
    """An English-origin show dubbed under Czech names (DuckTales): the CZ
    alternative titles from TMDB become extra search terms, the display stays
    the canonical English name."""
    import asyncio
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        if tvdbid == "75931":
            return ("DuckTales", "", "en", ("Kačeří příběhy", "My z Kačerova"), 1987)
        return None

    async def by_name(token, kind, q):
        return None

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    titles, display, language, czech, year = asyncio.run(
        torznab.expand_titles("tvsearch", "DuckTales", "5000", tvdbid="75931"))
    assert titles == ["DuckTales", "Kačeří příběhy", "My z Kačerova"]
    assert display == "DuckTales" and language == "English"
    assert czech == ["Kačeří příběhy", "My z Kačerova"] and year == 1987
    # A dubbed file named after the Czech title now matches and parses.
    assert torznab.matches_query(titles, "Kaceri.pribehy.S01E01.CZ.Dabing.mkv")
    assert torznab.file_episode(titles, "Kaceri.pribehy.S01E01.CZ.Dabing.mkv") == 1


def test_imdb_id_normalisation():
    from app.tmdb import _imdb_id
    assert _imdb_id("16532444") == "tt16532444"
    assert _imdb_id("tt16532444") == "tt16532444"
    assert _imdb_id("") == "" and _imdb_id(None) == ""


def test_dub_language_and_lang_name():
    from app.torznab import dub_language, lang_name
    assert dub_language("Bez.vedomi.S01E01.CZ.Dabing.1080p.mkv") == "Czech"
    assert dub_language("Bez.vedomi.S01E01.dabing.mkv") == "Czech"  # bare dabing = Czech
    assert dub_language("Futurama FHD 1080p CZ.mkv") == "Czech"
    assert dub_language("Film.2020.SK.dabing.mkv") == "Slovak"
    assert dub_language("Film.2020.CZ.SK.dabing.mkv") == "Czech"    # CZ present -> Czech
    assert dub_language("Movie.2020.1080p.CZ.titulky.mkv") == ""    # subtitles, not a dub
    assert dub_language("Movie.2020.1080p.BluRay.x264.mkv") == ""
    # A full CZECH/SLOVAK word marks the audio language (scene convention)...
    assert dub_language("DuckTales.S01E01.SLOVAK.1080p.AI.WEB.H264-GRP.mkv") == "Slovak"
    assert dub_language("Movie.2020.CZECH.1080p.WEB.mkv") == "Czech"
    # ...including bare CZ/SK markers, unless the name explicitly marks subtitles.
    assert dub_language("Movie.2020.CZECH.subs.1080p.mkv") == ""
    assert dub_language("Movie.2020.1080p.CZ.mkv") == "Czech"
    assert dub_language("Movie.2020.1080p.SK.mkv") == "Slovak"
    assert lang_name("en") == "English"
    assert lang_name("cs") == "Czech"
    assert lang_name("") == "" and lang_name("xx") == ""


def test_feed_tags_language_original_and_dub(client, fake_webshare, monkeypatch):
    """The feed tags each item with a newznab `language`: the title's original
    language, overridden to Czech/Slovak for a dubbed file."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")

    async def by_name(token, kind, q):
        return ("The Sleepers", "", "en", (), 2019)  # pretend an English-original title

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        return None

    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    fake_webshare.results = [
        SearchResult("o1", "Sleepers.S01E01.1080p.mkv", 2_000_000_000),
        SearchResult("o2", "Sleepers.S01E01.CZ.Dabing.1080p.mkv", 2_100_000_000),
        SearchResult("o3", "Sleepers.S01E01.FHD.1080p.CZ.mkv", 2_200_000_000),
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Sleepers", "season": "1", "ep": "1"})
    root = ET.fromstring(resp.content)
    by_ident = {}
    for it in root.findall("channel/item"):
        attrs = {a.get("name"): a.get("value") for a in it.findall(f"{NZNS}attr")}
        by_ident[it.findtext("title")] = attrs.get("language")
    langs = list(by_ident.values())
    assert "English" in langs   # original-audio file tagged with the original language
    assert langs.count("Czech") == 2  # explicit dabing and bare CZ are both Czech audio


def test_feed_tags_czech_for_file_named_after_czech_title(client, fake_webshare, monkeypatch):
    """A file named after the Czech dub title is a Czech release even without a
    "dabing" marker in the name — it must not inherit the English original tag."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")

    async def by_name(token, kind, q):
        return ("DuckTales", "", "en", ("Kačeří příběhy",), 1987)

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        return None

    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    fake_webshare.fuzzy = True  # real Webshare fulltext matches diacritics-insensitively
    fake_webshare.results = [
        SearchResult("d1", "DuckTales.S01E01.1080p.WEB.mkv", 2_000_000_000),
        SearchResult("d2", "Kaceri pribehy S01E01 Neopoustejte lod.mkv", 2_100_000_000),
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "DuckTales", "season": "1", "ep": "1"})
    root = ET.fromstring(resp.content)
    by_title = {}
    for it in root.findall("channel/item"):
        attrs = {a.get("name"): a.get("value") for a in it.findall(f"{NZNS}attr")}
        by_title[it.findtext("title")] = attrs.get("language")
    assert sorted(by_title.values()) == ["Czech", "English"]
    for title, lang in by_title.items():
        if "Kaceri" in title:
            assert lang == "Czech"
            # ...and gains the "CZ" marker its name lacks, for title-based
            # custom formats; the English original is left alone.
            assert title.endswith(" CZ")
        else:
            assert not title.endswith(" CZ")


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


def test_year_conflict():
    from app.torznab import year_conflict
    # The 2017 reboot file must not satisfy the 1987 series (same Czech name).
    assert year_conflict("Kaceri pribehy 2017 04 Slamastika s desetnikem.mkv", 1987)
    assert not year_conflict("My z Kacerova (1987) Studena kachna 960p.mkv", 1987)
    assert not year_conflict("Kaceri pribehy 05 - Bez roku.mkv", 1987)   # no year token
    assert not year_conflict("DuckTales.S01E02.1080p.mkv", 1987)         # 1080 != year
    assert not year_conflict("Film.2018.1080p.mkv", 2019)                # ±1 tolerated
    assert not year_conflict("Kaceri pribehy 2017 04.mkv", 0)            # unknown year


def test_file_marker_reads_season():
    from app.torznab import file_marker
    # SxxEyy and 1x05 carry the season; a bare episode number does not.
    assert file_marker("Zaklinac", "Zaklinac.S02E03.1080p.mkv") == (2, 3)
    assert file_marker("Zaklinac", "Zaklinac 1x07 dabing.avi") == (1, 7)
    assert file_marker("Skvrna", "Skvrna 05 - Bestie.mp4") == (None, 5)
    assert file_marker("Futurama", "Futurama Fialovy Trpaslik special 06.mkv") == (0, 6)
    assert file_marker("Skvrna", "Skvrna - Bestie 1080p.mkv") == (None, None)


def test_search_drops_other_season_with_same_episode(client, fake_webshare, monkeypatch):
    """A "DuckTales S01E02" search must not return "DuckTales.S02E02..." — the
    episode number matches but the season does not (the real-life mis-grab:
    Webshare fulltext matched the file on the show name alone, the old filter
    only compared episode numbers, and the title rewrite then hid the S02)."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.fuzzy = True  # like Webshare: everything containing any term
    fake_webshare.results = [
        SearchResult("k1", "DuckTales.S01E02.Wronguay.CZECH.1080p.mkv", 2_000_000_000),
        SearchResult("k2", "DuckTales.S02E02.The.Duck.Who.Would.Be.King.CZECH.1080p.mkv", 2_100_000_000),
        SearchResult("k3", "DuckTales 2x02 dabing.avi", 1_000_000_000),
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "DuckTales", "season": "1", "ep": "2"})
    root = ET.fromstring(resp.content)
    titles = [it.findtext("title") for it in root.findall("channel/item")]
    assert len(titles) == 1 and "Wronguay" in titles[0]


def test_season_search_returns_individual_episodes(client, fake_webshare, monkeypatch):
    """Sonarr's automatic search for a season with several missing episodes
    sends season=N without ep. Webshare has no season packs, so every file must
    be released under its *own* SxxEyy — labelling them all "Futurama S08 - ..."
    made each look like a (bogus) season pack and Sonarr grabbed nothing."""
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("e4", "Futurama s08e04 - Cesta k parazitum 1080p.mkv", 2_000_000_000),
        SearchResult("e2", "Futurama S08E02 Bahnem zapomenute deti 1080p WEB-DL.mkv", 1_900_000_000),
        SearchResult("s7", "Futurama S07E04 1080p.mkv", 1_800_000_000),       # other season
        SearchResult("noep", "Futurama - bonusy 1080p.mkv", 1_700_000_000),   # no episode
        SearchResult("bare", "Futurama 05 - neco 1080p.mkv", 1_600_000_000),  # bare no., S08
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Futurama", "season": "8"})
    root = ET.fromstring(resp.content)
    titles = [i.findtext("title") for i in root.findall("channel/item")]
    assert titles == [
        "Futurama S08E04 - Futurama - Cesta k parazitum 1080p",
        "Futurama S08E02 - Futurama Bahnem zapomenute deti 1080p WEB-DL",
    ]


def test_season_one_search_accepts_bare_episode_numbers(client, fake_webshare, monkeypatch):
    # CZ season-1 convention "Skvrna 05 - Bestie": the bare number is the episode.
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("b5", "Skvrna 05 - Bestie 1080p.mkv", 900_000_000),
        SearchResult("b1", "Skvrna 01 - Pohreb 1080p.mkv", 800_000_000),
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Skvrna", "season": "1"})
    root = ET.fromstring(resp.content)
    titles = [i.findtext("title") for i in root.findall("channel/item")]
    assert titles == [
        "Skvrna S01E05 - Skvrna 05 - Bestie 1080p",
        "Skvrna S01E01 - Skvrna 01 - Pohreb 1080p",
    ]


def test_regular_episode_search_drops_explicit_special(client, fake_webshare, monkeypatch):
    """A Webshare filename labelled `special 03` is S00E03, not whichever
    regular-season E03 Sonarr happened to request."""
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("regular", "Futurama.S04E03.1080p.CZ.mkv", 1_000_000_000),
        SearchResult("special", "Futurama Milion A Jedno Chapadlo FHD 1080p CZ special 03.mkv",
                     2_000_000_000),
    ]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Futurama", "season": "4", "ep": "3"})
    root = ET.fromstring(resp.content)
    titles = [it.findtext("title") for it in root.findall("channel/item")]
    assert titles == ["Futurama S04E03 - Futurama.1080p.CZ"]


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
    # Prowlarr only forwards the ids a caps lists: Sonarr v4 sends tmdbid too
    assert {"tvdbid", "imdbid", "tmdbid"} <= set(tv.get("supportedParams").split(","))


def test_tvsearch_by_tmdbid_only(client, fake_webshare, monkeypatch):
    """A series search carrying only a TMDB id still resolves the title by id."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")
    seen = {}

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        seen.update(kind=kind, tmdbid=tmdbid)
        return ("Bluey", "", "en", (), 2018)

    async def by_name(token, kind, q):
        return None

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    fake_webshare.results = [SearchResult("b1", "Bluey S01E01 1080p CZ.mkv", 300_000_000)]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "tmdbid": "82728", "season": "1", "ep": "1"})
    assert seen == {"kind": "tv", "tmdbid": "82728"}
    assert ET.fromstring(resp.content).findtext("channel/item/title").startswith("Bluey S01E01")


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


def test_unresolvable_id_search_returns_nothing_not_the_placeholder(client, fake_webshare, monkeypatch):
    """A search carrying an id TMDB can't resolve has nothing to look for, but
    it is a real search: no results, not the RSS-test placeholder (which would
    show up as an odd row in interactive search for that show)."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", nothing)
    monkeypatch.setattr(torznab, "tmdb_lookup", nothing)
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "tvdbid": "72860", "season": "1", "ep": "2"})
    root = ET.fromstring(resp.content)
    assert root.tag == "rss" and root.findall("channel/item") == []


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


def test_resolution_class():
    """The appended label must be a resolution *arr knows — a literal "384p" or
    "800p" parses as Unknown quality and the release is rejected outright."""
    from app.torznab import resolution_class
    assert resolution_class(1920, 1080) == 1080
    assert resolution_class(1920, 800) == 1080    # 2.39:1 crop of a 1080p source
    assert resolution_class(1440, 1080) == 1080   # 4:3 at 1080p
    assert resolution_class(3840, 1600) == 2160
    assert resolution_class(1280, 534) == 720
    assert resolution_class(768, 576) == 576
    assert resolution_class(720, 540) == 540
    assert resolution_class(640, 480) == 480
    assert resolution_class(640, 384) == 480      # old DVD-rip AVI
    assert resolution_class(320, 240) == 360
    assert resolution_class(0, 384) == 480        # width unknown
    assert resolution_class(0, 0) == 0


def _http_error(status):
    import httpx
    req = httpx.Request("POST", "https://webshare.cz/api/file_info/")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(status, request=req))


class _FlakyClient:
    def __init__(self, fail_times, status=403):
        self.calls = 0
        self.fail_times = fail_times
        self.status = status

    async def file_info(self, ident):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _http_error(self.status)
        return {"length": 5400, "width": 1920, "height": 1080}


def test_file_info_retries_on_403_and_caches():
    import asyncio
    from app import torznab
    c = _FlakyClient(fail_times=2)
    info = asyncio.run(torznab._file_info(c, "x1"))
    assert info["height"] == 1080 and c.calls == 3        # two 403s, then success
    asyncio.run(torznab._file_info(c, "x1"))
    assert c.calls == 3                                    # served from the cache


def test_file_info_fails_open_after_retries():
    import asyncio
    from app import torznab
    c = _FlakyClient(fail_times=99)
    assert asyncio.run(torznab._file_info(c, "x2")) == {}
    assert c.calls == 4                                    # 1 try + 3 retries
    c404 = _FlakyClient(fail_times=99, status=404)
    assert asyncio.run(torznab._file_info(c404, "x3")) == {}
    assert c404.calls == 1                                 # not retryable


def test_search_labels_odd_height_with_known_class(client, fake_webshare):
    fake_webshare.results = [SearchResult("q3", "Skvrna 05 - Bestie.avi", 200_000_000)]
    fake_webshare.file_infos = {"q3": {"length": 1700, "width": 640, "height": 384,
                                       "format": "MPEG4", "type": "avi"}}
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Skvrna", "season": "1", "ep": "5"})
    title = ET.fromstring(resp.content).findtext("channel/item/title")
    assert title == "Skvrna S01E05 - Skvrna 05 - Bestie 480p"


def test_search_keeps_existing_resolution(client, fake_webshare):
    # Name already has a resolution -> no file_info lookup, left as-is.
    fake_webshare.results = [SearchResult("q2", "Skvrna 05 - Bestie 720p.mkv", 500_000_000)]
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Skvrna", "season": "1", "ep": "5",
    })
    title = ET.fromstring(resp.content).findtext("channel/item/title")
    assert title == "Skvrna S01E05 - Skvrna 05 - Bestie 720p"


def test_audio_track_language_tags_czech_dub_without_name_marker(client, fake_webshare, monkeypatch):
    """A real Czech dub whose name carries no CZ/dabing marker ("... 1080p
    WEB-DL prima+") is recognised from its audio track (Webshare file_info):
    tagged Czech, and the title gains a "CZ" marker for title-based custom
    formats. An English-audio file with a Czech episode title stays original."""
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.results = [
        SearchResult("dub", "Futurama S08E02 Bahnem zapomenute deti 1080p WEB-DL prima+.mkv",
                     1_500_000_000),
        SearchResult("eng", "Futurama s08e02 - Bahnem zapomenute deti 2160p.mkv", 1_400_000_000),
        SearchResult("subs", "Futurama S08E02 1080p CZ titulky.mkv", 1_300_000_000),
    ]
    info = {"length": 1400, "width": 1920, "height": 1080, "format": "H264", "type": "mkv"}
    fake_webshare.file_infos = {
        "dub": {**info, "audio_languages": ["CZE"]},
        "eng": {**info, "audio_languages": ["ENG"]},
        "subs": {**info, "audio_languages": ["ENG"]},
    }
    resp = client.get("/torznab/api", params={
        "t": "tvsearch", "apikey": "testkey", "q": "Futurama", "season": "8", "ep": "2"})
    got = {}
    for it in ET.fromstring(resp.content).findall("channel/item"):
        attrs = {a.get("name"): a.get("value") for a in it.findall(f"{NZNS}attr")}
        got[it.findtext("guid")] = (it.findtext("title"), attrs.get("language"))
    assert got["websharr-dub"] == (
        "Futurama S08E02 - Futurama Bahnem zapomenute deti 1080p WEB-DL prima+ CZ", "Czech")
    assert got["websharr-eng"] == (
        "Futurama S08E02 - Futurama - Bahnem zapomenute deti 2160p", None)
    assert got["websharr-subs"] == ("Futurama S08E02 - Futurama 1080p CZ titulky", None)


def test_audio_language_mapping():
    from app.torznab import audio_language
    assert audio_language(["CZE", "ENG"]) == "Czech"
    assert audio_language(["cze"]) == "Czech"
    assert audio_language(["SLO"]) == "Slovak"
    assert audio_language(["SLK", "CZE"]) == "Czech"
    assert audio_language(["ENG"]) == ""
    assert audio_language([]) == ""


def test_pubdate_is_stable_per_file(client, fake_webshare):
    """Sonarr matches a usenet blocklist entry on title + *exact* publish date.
    A pubDate of "now" changed on every search, so a failed release was never
    recognised as blocklisted and got re-grabbed forever (40x for one dead
    Futurama file). The date must be the same each time for the same file."""
    import email.utils
    import time
    fake_webshare.results = [
        SearchResult("p1", "Skvrna 05 - Bestie 720p.mkv", 500_000_000),
        SearchResult("p2", "Skvrna 05 - Bestie 1080p.mkv", 900_000_000),
    ]
    params = {"t": "tvsearch", "apikey": "testkey", "q": "Skvrna", "season": "1", "ep": "5"}

    def dates():
        root = ET.fromstring(client.get("/torznab/api", params=params).content)
        return {i.findtext("guid"): i.findtext("pubDate") for i in root.findall("channel/item")}

    first = dates()
    real = time.time
    try:
        time.time = lambda: real() + 3600  # a later search
        second = dates()
    finally:
        time.time = real
    assert first == second
    assert first["websharr-p1"] != first["websharr-p2"]
    for d in first.values():
        assert email.utils.parsedate_to_datetime(d).timestamp() < real()


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


def test_runtime_mismatch():
    from app.torznab import runtime_mismatch
    assert not runtime_mismatch(95 * 60, 100, "movie")      # normal cut
    assert not runtime_mismatch(170 * 60, 100, "movie")     # long unmarked cut
    assert runtime_mismatch(22 * 60, 81, "movie")           # short special, not the feature
    assert runtime_mismatch(0 + 60 * 5, 44, "tv")           # 5-minute excerpt
    assert not runtime_mismatch(88 * 60, 44, "tv")          # double episode
    assert runtime_mismatch(60 * 60, 7, "tv")               # hour-long doc vs 7-min episode
    assert not runtime_mismatch(0, 100, "movie")            # unknown length
    assert not runtime_mismatch(3600, 0, "movie")           # unknown runtime


def test_runtime_mismatch_editions_multi_episodes_and_fallbacks():
    from app.torznab import runtime_mismatch
    # a named edition may run any amount longer, but is never a 22-minute file
    assert runtime_mismatch(260 * 60, 100, "movie")
    for name in ("Film 2001 Extended 1080p.mkv", "Film.2001.Directors.Cut.mkv",
                 "Film 2001 Director's Cut.mkv", "Film 2001 UNCUT.mkv",
                 "Film 2001 Special Edition.mkv", "Film 2001 prodloužená verze.mkv"):
        assert not runtime_mismatch(260 * 60, 100, "movie", name=name), name
    assert runtime_mismatch(22 * 60, 100, "movie", name="Film 2001 Extended.mkv")
    # multi-episode files run several episodes long
    for name in ("Bluey S01E01E02E03.mkv", "Bluey S01E01-E03.mkv", "Bluey S01E01-03.mkv"):
        assert not runtime_mismatch(25 * 60, 7, "tv", name=name), name
    assert runtime_mismatch(25 * 60, 7, "tv", name="Bluey S01E01 720p.mkv")
    # a show-level fallback never rejects a long episode, only a far shorter file
    assert not runtime_mismatch(120 * 60, 40, "tv", exact=False)
    assert not runtime_mismatch(20 * 60, 40, "tv", exact=False)
    assert runtime_mismatch(5 * 60, 40, "tv", exact=False)


def test_probe_state_is_fresh_in_every_test():
    """Both autouse fixtures apply: the probe cache, limiter and back-off are
    reset (a cached file_info leaked into the next test before), and TMDB
    runtimes are never fetched."""
    import asyncio
    from app import torznab
    assert torznab._probe_cache == {} and torznab._probe_sem is None
    assert torznab._PROBE_RETRIES == (0, 0, 0)
    assert asyncio.run(torznab.tmdb_runtime("tok", "movie", tmdbid="1")) == (0, False)


def _enable_runtime_check(monkeypatch, result, minutes, exact=True, calls=None):
    """Turn the runtime check on and fake TMDB: the id lookup answers `result`,
    the runtime lookup (minutes, exact) — recording each call in `calls`."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")
    monkeypatch.setattr(settings, "runtime_check", True)

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        return result

    async def by_name(token, kind, q):
        return None

    async def runtime(token, kind, tmdbid=None, imdbid=None, tvdbid=None, season=None, ep=None):
        if calls is not None:
            calls.append((kind, season, ep))
        return minutes, exact

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    monkeypatch.setattr(torznab, "tmdb_runtime", runtime)


_INFO = {"width": 1920, "height": 1080, "format": "H264", "type": "mkv"}
_TOY_STORY = ("Toy Story", "", "en", (), 1995)


def _feed(client, **params):
    resp = client.get("/torznab/api", params={"apikey": "testkey", **params})
    return [i.findtext("guid").removeprefix("websharr-")
            for i in ET.fromstring(resp.content).findall("channel/item")]


def test_runtime_check_is_off_by_default(client, fake_webshare, monkeypatch):
    """Off (the default), an id search behaves as without the feature: no TMDB
    runtime lookup, no extra probes or pages, nothing dropped by length."""
    from app import torznab
    from app.settings import settings
    assert settings.runtime_check is False
    calls = []
    _enable_runtime_check(monkeypatch, _TOY_STORY, 81, calls=calls)
    monkeypatch.setattr(settings, "runtime_check", False)
    probed = []
    real = fake_webshare.file_info

    async def file_info(ident):
        probed.append(ident)
        return await real(ident)

    fake_webshare.file_info = file_info
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("named", "Toy Story 1995 1080p CZ.mkv", 2_000_000_000),
        SearchResult("bare", "Toy Story 1995.avi", 1_000_000_000),
    ]
    fake_webshare.file_infos = {"named": {**_INFO, "length": 60}, "bare": {**_INFO, "length": 60}}
    assert _feed(client, t="movie", tmdbid="862", cat="2000", limit="1") == ["named"]
    assert calls == []                                  # no runtime lookup
    assert probed == []                                 # the named file needs no probe
    assert fake_webshare.searches == [("Toy Story", 1, 0)]
    assert torznab._probe_cache == {}


def test_movie_search_drops_files_far_off_the_tmdb_runtime(client, fake_webshare, monkeypatch):
    """An id search knows the movie's runtime; a file with the right name but a
    fraction of the length is a special/excerpt ("Toy Story That Time Forgot"
    for Toy Story), not the movie. A right length proves nothing: a sequel of
    the same length stays dropped by its year."""
    _enable_runtime_check(monkeypatch, _TOY_STORY, 81)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("full", "Toy Story 1995 1080p CZ.mkv", 4_000_000_000),
        SearchResult("spec", "Toy Story That Time Forgot 1080p CZ.mkv", 1_000_000_000),
        SearchResult("unk", "Toy Story 1995 CZ dabing.avi", 700_000_000),
        SearchResult("seq", "Toy Story 2 1999 1080p CZ.mkv", 3_000_000_000),
    ]
    fake_webshare.file_infos = {"full": {**_INFO, "length": 81 * 60},
                                "spec": {**_INFO, "length": 22 * 60},
                                "unk": {**_INFO, "length": 0},
                                "seq": {**_INFO, "length": 81 * 60}}
    guids = _feed(client, t="movie", tmdbid="862", cat="2000")
    assert "full" in guids and "unk" in guids           # unknown length is kept
    assert "spec" not in guids and "seq" not in guids


def test_runtime_check_keeps_long_editions(client, fake_webshare, monkeypatch):
    """A 170-minute cut of a 100-minute film passes, and a named edition passes
    however long it is."""
    _enable_runtime_check(monkeypatch, ("Film", "", "en", (), 2001), 100)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("ext", "Film 2001 Extended Edition 1080p.mkv", 9_000_000_000),
        SearchResult("long", "Film 2001 1080p.mkv", 8_000_000_000),
        SearchResult("theat", "Film 2001 720p.mkv", 4_000_000_000),
    ]
    fake_webshare.file_infos = {"ext": {**_INFO, "length": 260 * 60},
                                "long": {**_INFO, "length": 170 * 60},
                                "theat": {**_INFO, "length": 100 * 60}}
    assert _feed(client, t="movie", tmdbid="1", cat="2000") == ["ext", "long", "theat"]


def test_episode_search_drops_other_content_by_runtime(client, fake_webshare, monkeypatch):
    """A 7-minute Bluey episode vs an hour-long file that merely carries the
    right marker: the episode's own runtime tells them apart. A multi-episode
    file is not rejected for running several episodes long."""
    _enable_runtime_check(monkeypatch, ("Bluey", "", "en", (), 2018), 7)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("ok", "Bluey S01E01 Magic Xylophone 1080p CZ.mkv", 300_000_000),
        SearchResult("long", "Bluey S01E01 1080p BluRay Remux CZ.mkv", 10_700_000_000),
        SearchResult("multi", "Bluey S01E01-E03 1080p CZ.mkv", 900_000_000),
    ]
    fake_webshare.file_infos = {"ok": {**_INFO, "length": 7 * 60},
                                "long": {**_INFO, "length": 59 * 60},
                                "multi": {**_INFO, "length": 21 * 60}}
    guids = _feed(client, t="tvsearch", tvdbid="353546", season="1", ep="1")
    assert sorted(guids) == ["multi", "ok"]


def test_season_search_is_lenient_with_a_show_level_runtime(client, fake_webshare, monkeypatch):
    """A season search only has the show's typical episode length: a 120-minute
    episode of a 40-minute show passes, a 5-minute clip still doesn't."""
    _enable_runtime_check(monkeypatch, ("Show", "", "en", (), 2018), 40, exact=False)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("e1", "Show S01E01 1080p.mkv", 6_000_000_000),
        SearchResult("e2", "Show S01E02 1080p.mkv", 2_000_000_000),
        SearchResult("e3", "Show S01E03 1080p.mkv", 100_000_000),
    ]
    fake_webshare.file_infos = {"e1": {**_INFO, "length": 120 * 60},
                                "e2": {**_INFO, "length": 40 * 60},
                                "e3": {**_INFO, "length": 5 * 60}}
    assert _feed(client, t="tvsearch", tvdbid="1", season="1") == ["e1", "e2"]


def test_runtime_check_skips_specials(client, fake_webshare, monkeypatch):
    """Season 0 (specials) has no meaningful runtime: no lookup, nothing dropped."""
    calls = []
    _enable_runtime_check(monkeypatch, ("Bluey", "", "en", (), 2018), 7, calls=calls)
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("sp", "Bluey S00E01 Special 1080p CZ.mkv", 2_000_000_000)]
    fake_webshare.file_infos = {"sp": {**_INFO, "length": 60 * 60}}
    assert _feed(client, t="tvsearch", tvdbid="353546", season="0", ep="1") == ["sp"]
    assert calls == []


def test_runtime_check_refills_from_the_next_page(client, fake_webshare, monkeypatch):
    """Files dropped by length are replaced from the next Webshare page: with
    limit=2, two short files followed by two good ones still give two results."""
    _enable_runtime_check(monkeypatch, _TOY_STORY, 81)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("s1", "Toy Story 1995 extra A 1080p CZ.mkv", 9_000_000_000),
        SearchResult("s2", "Toy Story 1995 extra B 1080p CZ.mkv", 8_000_000_000),
        SearchResult("f1", "Toy Story 1995 1080p CZ.mkv", 4_000_000_000),
        SearchResult("f2", "Toy Story 1995 720p CZ.mkv", 2_000_000_000),
    ]
    fake_webshare.file_infos = {"s1": {**_INFO, "length": 10 * 60}, "s2": {**_INFO, "length": 12 * 60},
                                "f1": {**_INFO, "length": 81 * 60}, "f2": {**_INFO, "length": 80 * 60}}
    assert _feed(client, t="movie", tmdbid="862", cat="2000", limit="2") == ["f1", "f2"]
    assert fake_webshare.searches == [("Toy Story", 2, 0), ("Toy Story", 2, 2)]


def test_runtime_check_refill_is_bounded(client, fake_webshare, monkeypatch):
    """However many files are dropped, only a fixed number of extra pages is
    fetched; then the feed answers with what passed."""
    from app import torznab
    _enable_runtime_check(monkeypatch, _TOY_STORY, 81)
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult(f"s{i}", f"Toy Story 1995 clip {i} CZ.mkv", 100 - i)
                             for i in range(20)]
    fake_webshare.file_infos = {f"s{i}": {**_INFO, "length": 5 * 60} for i in range(20)}
    assert _feed(client, t="movie", tmdbid="862", cat="2000", limit="2") == []
    assert [o for _q, _l, o in fake_webshare.searches] == \
        [2 * p for p in range(1 + torznab._RUNTIME_EXTRA_PAGES)]


def test_runtime_check_answers_within_its_budget(client, fake_webshare, monkeypatch):
    """A probe that doesn't finish within the budget doesn't hold the search
    up: its file goes out unmeasured (fail open), the measured ones are judged."""
    import asyncio
    import time
    from app import torznab
    _enable_runtime_check(monkeypatch, _TOY_STORY, 81)
    monkeypatch.setattr(torznab, "_RUNTIME_BUDGET", 0.5)
    real = fake_webshare.file_info

    async def file_info(ident):
        if ident == "slow":
            await asyncio.sleep(3600)
        return await real(ident)

    fake_webshare.file_info = file_info
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("slow", "Toy Story 1995 2160p CZ.mkv", 9_000_000_000),
        SearchResult("short", "Toy Story 1995 clip CZ.mkv", 5_000_000_000),
        SearchResult("full", "Toy Story 1995 1080p CZ.mkv", 4_000_000_000),
    ]
    fake_webshare.file_infos = {"short": {**_INFO, "length": 5 * 60},
                                "full": {**_INFO, "length": 81 * 60}}
    started = time.monotonic()
    assert _feed(client, t="movie", tmdbid="862", cat="2000") == ["slow", "full"]
    assert time.monotonic() - started < 5


def test_runtime_check_budget_covers_the_tmdb_lookup(client, fake_webshare, monkeypatch):
    """A TMDB runtime lookup that outlives the budget switches the check off for
    that search instead of blocking it."""
    import asyncio
    import time
    from app import torznab
    _enable_runtime_check(monkeypatch, _TOY_STORY, 81)
    monkeypatch.setattr(torznab, "_RUNTIME_BUDGET", 0.3)

    async def hung(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(torznab, "tmdb_runtime", hung)
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("short", "Toy Story 1995 clip CZ.mkv", 5_000_000_000)]
    fake_webshare.file_infos = {"short": {**_INFO, "length": 5 * 60}}
    started = time.monotonic()
    assert _feed(client, t="movie", tmdbid="862", cat="2000") == ["short"]
    assert time.monotonic() - started < 5


def test_runtime_check_off_without_known_runtime(client, fake_webshare, monkeypatch):
    """No TMDB runtime -> nothing is dropped by length."""
    _enable_runtime_check(monkeypatch, _TOY_STORY, 0, exact=False)
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("spec", "Toy Story 1995 1080p CZ.mkv", 1_000_000_000)]
    fake_webshare.file_infos = {"spec": {"length": 60, "width": 1920, "height": 1080}}
    assert _feed(client, t="movie", tmdbid="862", cat="2000") == ["spec"]


# --- opt-in movie title prefix ---------------------------------------------

_ROCKY = ("Rocky", "", "en", (), 1976)
_TOY_STORY_CZ = ("Toy Story", "", "en", ("Příběh hraček",), 1995)
# Rocky's collection and a namesake, as tmdb.movie_rivals reports them
_ROCKY_RIVALS = [("Rocky II", 1979, True), ("Rocky V", 1990, True), ("Rocky Balboa", 2006, True),
                 ("Rocky", 1981, False)]


def test_movie_name_identity_needs_nothing_between_title_and_tags():
    """The reviewer's cases: a Roman numeral, a subtitle word, a number behind a
    language marker — and plain sequel markers — never pass for the film."""
    from app.torznab import movie_name_identity
    for name in ("Rocky V 1080p.mkv", "Rocky Returns 1080p.mkv", "Rocky CZ 2 1080p.mkv",
                 "Rocky 2 1080p.mkv", "Rocky II 1080p.mkv", "Rocky Balboa 1080p CZ.mkv",
                 "Rocky CZ Balboa 1080p.mkv", "Rocky (1976) II 1080p.mkv", "Rocky Part 2 1080p.mkv",
                 "Rocky-GRP.mkv", "Rocky Extended 1080p.mkv"):
        title, _dated, why = movie_name_identity(name, "Rocky", (), 1976)
        assert why, name
    # after the first technical tag, numbers, numerals and part/CD markers still refuse
    for name in ("Rocky 1080p 2.mkv", "Rocky 1080p CZ II.mkv", "Rocky 1976 1080p CD2.avi",
                 "Rocky 1976 720p sample.mkv", "Rocky 1976 1080p dil 2.mkv"):
        assert movie_name_identity(name, "Rocky", (), 1976)[2], name
    # any year must be the TMDB year, exactly
    assert movie_name_identity("Rocky 1977 1080p.mkv", "Rocky", (), 1976)[2] == "year 1977, not 1976"
    assert movie_name_identity("Rocky 1080p 1975 CZ.mkv", "Rocky", (), 1976)[2]
    # the film's own name, its year, language markers and release details pass
    assert movie_name_identity("Rocky (1976).mkv", "Rocky", (), 1976) == ("rocky", True, "")
    assert movie_name_identity("Rocky 1976 1080p CZ.mkv", "Rocky", (), 1976) == ("rocky", True, "")
    assert movie_name_identity("Rocky CZ (1976) 1080p.mkv", "Rocky", (), 1976) == ("rocky", True, "")
    assert movie_name_identity("Rocky.1976.1080p.BluRay.x264.DTS-HD.MA.5.1-GRP.mkv", "Rocky", (), 1976) == \
        ("rocky", True, "")
    assert movie_name_identity("Rocky 1080p CZ.mkv", "Rocky", (), 1976) == ("rocky", False, "")
    assert movie_name_identity("Pribeh hracek 1080p CZ.mkv", "Toy Story", ("Příběh hraček",), 1995) == \
        ("pribeh hracek", False, "")
    # no TMDB year, no identity
    assert movie_name_identity("Coco 1080p.mkv", "Coco", (), 0)[2]


def test_movie_name_identity_for_a_numbered_title():
    """A sequel's own number must be in the name, in either spelling; a title
    of the film that lacks it ("Rocky") doesn't count for "Rocky II"."""
    from app.torznab import movie_name_identity
    assert movie_name_identity("Rocky II (1979) CZ.mkv", "Rocky II", (), 1979) == ("rocky ii", True, "")
    assert movie_name_identity("Rocky 2 (1979) CZ.mkv", "Rocky II", (), 1979) == ("rocky ii", True, "")
    assert movie_name_identity("Rocky (1979) CZ.mkv", "Rocky II", ("Rocky",), 1979)[2]
    assert movie_name_identity("Rocky III 1080p.mkv", "Rocky II", (), 1979)[2]
    assert movie_name_identity("Rocky II 2 1080p.mkv", "Rocky II", (), 1979)[2]
    assert movie_name_identity("Pribeh hracek 2 CZ.mkv", "Toy Story 2", ("Příběh hraček 2",), 1999) == \
        ("pribeh hracek 2", False, "")


def test_movie_prefix_refusal_contract():
    from app.torznab import movie_prefix_refusal

    def why(name, length=119 * 60, minutes=119, rivals=_ROCKY_RIVALS):
        return movie_prefix_refusal(name, "Rocky", (), 1976, length_s=length, minutes=minutes, rivals=rivals)

    # the name proves nothing more than it says
    assert why("Rocky V 1080p.mkv") and why("Rocky Returns 1080p.mkv") and why("Rocky CZ 2 1080p.mkv")
    # runtime: unknown on either side refuses; a match is required, never enough
    assert why("Rocky (1976).mkv", length=0) == "length or TMDB runtime unknown"
    assert why("Rocky (1976).mkv", minutes=0) == "length or TMDB runtime unknown"
    assert why("Rocky (1976).mkv", length=60 * 60)
    assert why("Rocky Balboa 1080p.mkv")                  # same length, another film
    # the rival lookup failed: no identity claim
    assert why("Rocky (1976).mkv", rivals=None)
    # Rocky has sequels and a 1981 namesake: without the year it could be either
    assert why("Rocky 1080p CZ.mkv").startswith("without the year it could be")
    assert why("Rocky (1976).mkv") == ""
    assert why("Rocky 1976 1080p CZ.mkv") == ""
    assert why("Rocky CZ (1976) 1080p.mkv") == ""
    # a namesake of the same year leaves even the year undecided
    assert why("Rocky (1976).mkv", rivals=[("Rocky", 1976, False)])
    # a search hit that only extends the title isn't a namesake ("Rocky Horror")
    assert why("Rocky 1080p CZ.mkv", rivals=[("Rocky Horror Picture Show", 1975, False)]) == ""
    # ...but a collection part that extends it is, and its words anywhere refuse
    assert why("Rocky 1080p CZ.mkv", rivals=[("Rocky II", 1979, True)])
    assert why("Rocky 1976 1080p Balboa CZ.mkv") == "the name also has the words of 'Rocky Balboa'"


def _enable_title_prefix(monkeypatch, result, minutes, rivals=(), calls=None):
    """Turn the title prefix on and fake TMDB: the id lookup answers `result`,
    the runtime `minutes`, movie_rivals `rivals` (None = failed), recording each
    rivals call in `calls`."""
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "tok")
    monkeypatch.setattr(settings, "movie_title_prefix", True)

    async def by_id(token, kind, tmdbid=None, imdbid=None, tvdbid=None):
        return result

    async def by_name(token, kind, q):
        return result

    async def runtime(token, kind, tmdbid=None, imdbid=None, tvdbid=None, season=None, ep=None):
        return minutes, bool(minutes)

    async def movie_rivals(token, titles, tmdbid=None, imdbid=None):
        if calls is not None:
            calls.append(list(titles))
        return None if rivals is None else list(rivals)

    monkeypatch.setattr(torznab, "tmdb_lookup_by_id", by_id)
    monkeypatch.setattr(torznab, "tmdb_lookup", by_name)
    monkeypatch.setattr(torznab, "tmdb_runtime", runtime)
    monkeypatch.setattr(torznab, "tmdb_movie_rivals", movie_rivals)


def _titles(client, **params):
    """guid -> (title, nzbname from the link) of every feed item."""
    from urllib.parse import parse_qs, urlsplit
    resp = client.get("/torznab/api", params={"apikey": "testkey", **params})
    return {i.findtext("guid").removeprefix("websharr-"):
            (i.findtext("title"), parse_qs(urlsplit(i.findtext("link")).query)["nzbname"][0])
            for i in ET.fromstring(resp.content).findall("channel/item")}


def _rocky_files(fake_webshare, length=119 * 60):
    names = {"v": "Rocky V 1080p.mkv", "returns": "Rocky Returns 1080p.mkv",
             "cz2": "Rocky CZ 2 1080p.mkv", "two": "Rocky 2 1080p.mkv", "ii": "Rocky II 1080p.mkv",
             "balboa": "Rocky Balboa 1080p CZ.mkv", "bare": "Rocky 1080p CZ.mkv",
             "czyear": "Rocky CZ (1976) 1080p.mkv", "canon": "Rocky (1976) 1080p.mkv",
             "off1": "Rocky 1977 CZ 1080p.mkv"}
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult(k, n, 4_000_000_000 - i) for i, (k, n) in enumerate(names.items())]
    fake_webshare.file_infos = {k: {**_INFO, "length": length} for k in names}
    return names


def test_movie_title_prefix_is_off_by_default(client, fake_webshare, monkeypatch):
    """Off (the default), release names are exactly the file names, nothing asks
    TMDB for rivals or runtime, and only the usual label probes run."""
    from app import torznab
    from app.settings import settings
    assert settings.movie_title_prefix is False
    calls = []
    _enable_title_prefix(monkeypatch, _TOY_STORY_CZ, 81, rivals=[], calls=calls)
    monkeypatch.setattr(settings, "movie_title_prefix", False)
    runtimes = []

    async def runtime(*args, **kwargs):
        runtimes.append(args)
        return 81, True

    monkeypatch.setattr(torznab, "tmdb_runtime", runtime)
    probed = []
    real = fake_webshare.file_info

    async def file_info(ident):
        probed.append(ident)
        return await real(ident)

    fake_webshare.file_info = file_info
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("cz", "Pribeh hracek 1080p CZ.mkv", 2_000_000_000)]
    fake_webshare.file_infos = {"cz": {**_INFO, "length": 81 * 60}}
    assert _titles(client, t="movie", tmdbid="862", cat="2000") == \
        {"cz": ("Pribeh hracek 1080p CZ", "Pribeh hracek 1080p CZ")}
    assert calls == [] and runtimes == [] and probed == []


def test_movie_feed_prefixes_an_unambiguous_czech_name(client, fake_webshare, monkeypatch):
    calls = []
    _enable_title_prefix(monkeypatch, _TOY_STORY_CZ, 81, calls=calls)
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("cz", "Příběh hraček 1080p CZ.mkv", 2_000_000_000),
                             SearchResult("en", "Toy Story 1995 720p.mkv", 1_000_000_000)]
    fake_webshare.file_infos = {"cz": {**_INFO, "length": 81 * 60}, "en": {**_INFO, "length": 81 * 60}}
    feed = _titles(client, t="movie", tmdbid="862", cat="2000")
    assert feed["cz"] == ("Toy Story 1995 - Pribeh hracek 1080p CZ",) * 2
    assert feed["en"] == ("Toy Story 1995 720p",) * 2      # already "<title> <year>": left alone
    assert calls == [["Příběh hraček"]]                     # only the title a file began with


def test_movie_feed_prefix_rocky_cases(client, fake_webshare, monkeypatch):
    """Every file runs exactly as long as Rocky: only the name that leaves no
    doubt (title, then its year among markers and tags) gets the prefix."""
    _enable_title_prefix(monkeypatch, _ROCKY, 119, rivals=_ROCKY_RIVALS)
    names = _rocky_files(fake_webshare)
    feed = _titles(client, t="movie", tmdbid="1366", cat="2000")
    assert feed["czyear"][0] == "Rocky 1976 - Rocky CZ (1976) 1080p"
    for key, name in names.items():
        if key != "czyear":
            assert feed[key][0] == name.rsplit(".", 1)[0], key   # unchanged
            assert feed[key][1] == feed[key][0]


def test_movie_feed_no_prefix_without_runtime_or_probe(client, fake_webshare, monkeypatch):
    """The reviewer's offline feed: no TMDB runtime and no probe result -> no
    prefix and no canonical nzbname, however clean the name."""
    _enable_title_prefix(monkeypatch, _TOY_STORY_CZ, 0, rivals=[])
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("cz", "Pribeh hracek 1080p CZ.mkv", 2_000_000_000)]
    fake_webshare.file_infos = {"cz": {**_INFO, "length": 0}}
    assert _titles(client, t="movie", tmdbid="862", cat="2000")["cz"] == ("Pribeh hracek 1080p CZ",) * 2
    # a known runtime doesn't help while the file's own length is unknown
    _enable_title_prefix(monkeypatch, _TOY_STORY_CZ, 81, rivals=[])
    assert _titles(client, t="movie", tmdbid="862", cat="2000")["cz"] == ("Pribeh hracek 1080p CZ",) * 2


def test_movie_feed_no_prefix_when_the_rival_lookup_fails(client, fake_webshare, monkeypatch):
    """TMDB can't say which other films share the name (error or timeout): no
    prefix, even for a dated name, and the search isn't held up."""
    import asyncio
    import time
    from app import torznab
    _enable_title_prefix(monkeypatch, _ROCKY, 119, rivals=None)
    _rocky_files(fake_webshare)
    assert _titles(client, t="movie", tmdbid="1366", cat="2000")["czyear"][0] == "Rocky CZ (1976) 1080p"

    async def hung(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(torznab, "tmdb_movie_rivals", hung)
    monkeypatch.setattr(torznab, "_PREFIX_BUDGET", 0.3)
    started = time.monotonic()
    assert _titles(client, t="movie", tmdbid="1366", cat="2000")["czyear"][0] == "Rocky CZ (1976) 1080p"
    assert time.monotonic() - started < 5


def test_movie_feed_prefix_needs_an_id_search(client, fake_webshare, monkeypatch):
    """A text search (no tmdbid/imdbid) or a TV search never gets the prefix."""
    _enable_title_prefix(monkeypatch, _TOY_STORY_CZ, 81, rivals=[])
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("cz", "Pribeh hracek 1080p CZ.mkv", 2_000_000_000)]
    fake_webshare.file_infos = {"cz": {**_INFO, "length": 81 * 60}}
    assert _titles(client, t="movie", q="Toy Story", cat="2000")["cz"][0] == "Pribeh hracek 1080p CZ"
    assert _titles(client, t="search", q="Toy Story", cat="2000")["cz"][0] == "Pribeh hracek 1080p CZ"


def test_movie_feed_prefix_with_the_runtime_check(client, fake_webshare, monkeypatch):
    """With #4's runtime check on too: a file far off the runtime is dropped
    before the prefix is considered; a file of the right length but outside the
    prefix's tighter bounds stays, unprefixed."""
    from app.settings import settings
    _enable_title_prefix(monkeypatch, _TOY_STORY_CZ, 81, rivals=[])
    monkeypatch.setattr(settings, "runtime_check", True)
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("ok", "Pribeh hracek 1080p CZ.mkv", 3_000_000_000),
                             SearchResult("long", "Pribeh hracek 720p CZ.mkv", 2_000_000_000),
                             SearchResult("clip", "Pribeh hracek 480p CZ.mkv", 1_000_000_000)]
    fake_webshare.file_infos = {"ok": {**_INFO, "length": 80 * 60}, "long": {**_INFO, "length": 120 * 60},
                                "clip": {**_INFO, "length": 5 * 60}}
    feed = _titles(client, t="movie", tmdbid="862", cat="2000")
    assert sorted(feed) == ["long", "ok"]
    assert feed["ok"][0] == "Toy Story 1995 - Pribeh hracek 1080p CZ"
    assert feed["long"][0] == "Pribeh hracek 720p CZ"


# Radarr-like parse of a movie release title: a regex modeled on the "normal
# movie format" pattern of Radarr's Parser (title, then the year, then a
# separator). An isolated contract check only: no live Radarr was run.
_RADARR_LIKE = re.compile(
    r"^(?P<title>(?![(\[]).+?)?(?:(?:[-_\W](?<![)\[!]))*"
    r"(?P<year>(1(8|9)|20)\d{2}(?!p|i|x|\d+|\]|\W\d+)))+(\W+|_|$)(?!\\)", re.I)


def test_prefixed_titles_meet_the_radarr_import_contract(client, fake_webshare, monkeypatch):
    """Every prefixed release title (and the nzbname the job folder gets) starts
    with "<TMDB title> <year> - " and parses back, Radarr-like, to exactly that
    title and year — the mapping Radarr needs to import it by itself."""
    from app.torznab import _asciify, normalize_text
    cases = [
        (_TOY_STORY_CZ, 81, "Příběh hraček (1995) 1080p CZ.mkv"),
        (_TOY_STORY_CZ, 81, "Příběh.hraček.1080p.CZ.mkv"),
        (("Spider-Man: No Way Home", "", "en", ("Spider-Man: Bez domova",), 2021), 148,
         "Spider-Man Bez domova 2160p CZ.mkv"),
        (("Rocky II", "", "en", (), 1979), 119, "Rocky II CZ 1080p.mkv"),
        (("Amélie", "Le Fabuleux Destin d'Amélie Poulain", "fr", ("Amélie z Montmartru",), 2001), 122,
         "Amélie z Montmartru 1080p CZ dabing.mkv"),
    ]
    for i, (result, minutes, name) in enumerate(cases):
        _enable_title_prefix(monkeypatch, result, minutes, rivals=[])
        fake_webshare.fuzzy = True
        fake_webshare.results = [SearchResult(f"f{i}", name, 2_000_000_000)]
        fake_webshare.file_infos = {f"f{i}": {**_INFO, "length": minutes * 60}}
        title, nzbname = _titles(client, t="movie", tmdbid="1", cat="2000")[f"f{i}"]
        display, year = result[0], result[4]
        assert title == nzbname, name
        assert title.startswith(f"{_asciify(display)} {year} - "), title
        m = _RADARR_LIKE.match(title)
        assert m, title
        assert normalize_text(m.group("title")).split() == normalize_text(display).split(), title
        assert int(m.group("year")) == year, title
        title.encode("ascii")  # Prowlarr header-safe
    # a refused name keeps no canonical claim: nothing to map it by title
    _enable_title_prefix(monkeypatch, _ROCKY, 119, rivals=_ROCKY_RIVALS)
    fake_webshare.results = [SearchResult("r", "Rocky CZ 2 1080p.mkv", 2_000_000_000)]
    fake_webshare.file_infos = {"r": {**_INFO, "length": 119 * 60}}
    title, _nzb = _titles(client, t="movie", tmdbid="1366", cat="2000")["r"]
    assert not _RADARR_LIKE.match(title)
