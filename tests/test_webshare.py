import asyncio
import xml.etree.ElementTree as ET

from app.webshare import WebshareClient

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


def test_login_racing_a_credential_change_does_not_cache_old_token(monkeypatch):
    """A login that was running when the credentials changed must not leave
    the old account's token behind for the new account's requests."""
    client = WebshareClient("old", "", "digest-old")
    logins = []

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def fake_login():
            user = client._username
            logins.append(user)
            if user == "old":
                started.set()
                await release.wait()
            return f"token-{user}"

        monkeypatch.setattr(client, "_login", fake_login)
        in_flight = asyncio.create_task(client._get_token())
        await started.wait()
        client.set_credentials("new", "", "digest-new")
        release.set()
        await in_flight
        token = await client._get_token()
        await client.close()
        return token

    assert asyncio.run(run()) == "token-new"
    assert logins == ["old", "new"]
