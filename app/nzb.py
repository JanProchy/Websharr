"""Fake NZB generation and parsing.

Sonarr/Radarr fetch the "NZB" from our Torznab endpoint themselves and push it
to the SABnzbd API via mode=addfile. The NZB is just a carrier for the Webshare
file ident, name and size, embedded as <meta> entries, plus the idents of
identical copies to fall back to when the file's link is dead.
"""

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"


class NzbPayload:
    __slots__ = ("ident", "name", "size", "alternates")

    def __init__(self, ident: str, name: str, size: int, alternates: list[str] | None = None):
        self.ident = ident
        self.name = name
        self.size = size
        self.alternates = list(alternates or [])


def build_nzb(ident: str, name: str, size: int, alternates: list[str] | None = None) -> str:
    alt = f'\n    <meta type="websharr_alt">{escape(",".join(alternates))}</meta>' if alternates else ""
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
    # Older NZBs carry no websharr_alt: no copies to fall back to.
    alternates = [a.strip() for a in meta.get("websharr_alt", "").split(",") if a.strip()]
    return NzbPayload(ident=ident, name=meta.get("websharr_name", ident), size=size,
                      alternates=alternates)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Collapse runs of dots instead of replacing ".." with "_": with the path
    # separators already gone ".." can't climb out, and "Film 1080p..mkv" (real
    # Webshare names) must keep its ".mkv" or *arr won't see a video to import.
    name = re.sub(r"\.{2,}", ".", name).lstrip(".").strip()
    return name or "unnamed"
