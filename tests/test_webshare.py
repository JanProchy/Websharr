import asyncio
import time
import xml.etree.ElementTree as ET

import pytest

from app.webshare import WebshareClient, WebshareError

FILE_INFO_XML = """<response><status>OK</status><name>x.mkv</name><type>mkv</type>
<length>1472</length><format>H264</format><width>1920</width><height>1080</height>
<video><stream><format>H264</format><width>1920</width><height>1080</height></stream></video>
<audio>
<stream><format>AC3</format><channels>2</channels><language>CZE</language></stream>
<stream><format>AC3</format><channels>6</channels><language>eng</language></stream>
<stream><format>MP3</format><channels>2</channels><language></language></stream>
</audio></response>"""


def test_file_info_reads_audio_languages(monkeypatch):
    client = WebshareClient("user", "pw")

    async def fake_post(path, data):
        assert path == "/file_info/"
        return ET.fromstring(FILE_INFO_XML)

    monkeypatch.setattr(client, "_authed_post", fake_post)
    info = asyncio.run(client.file_info("abc"))
    assert info["height"] == 1080
    # Untagged tracks are skipped; codes are upper-cased.
    assert info["audio_languages"] == ["CZE", "ENG"]


SEARCH_XML = """<response><status>OK</status>
<file><ident>a1</ident><name>Show.S01E01.mkv</name><size>100</size><type>mkv</type></file>
</response>"""


def _counting_client(monkeypatch, ttl=600, fail=False, delay=0.0):
    client = WebshareClient("user", "pw", search_cache_ttl=ttl)
    calls = []

    async def fake_post(path, data):
        assert path == "/search/"
        calls.append(dict(data))
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise WebshareError("Webshare /search/ failed: 403")
        return ET.fromstring(SEARCH_XML)

    monkeypatch.setattr(client, "_authed_post", fake_post)
    return client, calls


def test_search_repeated_within_ttl_is_cached(monkeypatch):
    client, calls = _counting_client(monkeypatch)

    async def run():
        first = await client.search("show s01e01")
        first.clear()  # a caller mutating its list must not touch the cache
        return await client.search("show s01e01")

    second = asyncio.run(run())
    assert len(calls) == 1
    assert [r.ident for r in second] == ["a1"]


def test_search_different_limit_or_offset_is_not_shared(monkeypatch):
    client, calls = _counting_client(monkeypatch)

    async def run():
        await client.search("show", limit=60, offset=0)
        await client.search("show", limit=60, offset=60)
        await client.search("show", limit=100, offset=0)
        await client.search("other", limit=60, offset=0)

    asyncio.run(run())
    assert len(calls) == 4


def test_search_cache_expires(monkeypatch):
    client, calls = _counting_client(monkeypatch, ttl=600)
    real = time.monotonic
    shift = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: real() + shift)

    async def run():
        nonlocal shift
        await client.search("show")
        shift = 599
        await client.search("show")
        assert len(calls) == 1
        shift = 601
        await client.search("show")

    asyncio.run(run())
    assert len(calls) == 2


def test_search_errors_are_not_cached(monkeypatch):
    client, calls = _counting_client(monkeypatch, fail=True)

    async def run():
        for _ in range(2):
            with pytest.raises(WebshareError):
                await client.search("show")

    asyncio.run(run())
    assert len(calls) == 2


def test_search_cache_ttl_zero_disables(monkeypatch):
    client, calls = _counting_client(monkeypatch, ttl=0)

    async def run():
        await client.search("show")
        await client.search("show")

    asyncio.run(run())
    assert len(calls) == 2


def test_concurrent_identical_searches_send_one_request(monkeypatch):
    client, calls = _counting_client(monkeypatch, delay=0.05)

    async def run():
        return await asyncio.gather(*(client.search("show") for _ in range(5)))

    results = asyncio.run(run())
    assert len(calls) == 1
    assert all([r.ident for r in res] == ["a1"] for res in results)
