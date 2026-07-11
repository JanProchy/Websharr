"""TMDB title resolution: English display for foreign-origin titles."""

import asyncio

from app import tmdb


def test_pick_english():
    items = [{"iso_3166_1": "FR", "title": "Les Nineties"},
             {"iso_3166_1": "GB", "title": "Nineties"}]
    assert tmdb._pick_english(items) == "Nineties"
    assert tmdb._pick_english([{"iso_3166_1": "RU", "title": "Девяностые"}]) == ""
    assert tmdb._pick_english([]) == ""


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
    disp, orig, lang = asyncio.run(tmdb._resolve(client, entry, "tv"))
    assert disp == "Nineties" and orig == "Devadesátky" and lang == "cs"


def test_resolve_keeps_existing_english_name():
    # Show that already has a distinct English name must not fetch alt titles.
    class _Boom:
        async def get(self, *a, **k):
            raise AssertionError("should not fetch alternative titles")

    entry = {"id": 1, "name": "The Sleepers", "original_name": "Bez vědomí",
             "original_language": "cs"}
    disp, orig, lang = asyncio.run(tmdb._resolve(_Boom(), entry, "tv"))
    assert disp == "The Sleepers" and orig == "Bez vědomí" and lang == "cs"


def test_resolve_english_show_no_swap():
    class _Boom:
        async def get(self, *a, **k):
            raise AssertionError("English show needs no alt-title lookup")

    entry = {"id": 2, "name": "The Office", "original_name": "The Office",
             "original_language": "en"}
    disp, orig, lang = asyncio.run(tmdb._resolve(_Boom(), entry, "tv"))
    assert disp == "The Office" and orig == "" and lang == "en"
