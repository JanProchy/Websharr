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
