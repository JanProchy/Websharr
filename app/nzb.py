"""Fake NZB generation and parsing.

Sonarr/Radarr fetch the "NZB" from our Torznab endpoint themselves and push it
to the SABnzbd API via mode=addfile. The NZB is just a carrier for the Webshare
file ident, name and size, embedded as <meta> entries. With the duplicate
fallback on, also the idents of verified copies of the file and the length
(seconds) they were matched on, for the download client to fall back to.
"""

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"


class NzbPayload:
    __slots__ = ("ident", "name", "size", "alternates", "length")

    def __init__(self, ident: str, name: str, size: int,
                 alternates: list[str] | None = None, length: int = 0):
        self.ident = ident
        self.name = name
        self.size = size
        self.alternates = list(alternates or [])
        self.length = length


def build_nzb(ident: str, name: str, size: int,
              alternates: list[str] | None = None, length: int = 0) -> str:
    # Without copies the NZB is exactly what it was before they existed.
    alt = (f'\n    <meta type="websharr_alt">{escape(",".join(alternates))}</meta>'
           f'\n    <meta type="websharr_length">{int(length)}</meta>'
           if alternates and length > 0 else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">
<nzb xmlns="{NZB_NS}">
  <head>
    <meta type="websharr_ident">{escape(ident)}</meta>
    <meta type="websharr_name">{escape(name)}</meta>
    <meta type="websharr_size">{size}</meta>{alt}
  </head>
  <file poster="websharr" date="0" subject={quoteattr(name)}>
    <groups><group>alt.binaries.websharr</group></groups>
    <segments><segment bytes="{size}" number="1">{escape(ident)}@websharr</segment></segments>
  </file>
</nzb>
"""


def parse_nzb(content: bytes) -> NzbPayload | None:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    meta: dict[str, str] = {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "meta" and el.get("type", "").startswith("websharr_"):
            meta[el.get("type", "")] = (el.text or "").strip()

    ident = meta.get("websharr_ident")
    if not ident:
        return None
    try:
        size = int(meta.get("websharr_size", "0"))
    except ValueError:
        size = 0
    # NZBs without websharr_alt (the feature off, or built before it) carry no
    # copies; copies without a length are useless (see DownloadManager.add).
    alternates = [a.strip() for a in meta.get("websharr_alt", "").split(",") if a.strip()]
    try:
        length = int(meta.get("websharr_length", "0"))
    except ValueError:
        length = 0
    return NzbPayload(ident=ident, name=meta.get("websharr_name", ident), size=size,
                      alternates=alternates, length=length)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Collapse runs of dots instead of replacing ".." with "_": with the path
    # separators already gone ".." can't climb out, and "Film 1080p..mkv" (real
    # Webshare names) must keep its ".mkv" or *arr won't see a video to import.
    name = re.sub(r"\.{2,}", ".", name).lstrip(".").strip()
    return name or "unnamed"
