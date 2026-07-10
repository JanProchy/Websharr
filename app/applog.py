"""In-memory ring buffer of recent log records, surfaced in the UI Log tab.

A logging handler keeps the last ~1000 records so the web UI can show what
Websharr is doing on the wire — including the Sonarr/Radarr HTTP requests
(captured from uvicorn's access logger) and internal search/download events.
"""

import logging
from collections import deque
from threading import Lock

_MAX = 1000
_BUFFER: deque = deque(maxlen=_MAX)
_LOCK = Lock()
_SEQ = 0

# Loggers the buffer subscribes to. Root catches websharr.*; the uvicorn ones
# do not propagate to root, so attach directly to capture access/error lines.
_TARGET_LOGGERS = ("", "uvicorn.access", "uvicorn.error")


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _SEQ
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging break the app
            return
        with _LOCK:
            _SEQ += 1
            _BUFFER.append({
                "seq": _SEQ,
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            })


def install(level: int = logging.INFO) -> None:
    handler = RingBufferHandler()
    handler.setLevel(level)
    for name in _TARGET_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(h, RingBufferHandler) for h in lg.handlers):
            lg.addHandler(handler)


def records(after: int = 0, limit: int = 500) -> list[dict]:
    """Records with seq > after (oldest first), capped to the newest `limit`."""
    with _LOCK:
        items = [r for r in _BUFFER if r["seq"] > after]
    return items[-limit:]
