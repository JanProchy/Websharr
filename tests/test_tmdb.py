"""TMDB title resolution: English display for foreign-origin titles."""

import asyncio

from app import tmdb


def test_pick_english():
    items = [{"iso_3166_1": "FR", "title": "Les Nineties"},
             {"iso_3166_1": "GB", "title": "Nineties"}]
    assert tmdb._pick_english(items) == "Nineties"
    assert tmdb._pick_english([{"iso_3166_1": "RU", "title": "Девяностые"}]) == ""
    assert tmdb._pick_english([]) == ""


def test_pick_czech():
    items = [{"iso_3166_1": "CZ", "title": "Kačeří příběhy"},
             {"iso_3166_1": "CZ", "title": "My z Kačerova"},
             {"iso_3166_1": "CZ", "title": "kačeří příběhy"},  # case-dup dropped
             {"iso_3166_1": "PL", "title": "Kacze Opowieści"},
             {"iso_3166_1": "US", "title": "Disney's DuckTales"}]
    assert tmdb._pick_czech(items) == ["Kačeří příběhy", "My z Kačerova"]
    assert tmdb._pick_czech([]) == []


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Client:
    def __init__(self, alt_titles):
        self._alt = alt_titles

    async def get(self, url, params=None):
        return _Resp({"results": self._alt})


def test_resolve_swaps_english_for_foreign_origin():
    # CZ-origin show with no English name of its own: display should become the
    # English alternative title, the Czech name becomes the search term.
    entry = {"id": 155277, "name": "Devadesátky", "original_name": "Devadesátky",
             "original_language": "cs"}
    client = _Client([{"iso_3166_1": "GB", "title": "Nineties"}])
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "Nineties" and orig == "Devadesátky" and lang == "cs"
    assert czech == ()  # Czech-origin: the original already is the Czech name


def test_resolve_keeps_existing_english_name():
    # CZ-origin show with a distinct English name must not fetch alt titles:
    # the original already is the Czech search term.
    class _Boom:
        async def get(self, *a, **k):
            raise AssertionError("should not fetch alternative titles")

    entry = {"id": 1, "name": "The Sleepers", "original_name": "Bez vědomí",
             "original_language": "cs"}
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(_Boom(), entry, "tv"))
    assert disp == "The Sleepers" and orig == "Bez vědomí" and lang == "cs"
    assert czech == ()


def test_resolve_english_show_picks_czech_alt_titles():
    # English-origin show dubbed under Czech names: the CZ alternative titles
    # become extra search terms; display/original stay untouched.
    entry = {"id": 720, "name": "DuckTales", "original_name": "DuckTales",
             "original_language": "en", "first_air_date": "1987-09-18"}
    client = _Client([{"iso_3166_1": "CZ", "title": "Kačeří příběhy"},
                      {"iso_3166_1": "CZ", "title": "My z Kačerova"},
                      {"iso_3166_1": "US", "title": "Disney's DuckTales"}])
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "DuckTales" and orig == "" and lang == "en"
    assert czech == ("Kačeří příběhy", "My z Kačerova")
    assert year == 1987


def test_resolve_english_show_without_czech_titles():
    entry = {"id": 2, "name": "The Office", "original_name": "The Office",
             "original_language": "en"}
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(_Client([]), entry, "tv"))
    assert disp == "The Office" and orig == "" and lang == "en" and czech == ()
    assert year == 0  # no air date in the entry


class _RoutedClient:
    """Serves alternative_titles and translations from separate payloads."""

    def __init__(self, alt_titles, translations):
        self._alt, self._tr = alt_titles, translations

    async def get(self, url, params=None):
        if url.endswith("/translations"):
            return _Resp({"translations": self._tr})
        return _Resp({"results": self._alt})


def test_resolve_picks_czech_translation_name():
    # Red Dwarf has no CZ *alternative* title; its Czech name lives only in the
    # cs translation. An empty or same-as-display translation adds nothing.
    entry = {"id": 326, "name": "Red Dwarf", "original_name": "Red Dwarf",
             "original_language": "en", "first_air_date": "1988-02-15"}
    client = _RoutedClient([], [
        {"iso_639_1": "cs", "iso_3166_1": "CZ", "data": {"name": "Červený trpaslík"}},
        {"iso_639_1": "sk", "iso_3166_1": "SK", "data": {"name": "Červený trpaslík"}},
        {"iso_639_1": "de", "iso_3166_1": "DE", "data": {"name": "Red Dwarf DE"}},
    ])
    disp, orig, lang, czech, year = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "Red Dwarf" and czech == ("Červený trpaslík",)

    entry = {"id": 615, "name": "Futurama", "original_name": "Futurama",
             "original_language": "en"}
    client = _RoutedClient([], [{"iso_639_1": "cs", "data": {"name": ""}},
                                {"iso_639_1": "cs", "data": {"name": "Futurama"}}])
    assert asyncio.run(tmdb._resolve(client, entry, "tv"))[3] == ()


def test_resolve_merges_czech_alt_titles_and_translation():
    entry = {"id": 720, "name": "DuckTales", "original_name": "DuckTales",
             "original_language": "en"}
    client = _RoutedClient(
        [{"iso_3166_1": "CZ", "title": "Kačeří příběhy"}, {"iso_3166_1": "CZ", "title": "My z Kačerova"}],
        [{"iso_639_1": "cs", "data": {"name": "Kačeří příběhy"}}])
    assert asyncio.run(tmdb._resolve(client, entry, "tv"))[3] == \
        ("Kačeří příběhy", "My z Kačerova")


def test_resolve_movie_uses_translation_title():
    entry = {"id": 9, "title": "The Shawshank Redemption",
             "original_title": "The Shawshank Redemption", "original_language": "en"}
    client = _RoutedClient([], [{"iso_639_1": "cs", "data": {"title": "Vykoupení z věznice Shawshank"}}])
    assert asyncio.run(tmdb._resolve(client, entry, "movie"))[3] == \
        ("Vykoupení z věznice Shawshank",)


class _RecordingClient:
    """Stands in for httpx.AsyncClient; answers by URL and records every call."""

    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        _RecordingClient.calls.append(url)
        if url.endswith("/movie/862"):
            return _Resp({"id": 862, "title": "Toy Story", "original_title": "Toy Story",
                          "original_language": "en", "release_date": "1995-10-30", "runtime": 81})
        if url.endswith("/tv/82728/season/1/episode/1"):
            return _Resp({"runtime": 8})
        if url.endswith("/tv/82728/season/1/episode/2"):
            return _Resp({"runtime": None})
        if url.endswith("/tv/82728"):
            return _Resp({"id": 82728, "name": "Bluey", "episode_run_time": [7]})
        if url.endswith("/find/tt0114709"):
            return _Resp({"movie_results": [{"id": 862, "title": "Toy Story"}]})
        return _Resp({"results": [], "translations": []})


def test_runtime_reuses_the_id_lookup(monkeypatch):
    """lookup_by_id already fetched the movie details for this search: the
    runtime check must not ask TMDB for them a second time."""
    from app import tmdb
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _RecordingClient)
    monkeypatch.setattr(tmdb, "_cache", {})
    monkeypatch.setattr(tmdb, "_resolved", {})
    monkeypatch.setattr(tmdb, "_runtime_cache", {})
    _RecordingClient.calls = []
    asyncio.run(tmdb.lookup_by_id("tok", "movie", tmdbid="862"))
    before = list(_RecordingClient.calls)
    assert asyncio.run(tmdb.runtime("tok", "movie", tmdbid="862")) == (81, True)
    assert _RecordingClient.calls == before


def test_runtime_resolves_an_imdb_id_once(monkeypatch):
    """An IMDb-only search: the TMDB id found by lookup_by_id is reused, so the
    runtime check needs neither /find nor the details again."""
    from app import tmdb
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _RecordingClient)
    monkeypatch.setattr(tmdb, "_cache", {})
    monkeypatch.setattr(tmdb, "_resolved", {})
    monkeypatch.setattr(tmdb, "_runtime_cache", {})
    _RecordingClient.calls = []
    asyncio.run(tmdb.lookup_by_id("tok", "movie", imdbid="tt0114709"))
    assert asyncio.run(tmdb.runtime("tok", "movie", imdbid="tt0114709")) == (81, True)
    assert sum("/find/" in c for c in _RecordingClient.calls) == 1
    assert sum(c.endswith("/movie/862") for c in _RecordingClient.calls) == 1


def test_runtime_says_whether_it_is_the_episodes_own(monkeypatch):
    """An episode's own runtime is exact; the show's typical episode length (a
    season search, or an episode without a runtime) is only a fallback, which
    the runtime check treats leniently."""
    from app import tmdb
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _RecordingClient)
    monkeypatch.setattr(tmdb, "_resolved", {})
    monkeypatch.setattr(tmdb, "_runtime_cache", {})
    assert asyncio.run(tmdb.runtime("tok", "tv", tmdbid="82728", season="1", ep="1")) == (8, True)
    assert asyncio.run(tmdb.runtime("tok", "tv", tmdbid="82728", season="1", ep="2")) == (7, False)
    assert asyncio.run(tmdb.runtime("tok", "tv", tmdbid="82728", season="1")) == (7, False)
    assert asyncio.run(tmdb.runtime("tok", "tv")) == (0, False)


class _RivalsClient:
    """Stands in for httpx.AsyncClient for movie_rivals: Rocky (1366), its
    collection and a title search; `fail` makes the search answer 500."""

    calls: list[tuple[str, dict | None]] = []
    fail = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        _RivalsClient.calls.append((url, params))
        if url.endswith("/movie/1366"):
            return _Resp({"id": 1366, "title": "Rocky", "belongs_to_collection": {"id": 1575}})
        if url.endswith("/find/tt0075148"):
            return _Resp({"movie_results": [{"id": 1366, "title": "Rocky"}]})
        if url.endswith("/collection/1575"):
            return _Resp({"parts": [
                {"id": 1366, "title": "Rocky", "original_title": "Rocky", "release_date": "1976-11-21"},
                {"id": 1367, "title": "Rocky II", "original_title": "Rocky II", "release_date": "1979-06-15"},
                {"id": 1374, "title": "Rocky Balboa", "original_title": "Rocky Balboa",
                 "release_date": "2006-12-20"}]})
        if url.endswith("/search/movie"):
            if _RivalsClient.fail:
                r = _Resp({})
                r.status_code = 500
                return r
            return _Resp({"results": [
                {"id": 1366, "title": "Rocky", "original_title": "Rocky", "release_date": "1976-11-21"},
                {"id": 99999, "title": "Rocky", "original_title": "Rocky", "release_date": "1981-10-02"}]})
        return _Resp({})


def test_movie_rivals_lists_other_films_and_fails_closed(monkeypatch):
    """The collection's other parts and the other films a title search finds,
    never the movie itself; cached once it succeeded, None when TMDB fails."""
    monkeypatch.setattr(tmdb.httpx, "AsyncClient", _RivalsClient)
    monkeypatch.setattr(tmdb, "_resolved", {})
    monkeypatch.setattr(tmdb, "_parts_cache", {})
    monkeypatch.setattr(tmdb, "_hits_cache", {})
    _RivalsClient.calls, _RivalsClient.fail = [], False
    rivals = asyncio.run(tmdb.movie_rivals("tok", ["Rocky"], tmdbid="1366"))
    assert ("Rocky II", 1979, True) in rivals and ("Rocky Balboa", 2006, True) in rivals
    assert ("Rocky", 1981, False) in rivals
    assert not any(year == 1976 for _t, year, _p in rivals)
    first = len(_RivalsClient.calls)
    assert first == 1 + 2 + 2                     # details, collection en/cs, search en/cs
    assert asyncio.run(tmdb.movie_rivals("tok", ["Rocky"], tmdbid="1366")) == rivals
    assert len(_RivalsClient.calls) == first      # cached
    # by IMDb id: one /find, then the cached collection
    assert asyncio.run(tmdb.movie_rivals("tok", ["Rocky"], imdbid="tt0075148")) == rivals
    # a failed lookup is not cached and says "can't tell"
    monkeypatch.setattr(tmdb, "_hits_cache", {})
    _RivalsClient.fail = True
    assert asyncio.run(tmdb.movie_rivals("tok", ["Rocky"], tmdbid="1366")) is None
    assert tmdb._hits_cache == {}
    assert asyncio.run(tmdb.movie_rivals("", ["Rocky"], tmdbid="1366")) is None
    assert asyncio.run(tmdb.movie_rivals("tok", ["Rocky"])) is None
