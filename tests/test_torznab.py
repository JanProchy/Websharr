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


class _GatedClient:
    """file_info that waits for `gate` and counts the requests that reach it."""

    def __init__(self, gate, fail=None):
        self.calls = 0
        self.gate = gate
        self.fail = fail

    async def file_info(self, ident):
        self.calls += 1
        await self.gate.wait()
        if self.fail:
            raise self.fail
        return {"length": 5400, "width": 1920, "height": 1080}


def test_concurrent_probes_of_one_file_share_one_request():
    """Parallel searches sharing a file (cold cache) send one file_info for it,
    not one each."""
    import asyncio
    from app import torznab

    async def run():
        gate = asyncio.Event()
        c = _GatedClient(gate)
        waiters = [asyncio.ensure_future(torznab._file_info(c, "same")) for _ in range(5)]
        other = asyncio.ensure_future(torznab._file_info(c, "other"))
        await asyncio.sleep(0.01)
        gate.set()
        infos = await asyncio.gather(*waiters, other)
        return c.calls, infos

    calls, infos = asyncio.run(run())
    assert calls == 2                                      # one per ident
    assert all(i["height"] == 1080 for i in infos)
    assert torznab._probe_inflight == {} and set(torznab._probe_cache) == {"same", "other"}


def test_probe_abandoned_by_one_search_still_serves_the_others():
    """A search whose budget runs out stops waiting, but the shared request is
    not cancelled: another search waiting on it gets the answer, and it lands
    in the cache."""
    import asyncio
    from app import torznab

    async def run():
        gate = asyncio.Event()
        c = _GatedClient(gate)
        late = asyncio.ensure_future(torznab._file_info(c, "x"))
        try:
            await asyncio.wait_for(torznab._file_info(c, "x"), timeout=0.01)
        except asyncio.TimeoutError:
            pass
        gate.set()
        return c.calls, await late

    calls, info = asyncio.run(run())
    assert calls == 1 and info["height"] == 1080
    assert torznab._probe_cache["x"]["height"] == 1080


def test_shared_probe_failure_fails_open_for_every_waiter():
    """An upstream error answers {} to every search waiting on the probe; nothing
    is cached, so the next search asks again."""
    import asyncio
    import httpx
    from app import torznab

    async def run():
        gate = asyncio.Event()
        c = _GatedClient(gate, fail=httpx.ConnectError("down"))
        waiters = [asyncio.ensure_future(torznab._file_info(c, "x")) for _ in range(3)]
        await asyncio.sleep(0.01)
        gate.set()
        return c.calls, await asyncio.gather(*waiters)

    calls, infos = asyncio.run(run())
    assert calls == 1 and infos == [{}, {}, {}]
    assert "x" not in torznab._probe_cache and torznab._probe_inflight == {}


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
    assert torznab._probe_inflight == {}
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


# --- measured metadata (opt-in) ----------------------------------------------

_GB = 1024 ** 3


def _info(**kw):
    """A file_info record; `audio_languages` follows the tracks, as Webshare reports it."""
    base = {"length": 6000, "width": 1920, "height": 1080, "format": "H264", "type": "mkv", "audio": []}
    base.update(kw)
    base.setdefault("audio_languages", [a["language"] for a in base["audio"] if a.get("language")])
    return base


def _track(fmt, channels, language=""):
    return {"format": fmt, "channels": channels, "language": language}


def _items(client, **params):
    """{guid: (title, [language attrs])} of a feed."""
    resp = client.get("/torznab/api", params={"apikey": "testkey", **params})
    out = {}
    for it in ET.fromstring(resp.content).findall("channel/item"):
        langs = [a.get("value") for a in it.findall(f"{NZNS}attr") if a.get("name") == "language"]
        out[it.findtext("guid").removeprefix("websharr-")] = (it.findtext("title"), langs)
    return out


def _counting_file_info(fake_webshare):
    probed = []
    real = fake_webshare.file_info

    async def file_info(ident):
        probed.append(ident)
        return await real(ident)

    fake_webshare.file_info = file_info
    return probed


def test_audio_codec_is_the_reported_codec_only():
    """No layout from a channel count, no profile from a codec name: 8-channel
    DTS stays "DTS" (not "DTS-HD MA 7.1"), 3 channels give no "2.1"."""
    from app.torznab import audio_codec
    assert audio_codec({"audio": [_track("DTS", 8)]}) == "DTS"
    assert audio_codec({"audio": [_track("DTS", 6)]}) == "DTS"
    assert audio_codec({"audio": [_track("AAC", 3)]}) == "AAC"
    assert audio_codec({"audio": [_track("E-AC-3", 6)]}) == "EAC3"
    tracks = [_track("TRUEHD", 8, "ENG"), _track("EAC3", 6, "CZE"), _track("AC3", 6, "SLO")]
    assert audio_codec({"audio": tracks}) == "TrueHD"
    assert audio_codec({"audio": tracks}, "Czech") == "EAC3"     # the dub's own track
    assert audio_codec({"audio": tracks}, "Slovak") == "AC3"
    assert audio_codec({"audio": [_track("WMA", 2)]}) == ""        # unknown codec: nothing
    assert audio_codec({"audio": []}) == "" and audio_codec({}) == ""


def test_measured_tags_never_add_over_a_named_tag():
    """An audio tag in the name, in any spelling, suppresses the measured audio
    codec (DD5.1 must not gain DDP5.1/EAC3); same for the video codec."""
    from app.torznab import measured_tags
    eac3 = _info(audio=[_track("EAC3", 6, "ENG")])
    for name in ("Film 2020 1080p WEB DD5.1", "Film.2020.1080p.WEB.DD.5.1", "Film 2020 DD51",
                 "Film 2020 DD+5.1", "Film 2020 DDP5.1", "Film.2020.DDP.Atmos", "Film 2020 AC3",
                 "Film 2020 AC-3", "Film 2020 EAC3", "Film 2020 E-AC-3", "Film 2020 DTS",
                 "Film 2020 DTS-HD MA 7.1", "Film 2020 DTSHD", "Film 2020 TrueHD 7.1",
                 "Film 2020 Atmos", "Film 2020 AAC2.0", "Film_2020_AAC", "Film 2020 FLAC",
                 "Film 2020 MP3", "Film 2020 Opus", "Film 2020 LPCM", "Film 2020 Dolby Digital"):
        assert measured_tags(name + " x264", eac3) == [], name
    for name in ("Film 2020 x264", "Film 2020 X265", "Film.2020.H.264", "Film 2020 h265",
                 "Film 2020 HEVC", "Film 2020 AVC", "Film 2020 XviD", "Film 2020 AV1",
                 "Film_2020_x264_"):
        assert measured_tags(name + " AC3", eac3) == [], name
    # nothing in the name: the reported codecs
    assert measured_tags("Film 2020 1080p", eac3) == ["H264", "EAC3"]
    assert measured_tags("Film 2020 1080p BluRay", _info(format="HEVC")) == ["H265"]  # not a bare HEVC
    # words that merely contain a codec name don't count as a tag
    assert measured_tags("Addams Family 1991 1080p", eac3) == ["H264", "EAC3"]


def test_measured_metadata_is_off_by_default(client, fake_webshare, monkeypatch):
    """Off (the default), probing and the feed are as without the feature: only
    names lacking a resolution or a language marker are probed, nothing is
    added to titles, one language attr."""
    from app import torznab
    from app.settings import settings
    assert settings.measured_metadata is False
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    probed = _counting_file_info(fake_webshare)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("named", "Hleda se Nemo 2003 1080p CZ.mkv", 8 * _GB),
        SearchResult("bare", "Hleda se Nemo 2003.mkv", 7 * _GB),
    ]
    tracks = [_track("EAC3", 6, "CZE"), _track("TRUEHD", 8, "ENG")]
    fake_webshare.file_infos = {"named": _info(audio=tracks), "bare": _info(audio=tracks)}
    got = _items(client, t="movie", q="Hleda se Nemo")
    assert probed == ["bare"]
    assert set(torznab._probe_cache) == {"bare"}
    assert got["named"] == ("Hleda se Nemo 2003 1080p CZ", ["Czech"])
    assert got["bare"] == ("Hleda se Nemo 2003 1080p CZ", ["Czech"])
    assert fake_webshare.searches == [("Hleda se Nemo", 60, 0)]


def test_measured_metadata_adds_reported_codecs_and_track_languages(client, fake_webshare, monkeypatch):
    """On, every shown file is measured (a cold cache: one probe each) and gets
    the reported codecs it doesn't name, plus one language attr per tagged
    track, the name's dub claim first. No extra Webshare pages are fetched."""
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    monkeypatch.setattr(settings, "measured_metadata", True)
    probed = _counting_file_info(fake_webshare)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("dual", "Hleda se Nemo 2003 1080p CZ.mkv", 8 * _GB),
        SearchResult("dd", "Hleda se Nemo 2003 1080p BluRay x264 DD5.1 CZ.mkv", 7 * _GB),
        SearchResult("dts", "Hleda se Nemo 2003 2160p.mkv", 20 * _GB),
        SearchResult("untagged", "Hleda se Nemo 2003 720p CZ dabing.mkv", 2 * _GB),
    ]
    fake_webshare.file_infos = {
        "dual": _info(audio=[_track("TRUEHD", 8, "ENG"), _track("EAC3", 6, "CZE")]),
        "dd": _info(audio=[_track("EAC3", 6, "CZE")]),
        "dts": _info(width=3840, height=2160, format="HEVC", audio=[_track("DTS", 8, "ENG")]),
        "untagged": _info(width=1280, height=720, audio=[_track("AAC", 2)]),
    }
    got = _items(client, t="movie", q="Hleda se Nemo", limit="10")
    assert sorted(probed) == ["dd", "dts", "dual", "untagged"]
    # the dub's own track, not the English TrueHD
    assert got["dual"] == ("Hleda se Nemo 2003 1080p CZ H264 EAC3", ["Czech", "English"])
    # the uploader's DD5.1 and x264 stay alone: no DDP5.1/EAC3, no H264
    assert got["dd"] == ("Hleda se Nemo 2003 1080p BluRay x264 DD5.1 CZ", ["Czech"])
    # 8-channel DTS is "DTS": no DTS-HD MA, no 7.1
    assert got["dts"] == ("Hleda se Nemo 2003 2160p H265 DTS", ["English"])
    # untagged tracks: the name's claim, as without the option
    assert got["untagged"] == ("Hleda se Nemo 2003 720p CZ dabing H264 AAC", ["Czech"])
    assert fake_webshare.searches == [("Hleda se Nemo", 10, 0)]


def test_measured_metadata_fails_open_on_probe_errors(client, fake_webshare, monkeypatch):
    """A file whose probe fails upstream goes out exactly as with the option off."""
    import httpx
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("x", "Hleda se Nemo 2003 1080p CZ.mkv", 8 * _GB)]
    off = _items(client, t="movie", q="Hleda se Nemo")

    async def broken(ident):
        raise httpx.ConnectError("webshare down")

    fake_webshare.file_info = broken
    monkeypatch.setattr(settings, "measured_metadata", True)
    assert _items(client, t="movie", q="Hleda se Nemo") == off == {
        "x": ("Hleda se Nemo 2003 1080p CZ", ["Czech"])}


def test_measured_metadata_answers_within_the_budget(client, fake_webshare, monkeypatch):
    """A probe that outlives the shared budget doesn't hold the search up: that
    file goes out unchanged (fail open), the others are measured."""
    import asyncio
    import time
    from app import torznab
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    monkeypatch.setattr(settings, "measured_metadata", True)
    monkeypatch.setattr(torznab, "_RUNTIME_BUDGET", 0.5)
    real = fake_webshare.file_info

    async def file_info(ident):
        if ident == "slow":
            await asyncio.sleep(3600)
        return await real(ident)

    fake_webshare.file_info = file_info
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("slow", "Hleda se Nemo 2003 2160p CZ.mkv", 20 * _GB),
        SearchResult("fast", "Hleda se Nemo 2003 1080p CZ.mkv", 8 * _GB),
    ]
    fake_webshare.file_infos = {"fast": _info(audio=[_track("AC3", 6, "CZE")])}
    started = time.monotonic()
    got = _items(client, t="movie", q="Hleda se Nemo")
    assert time.monotonic() - started < 5
    assert got == {"slow": ("Hleda se Nemo 2003 2160p CZ", ["Czech"]),
                   "fast": ("Hleda se Nemo 2003 1080p CZ H264 AC3", ["Czech"])}


def test_runtime_check_and_measured_metadata_share_one_pass(client, fake_webshare, monkeypatch):
    """Both on: each file is probed once, the runtime check drops what is far off
    and the kept files carry the measured codecs."""
    from app.settings import settings
    _enable_runtime_check(monkeypatch, _TOY_STORY, 81)
    monkeypatch.setattr(settings, "measured_metadata", True)
    probed = _counting_file_info(fake_webshare)
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("full", "Toy Story 1995 1080p CZ.mkv", 4 * _GB),
        SearchResult("spec", "Toy Story 1995 extra 1080p CZ.mkv", 1 * _GB),
    ]
    fake_webshare.file_infos = {"full": _info(length=81 * 60, audio=[_track("AC3", 6, "CZE")]),
                                "spec": _info(length=10 * 60)}
    got = _items(client, t="movie", tmdbid="862", cat="2000")
    assert got == {"full": ("Toy Story 1995 1080p CZ H264 AC3", ["Czech"])}
    assert sorted(probed) == ["full", "spec"]


# --- quality heuristics (opt-in) ---------------------------------------------

def test_quality_heuristics_correct_an_overstated_resolution():
    from app.torznab import quality_heuristics
    assert quality_heuristics("Film 2020 4K CZ", 8 * _GB, _info())[0] == "Film 2020 1080p CZ"
    # any overstated claim, not only 4K; one resolution token left, never two
    assert quality_heuristics("Film 2020 1080p CZ", 4 * _GB, _info(width=1280, height=720))[0] == \
        "Film 2020 720p CZ"
    assert quality_heuristics("Film.2020.4K.UHD.x265", 8 * _GB, _info())[0] == "Film.2020.1080p.x265"
    assert quality_heuristics("Film 2020 1080p 4K", 8 * _GB, _info())[0] == "Film 2020 1080p"
    # 4:3 1440x1080 and cropped 1920x800 are 1080p; an understated name stays
    assert quality_heuristics("Film 1943 1080p", 6 * _GB, _info(width=1440, height=1080))[0] == \
        "Film 1943 1080p"
    assert quality_heuristics("Film 2020 1080p", 6 * _GB, _info(width=1920, height=800))[0] == \
        "Film 2020 1080p"
    assert quality_heuristics("Film 2020 720p", 6 * _GB, _info())[0] == "Film 2020 720p"
    # nothing measured: nothing corrected
    assert quality_heuristics("Film 2020 4K", 8 * _GB, {}) == ("Film 2020 4K", [])


def test_quality_heuristics_upscaled():
    """Bitrate only judges a measured 2160p H264/H265; an AI upscale named in
    the release counts even unprobed; Regrade is not an upscale."""
    from app.torznab import quality_heuristics, upscale_claim
    uhd = {"width": 3840, "height": 2160, "length": 6000}   # 100 minutes
    assert quality_heuristics("Kaceri pribehy 2160p", 2 * _GB, _info(**uhd, format="HEVC"))[1] == \
        ["Upscaled"]                                          # 20 MiB/min
    assert quality_heuristics("Film 2160p", 20 * _GB, _info(**uhd, format="HEVC"))[1] == []
    assert quality_heuristics("Film 2160p AV1", 2 * _GB, _info(**uhd, format="AV1"))[1] == []
    assert quality_heuristics("Film 1080p", 1 * _GB, _info())[1] == []   # not UHD: not judged
    assert upscale_claim("DuckTales.Maid.Of.The.Myth.CZECH.1080p.AI.WEB.H264-ZEPPELiN")
    assert upscale_claim("Vinnetou.1963.CZ.EN.Blu-Ray.AI.Upscale.2160p.x265")
    assert upscale_claim("Film AIUS 2160p")
    assert not upscale_claim("Film 1985 Regrade 1080p")
    assert not upscale_claim("Ai Weiwei Never Sorry 2012 1080p")
    assert not upscale_claim("A.I. Artificial Intelligence 2001 1080p")
    assert quality_heuristics("Film 1080p AI WEB", 1, {}) == ("Film 1080p AI WEB", ["Upscaled"])
    # already named Upscaled: not doubled
    assert quality_heuristics("Film 2160p Upscaled", 2 * _GB, _info(**uhd))[1] == []


def test_uhd_av1_is_not_called_upscaled_by_default(client, fake_webshare, monkeypatch):
    """Reviewer case: a measured 2160p AV1 of 2 GiB over 100 minutes. Neither
    the default nor the measured metadata calls it Upscaled; bitrate alone
    can't show the source resolution. Same for an H265 file of that size
    unless the heuristics are turned on."""
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("av1", "Film 2020 2160p.mkv", 2 * _GB),
                             SearchResult("hevc", "Film 2020 2160p CZ.mkv", 2 * _GB)]
    uhd = {"width": 3840, "height": 2160, "length": 6000}
    fake_webshare.file_infos = {"av1": _info(**uhd, format="AV1"), "hevc": _info(**uhd, format="HEVC")}
    titles = lambda: {g: t for g, (t, _l) in _items(client, t="movie", q="Film").items()}  # noqa: E731
    assert titles() == {"av1": "Film 2020 2160p", "hevc": "Film 2020 2160p CZ"}
    monkeypatch.setattr(settings, "measured_metadata", True)
    assert titles() == {"av1": "Film 2020 2160p AV1", "hevc": "Film 2020 2160p CZ H265"}
    monkeypatch.setattr(settings, "measured_metadata", False)
    monkeypatch.setattr(settings, "quality_heuristics", True)
    assert titles() == {"av1": "Film 2020 2160p", "hevc": "Film 2020 2160p CZ Upscaled"}


def test_quality_heuristics_in_the_feed(client, fake_webshare, monkeypatch):
    """On, every shown file is measured: a named "4K" that is 1080p inside is
    relabelled, an AI upscale named in the release is marked even when its
    probe fails; a file whose probe fails otherwise goes out unchanged."""
    import httpx
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    monkeypatch.setattr(settings, "quality_heuristics", True)
    real = fake_webshare.file_info

    async def file_info(ident):
        if ident in ("ai", "broken"):
            raise httpx.ConnectError("webshare down")
        return await real(ident)

    fake_webshare.file_info = file_info
    fake_webshare.fuzzy = True
    fake_webshare.results = [
        SearchResult("fake4k", "Film 2020 4K CZ.mkv", 8 * _GB),
        SearchResult("ai", "Film 2020 1080p AI WEB CZ.mkv", 4 * _GB),
        SearchResult("broken", "Film 2020 4K EN.mkv", 3 * _GB),
    ]
    fake_webshare.file_infos = {"fake4k": _info()}
    titles = {g: t for g, (t, _l) in _items(client, t="movie", q="Film").items()}
    # (a bare "4K" isn't a resolution label, so the measured "1080p" is appended
    # as without the option; the heuristics then drop the false "4K")
    assert titles == {"fake4k": "Film 2020 CZ 1080p", "ai": "Film 2020 1080p AI WEB CZ Upscaled",
                      "broken": "Film 2020 4K EN"}


# --- stub filter (opt-in) ----------------------------------------------------

def test_is_stub():
    from app.torznab import is_stub
    assert is_stub(10 * 1024 ** 2, 135 * 60)            # 10 MiB over 135 min
    assert not is_stub(1 * _GB, 100 * 60)               # 10 MiB/min: a poor encode, not a stub
    assert not is_stub(10 * 1024 ** 2, 0)               # unknown length
    assert not is_stub(0, 6000)                         # unknown size


def test_stubs_are_kept_by_default(client, fake_webshare, monkeypatch):
    """Off (the default) a stub goes out like any other file, and a named file
    isn't probed for it."""
    from app.settings import settings
    assert settings.reject_stubs is False
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    probed = _counting_file_info(fake_webshare)
    fake_webshare.fuzzy = True
    fake_webshare.results = [SearchResult("stub", "Hleda se Nemo 2003 1080p CZ.mp4", 10 * 1024 ** 2)]
    fake_webshare.file_infos = {"stub": _info(length=8000)}
    assert list(_items(client, t="movie", q="Hleda se Nemo")) == ["stub"]
    assert probed == []


def test_reject_stubs_drops_and_replaces_them(client, fake_webshare, monkeypatch):
    """On, a stub is dropped and replaced from the next page; unknown length and
    a failed probe keep the file (fail open)."""
    import httpx
    from app.settings import settings
    monkeypatch.setattr(settings, "aliases", [])
    monkeypatch.setattr(settings, "tmdb_token", "")
    monkeypatch.setattr(settings, "reject_stubs", True)
    real = fake_webshare.file_info

    async def file_info(ident):
        if ident == "broken":
            raise httpx.ConnectError("webshare down")
        return await real(ident)

    fake_webshare.file_info = file_info
    fake_webshare.fuzzy = True
    fake_webshare.results = [  # page 1 (limit 3): the first three; page 2: "good"
        SearchResult("broken", "Hleda se Nemo 2003 720p CZ.mkv", 8 * _GB),
        SearchResult("unknown", "Hleda se Nemo 2003 CZ.avi", 7 * _GB),
        SearchResult("stub", "Hleda se Nemo 2003 1080p CZ.mp4", 10 * 1024 ** 2),
        SearchResult("good", "Hleda se Nemo 2003 2160p CZ.mkv", 6 * _GB),
    ]
    fake_webshare.file_infos = {"stub": _info(length=8000), "unknown": _info(length=0),
                                "good": _info(width=3840, height=2160)}
    got = _items(client, t="movie", q="Hleda se Nemo", limit="3")
    assert sorted(got) == ["broken", "good", "unknown"]
    assert [o for _q, _l, o in fake_webshare.searches] == [0, 3]   # one refill page
    # nothing else changes with only the stub filter on
    assert got["good"] == ("Hleda se Nemo 2003 2160p CZ", ["Czech"])
