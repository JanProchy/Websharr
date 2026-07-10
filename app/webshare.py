"""Webshare.cz API client.

Webshare authenticates with SHA1(md5crypt(password, salt)) where the salt is
fetched per-user from /api/salt/. Responses are XML.
"""

import asyncio
import hashlib
import logging
import time
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger("websharr.webshare")

API_BASE = "https://webshare.cz/api"
TOKEN_TTL = 30 * 60  # re-login after 30 minutes

ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(value: int, length: int) -> str:
    out = ""
    for _ in range(length):
        out += ITOA64[value & 0x3F]
        value >>= 6
    return out


def md5crypt(password: str, salt: str) -> str:
    """FreeBSD-style MD5 crypt ($1$), as used by Webshare login."""
    magic = b"$1$"
    pw = password.encode("utf-8")
    salt_b = salt.encode("utf-8")
    if salt_b.startswith(magic):
        salt_b = salt_b[len(magic):]
    salt_b = salt_b.split(b"$")[0][:8]

    ctx = pw + magic + salt_b
    final = hashlib.md5(pw + salt_b + pw).digest()

    pl = len(pw)
    while pl > 0:
        ctx += final[: min(pl, 16)]
        pl -= 16

    i = len(pw)
    while i:
        if i & 1:
            ctx += b"\x00"
        else:
            ctx += pw[:1]
        i >>= 1

    final = hashlib.md5(ctx).digest()

    for rnd in range(1000):
        ctx1 = b""
        ctx1 += pw if rnd & 1 else final
        if rnd % 3:
            ctx1 += salt_b
        if rnd % 7:
            ctx1 += pw
        ctx1 += final if rnd & 1 else pw
        final = hashlib.md5(ctx1).digest()

    out = ""
    for a, b, c in ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5)):
        out += _to64((final[a] << 16) | (final[b] << 8) | final[c], 4)
    out += _to64(final[11], 2)

    return magic.decode() + salt_b.decode() + "$" + out


class WebshareError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class SearchResult:
    __slots__ = ("ident", "name", "size", "positive_votes", "negative_votes", "password")

    def __init__(self, ident: str, name: str, size: int,
                 positive_votes: int = 0, negative_votes: int = 0, password: bool = False):
        self.ident = ident
        self.name = name
        self.size = size
        self.positive_votes = positive_votes
        self.negative_votes = negative_votes
        self.password = password


def _parse_response(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise WebshareError(f"Invalid XML response from Webshare: {exc}") from exc
    return root


def _text(root: ET.Element, tag: str, default: str = "") -> str:
    el = root.find(tag)
    return el.text if el is not None and el.text is not None else default


class WebshareClient:
    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password
        self._token: str | None = None
        self._token_ts: float = 0.0
        self._login_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Accept": "text/xml; charset=UTF-8"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, data: dict) -> ET.Element:
        resp = await self._http.post(path, data=data)
        resp.raise_for_status()
        root = _parse_response(resp.text)
        status = _text(root, "status")
        if status != "OK":
            raise WebshareError(
                f"Webshare {path} failed: {_text(root, 'message', status)}",
                code=_text(root, "code"),
            )
        return root

    async def _login(self) -> str:
        if not self._username or not self._password:
            raise WebshareError("WEBSHARE_USERNAME / WEBSHARE_PASSWORD not configured")

        salt_root = await self._post("/salt/", {"username_or_email": self._username})
        salt = _text(salt_root, "salt")

        password_hash = hashlib.sha1(md5crypt(self._password, salt).encode()).hexdigest()
        login_root = await self._post(
            "/login/",
            {
                "username_or_email": self._username,
                "password": password_hash,
                "keep_logged_in": "1",
            },
        )
        token = _text(login_root, "token")
        if not token:
            raise WebshareError("Webshare login returned no token")
        logger.info("Logged in to Webshare as %s", self._username)
        return token

    async def _get_token(self, force: bool = False) -> str:
        async with self._login_lock:
            if force or not self._token or time.monotonic() - self._token_ts > TOKEN_TTL:
                self._token = await self._login()
                self._token_ts = time.monotonic()
            return self._token

    async def _authed_post(self, path: str, data: dict) -> ET.Element:
        data = dict(data)
        data["wst"] = await self._get_token()
        try:
            return await self._post(path, data)
        except WebshareError as exc:
            # Token may have expired server-side; re-login once and retry.
            if "not logged" in str(exc).lower() or exc.code in ("LOGIN_FATAL_1",):
                data["wst"] = await self._get_token(force=True)
                return await self._post(path, data)
            raise

    async def search(self, query: str, limit: int = 60, offset: int = 0) -> list[SearchResult]:
        root = await self._authed_post(
            "/search/",
            {
                "what": query,
                "category": "video",
                "sort": "largest",
                "limit": str(limit),
                "offset": str(offset),
            },
        )
        results: list[SearchResult] = []
        for f in root.findall("file"):
            try:
                results.append(
                    SearchResult(
                        ident=_text(f, "ident"),
                        name=_text(f, "name"),
                        size=int(_text(f, "size", "0")),
                        positive_votes=int(_text(f, "positive_votes", "0")),
                        negative_votes=int(_text(f, "negative_votes", "0")),
                        password=_text(f, "password", "0") == "1",
                    )
                )
            except ValueError:
                continue
        return results

    async def file_link(self, ident: str) -> str:
        root = await self._authed_post("/file_link/", {"ident": ident, "force_https": "1"})
        link = _text(root, "link")
        if not link:
            raise WebshareError("Webshare returned no download link")
        return link

    async def check_credentials(self) -> bool:
        try:
            await self._get_token(force=True)
            return True
        except (WebshareError, httpx.HTTPError) as exc:
            logger.error("Webshare credential check failed: %s", exc)
            return False
