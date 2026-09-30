"""In-memory ring buffer of recent log records, surfaced in the UI Log tab.

A logging handler keeps the last ~1000 records so the web UI can show what
Websharr is doing on the wire — including the Sonarr/Radarr HTTP requests
(captured from uvicorn's access logger) and internal search/download events.
"""

import logging
import re
from collections import deque
from threading import Lock

_MAX = 1000
_BUFFER: deque = deque(maxlen=_MAX)
_LOCK = Lock()
_SEQ = 0

# Loggers the buffer subscribes to. Root catches websharr.*; the uvicorn ones
# do not propagate to root, so attach directly to capture access/error lines.
_TARGET_LOGGERS = ("", "uvicorn.access", "uvicorn.error")


# The UI polls these endpoints every couple of seconds; their access-log lines
# would drown out everything interesting, so keep them out of the buffer.
_SPAM = re.compile(r"GET /ui/api/(status|queue|history|log)\b")


# Sonarr/Radarr/Prowlarr pass the API key in the query string (?apikey=...),
# and uvicorn's access log writes the whole path — so the key ended up in
# `docker logs` and in the UI Log tab. Mask it (and similar secrets) in place.
_SECRET_PARAM = re.compile(r"(?i)\b(apikey|api_key|token|password)=[^&\s\"']+")


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(r"\1=***", text)


class RedactSecrets(logging.Filter):
    """Mask secret query parameters in a record's message and arguments.

    The arguments are rewritten in place, not folded into the message:
    uvicorn's AccessFormatter unpacks the five access-log args itself."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _SEQ
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never let logging break the app
            return
        if record.name == "uvicorn.access" and _SPAM.search(message):
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
    # A logger filter runs before every handler: stdout (docker logs) and the
    # ring buffer both get the masked line.
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactSecrets) for f in access.filters):
        access.addFilter(RedactSecrets())


def records(after: int = 0, limit: int = 500) -> list[dict]:
    """Records with seq > after (oldest first), capped to the newest `limit`."""
    with _LOCK:
        items = [r for r in _BUFFER if r["seq"] > after]
    return items[-limit:]
