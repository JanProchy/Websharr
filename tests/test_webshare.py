import asyncio
import time
import xml.etree.ElementTree as ET

import pytest

from app.webshare import SEARCH_CACHE_EMPTY_TTL, WebshareClient, WebshareError

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


async def _until(cond, steps: int = 100) -> None:
    """Let the other tasks run until cond() holds; fail instead of hanging."""
    for _ in range(steps):
        if cond():
            return
        await asyncio.sleep(0)
    assert cond()


def test_search_empty_result_expires_sooner(monkeypatch):
    client, calls = _counting_client(monkeypatch, ttl=600)
    real = time.monotonic
    shift = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: real() + shift)

    async def empty(path, data):
        calls.append(dict(data))
        return ET.fromstring("<response><status>OK</status></response>")

    monkeypatch.setattr(client, "_authed_post", empty)

    async def run():
        nonlocal shift
        assert await client.search("nothing") == []
        shift = SEARCH_CACHE_EMPTY_TTL - 1
        await client.search("nothing")
        assert len(calls) == 1
        shift = SEARCH_CACHE_EMPTY_TTL + 1
        await client.search("nothing")

    asyncio.run(run())
    assert len(calls) == 2


def test_search_cache_ttl_zero_does_not_coalesce(monkeypatch):
    client = WebshareClient("user", "pw")  # default: cache off
    started = []

    async def run():
        gate = asyncio.Event()

        async def fake_post(path, data):
            started.append(path)
            await gate.wait()
            return ET.fromstring(SEARCH_XML)

        monkeypatch.setattr(client, "_authed_post", fake_post)
        tasks = [asyncio.ensure_future(client.search("show")) for _ in range(2)]
        await _until(lambda: len(started) == 2)  # both on the wire at once
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert started == ["/search/", "/search/"]


def test_search_cache_ttl_defaults_to_off(monkeypatch):
    from app.config import Config
    monkeypatch.delenv("SEARCH_CACHE_TTL", raising=False)
    assert Config().search_cache_ttl == 0
    monkeypatch.setenv("SEARCH_CACHE_TTL", "600")
    assert Config().search_cache_ttl == 600


def test_mutating_a_returned_result_does_not_change_later_hits(monkeypatch):
    client, calls = _counting_client(monkeypatch)

    async def run():
        first = await client.search("show")
        first[0].name = "changed"
        first[0].password = True
        return await client.search("show")

    second = asyncio.run(run())
    assert len(calls) == 1
    assert second[0].name == "Show.S01E01.mkv"
    assert second[0].password is False


def test_coalesced_callers_get_their_own_result_objects(monkeypatch):
    client, calls = _counting_client(monkeypatch)

    async def run():
        return await asyncio.gather(client.search("show"), client.search("show"))

    a, b = asyncio.run(run())
    assert len(calls) == 1
    assert a[0] is not b[0]
    a[0].name = "changed"
    assert b[0].name == "Show.S01E01.mkv"


# --- credential lifecycle ----------------------------------------------------
# These drive the real login/_authed_post path: only _post is faked. A search
# answers with an ident naming the account whose token was sent, so a result
# fetched under the wrong account is visible in the assertions.

class _FakeWebshare:
    def __init__(self, client: WebshareClient):
        self.client = client
        self.searches: list[str] = []  # account (token owner) of each /search/
        self.logins: list[str] = []
        self.search_gate: asyncio.Event | None = None  # set = searches may answer
        self.login_gate: asyncio.Event | None = None
        self.search_started = asyncio.Event()
        self.login_started = asyncio.Event()

    async def post(self, path, data):
        if path == "/salt/":
            return ET.fromstring("<response><status>OK</status><salt>s</salt></response>")
        if path == "/login/":
            user = data["username_or_email"]
            self.logins.append(user)
            self.login_started.set()
            if self.login_gate is not None:
                await self.login_gate.wait()
            return ET.fromstring(
                f"<response><status>OK</status><token>tok-{user}</token></response>")
        assert path == "/search/"
        account = data["wst"].removeprefix("tok-")
        self.searches.append(account)
        self.search_started.set()
        if self.search_gate is not None:
            await self.search_gate.wait()
        return ET.fromstring(
            "<response><status>OK</status><file><ident>" + account + "</ident>"
            "<name>Show.S01E01.mkv</name><size>100</size></file></response>")


def _lifecycle_client(monkeypatch):
    client = WebshareClient("alice", "", "digest-a", search_cache_ttl=600)
    fake = _FakeWebshare(client)
    monkeypatch.setattr(client, "_post", fake.post)
    return client, fake


def test_search_cache_does_not_outlive_an_account_change(monkeypatch):
    client, fake = _lifecycle_client(monkeypatch)

    async def run():
        assert [r.ident for r in await client.search("show")] == ["alice"]
        client.set_credentials("bob", password_digest="digest-b")
        assert [r.ident for r in await client.search("show")] == ["bob"]
        assert [r.ident for r in await client.search("show")] == ["bob"]  # cached

    asyncio.run(run())
    assert fake.searches == ["alice", "bob"]


def test_search_cache_does_not_outlive_cleared_credentials(monkeypatch):
    client, fake = _lifecycle_client(monkeypatch)

    async def run():
        await client.search("show")
        client.set_credentials("", "", "")
        with pytest.raises(WebshareError, match="not configured"):
            await client.search("show")

    asyncio.run(run())
    assert fake.searches == ["alice"]


def test_unchanged_credentials_keep_the_search_cache(monkeypatch):
    # Every UI settings save re-applies the same credentials.
    client, fake = _lifecycle_client(monkeypatch)

    async def run():
        await client.search("show")
        client.set_credentials("alice", "", "digest-a")
        assert [r.ident for r in await client.search("show")] == ["alice"]

    asyncio.run(run())
    assert fake.searches == ["alice"]


def test_new_account_does_not_join_an_old_in_flight_search(monkeypatch):
    client, fake = _lifecycle_client(monkeypatch)

    async def run():
        fake.search_gate = asyncio.Event()
        old = asyncio.ensure_future(client.search("show"))
        await fake.search_started.wait()  # alice's request is on the wire
        client.set_credentials("bob", password_digest="digest-b")
        new = asyncio.ensure_future(client.search("show"))
        await _until(lambda: len(fake.searches) == 2)  # bob sent his own request
        # Bob's answer lands first, then alice's late one.
        fake.search_gate.set()
        new_res = await new
        old_res = await old
        assert [r.ident for r in old_res] == ["alice"]  # the old caller still gets its answer
        assert [r.ident for r in new_res] == ["bob"]
        # Alice's late completion must not have overwritten bob's cache entry.
        assert [r.ident for r in await client.search("show")] == ["bob"]

    asyncio.run(run())
    assert fake.searches == ["alice", "bob"]


def test_late_old_account_search_does_not_repopulate_the_cache(monkeypatch):
    client, fake = _lifecycle_client(monkeypatch)

    async def run():
        fake.search_gate = asyncio.Event()
        old = asyncio.ensure_future(client.search("show"))
        await fake.search_started.wait()
        client.set_credentials("", "", "")
        fake.search_gate.set()
        assert [r.ident for r in await old] == ["alice"]
        with pytest.raises(WebshareError, match="not configured"):
            await client.search("show")
        client.set_credentials("bob", password_digest="digest-b")
        assert [r.ident for r in await client.search("show")] == ["bob"]

    asyncio.run(run())
    assert fake.searches == ["alice", "bob"]


def test_login_racing_an_account_change_logs_in_as_the_new_account(monkeypatch):
    client, fake = _lifecycle_client(monkeypatch)

    async def run():
        fake.login_gate = asyncio.Event()
        old = asyncio.ensure_future(client.search("show"))
        await fake.login_started.wait()  # alice's login is on the wire
        client.set_credentials("bob", password_digest="digest-b")
        new = asyncio.ensure_future(client.search("show"))
        fake.login_gate.set()
        await asyncio.gather(old, new)
        # Alice's token arrived after the switch and was thrown away.
        assert [r.ident for r in await client.search("show")] == ["bob"]

    asyncio.run(run())
    assert fake.logins == ["alice", "bob"]
    assert "alice" not in fake.searches
