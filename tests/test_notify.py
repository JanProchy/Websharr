"""Notification helper: URL parsing, no-op without URLs, throttling."""

import asyncio

from app import notify
from app.ui import _parse_urls


def test_parse_urls():
    assert _parse_urls("discord://a,tgram://b\nntfy://c") == ["discord://a", "tgram://b", "ntfy://c"]
    assert _parse_urls(["x", " y ", ""]) == ["x", "y"]
    assert _parse_urls("") == [] and _parse_urls(None) == []


def test_send_without_urls_is_noop():
    assert asyncio.run(notify.send([], "t", "b")) is False


def test_send_throttled(monkeypatch):
    sent = []

    async def fake_send(urls, title, body):
        sent.append(title)
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    notify.reset_throttle("k")
    assert asyncio.run(notify.send_throttled(["x"], "k", "t", "b", 1000)) is True
    # Second call within the interval is suppressed.
    assert asyncio.run(notify.send_throttled(["x"], "k", "t", "b", 1000)) is False
    assert sent == ["t"]
    # After a reset it sends again.
    notify.reset_throttle("k")
    assert asyncio.run(notify.send_throttled(["x"], "k", "t", "b", 1000)) is True
    assert sent == ["t", "t"]
