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
    disp, orig, lang, czech = asyncio.run(tmdb._resolve(client, entry, "tv"))
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
    disp, orig, lang, czech = asyncio.run(tmdb._resolve(_Boom(), entry, "tv"))
    assert disp == "The Sleepers" and orig == "Bez vědomí" and lang == "cs"
    assert czech == ()


def test_resolve_english_show_picks_czech_alt_titles():
    # English-origin show dubbed under Czech names: the CZ alternative titles
    # become extra search terms; display/original stay untouched.
    entry = {"id": 720, "name": "DuckTales", "original_name": "DuckTales",
             "original_language": "en"}
    client = _Client([{"iso_3166_1": "CZ", "title": "Kačeří příběhy"},
                      {"iso_3166_1": "CZ", "title": "My z Kačerova"},
                      {"iso_3166_1": "US", "title": "Disney's DuckTales"}])
    disp, orig, lang, czech = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "DuckTales" and orig == "" and lang == "en"
    assert czech == ("Kačeří příběhy", "My z Kačerova")


def test_resolve_english_show_without_czech_titles():
    entry = {"id": 2, "name": "The Office", "original_name": "The Office",
             "original_language": "en"}
    disp, orig, lang, czech = asyncio.run(tmdb._resolve(_Client([]), entry, "tv"))
    assert disp == "The Office" and orig == "" and lang == "en" and czech == ()
