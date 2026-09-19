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
