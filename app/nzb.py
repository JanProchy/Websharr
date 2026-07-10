"""Fake NZB generation and parsing.

Sonarr/Radarr fetch the "NZB" from our Torznab endpoint themselves and push it
to the SABnzbd API via mode=addfile. The NZB is just a carrier for the Webshare
file ident, name and size, embedded as <meta> entries.
"""

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"


class NzbPayload:
    __slots__ = ("ident", "name", "size")

    def __init__(self, ident: str, name: str, size: int):
        self.ident = ident
        self.name = name
        self.size = size


def build_nzb(ident: str, name: str, size: int) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">
<nzb xmlns="{NZB_NS}">
  <head>
    <meta type="websharr_ident">{escape(ident)}</meta>
    <meta type="websharr_name">{escape(name)}</meta>
    <meta type="websharr_size">{size}</meta>
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
    return NzbPayload(ident=ident, name=meta.get("websharr_name", ident), size=size)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.replace("..", "_").lstrip(".").strip()
    return name or "unnamed"
