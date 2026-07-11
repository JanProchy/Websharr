"""Outbound notifications via Apprise.

Users configure one or more Apprise URLs (Discord, Telegram, ntfy, Slack, …) in
Settings; Websharr sends a short message on notable events (a failed download,
Webshare unreachable, VIP running low). All sending is best-effort and never
raises into the caller — a broken notifier must not break a download.
"""

import asyncio
import logging
import time

logger = logging.getLogger("websharr.notify")

try:  # apprise is optional at import time so the app still boots without it
    import apprise
except ImportError:  # pragma: no cover
    apprise = None

# Per-key throttle so recurring conditions (VIP low, Webshare down) don't spam.
_last_sent: dict[str, float] = {}


async def send(urls: list[str], title: str, body: str) -> bool:
    """Send to every configured Apprise URL. Returns True if anything was sent."""
    urls = [u for u in (urls or []) if u]
    if not urls:
        return False
    if apprise is None:
        logger.warning("apprise not installed; cannot send %r", title)
        return False
    ap = apprise.Apprise()
    for url in urls:
        ap.add(url)
    try:
        ok = await asyncio.to_thread(ap.notify, body=body, title=title)
        if not ok:
            logger.warning("Notification %r reported failure", title)
        return bool(ok)
    except Exception as exc:  # apprise can raise on bad URLs/network
        logger.warning("Notification %r failed: %s", title, exc)
        return False


async def send_throttled(urls: list[str], key: str, title: str, body: str,
                         min_interval: float) -> bool:
    """Like send(), but at most once per `min_interval` seconds for `key`."""
    now = time.monotonic()
    if now - _last_sent.get(key, 0.0) < min_interval:
        return False
    _last_sent[key] = now
    return await send(urls, title, body)


def reset_throttle(key: str) -> None:
    """Clear a throttle key so the next matching event notifies immediately
    (e.g. once VIP recovers, a later expiry warns again straight away)."""
    _last_sent.pop(key, None)
