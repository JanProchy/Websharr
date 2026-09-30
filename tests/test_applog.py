"""Log capture: secrets in request paths never reach the logs."""

import logging

from app import applog


def test_redact_masks_api_key_and_similar():
    path = "GET /torznab/api?t=movie&apikey=1e9c5916abc&tmdbid=862 HTTP/1.1"
    assert applog.redact(path) == "GET /torznab/api?t=movie&apikey=***&tmdbid=862 HTTP/1.1"
    assert applog.redact("/sabnzbd/api?mode=queue&APIKEY=x&output=json") == \
        "/sabnzbd/api?mode=queue&APIKEY=***&output=json"
    assert applog.redact("/torznab/nzb/abc?apikey=k&name=a.mkv") == "/torznab/nzb/abc?apikey=***&name=a.mkv"
    assert applog.redact("GET /health HTTP/1.1") == "GET /health HTTP/1.1"


def test_access_log_line_is_masked_for_every_handler():
    """uvicorn formats access lines from args; the filter must mask the final
    message for stdout and the UI ring buffer alike."""
    applog.install()
    access = logging.getLogger("uvicorn.access")
    seen = []

    class Grab(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    grab = Grab()
    access.addHandler(grab)
    try:
        access.info('%s - "%s %s HTTP/%s" %d', "172.19.0.27:1", "GET",
                    "/torznab/api?t=caps&apikey=secret123", "1.1", 200)
    finally:
        access.removeHandler(grab)
    assert seen and "secret123" not in seen[-1] and "apikey=***" in seen[-1]
    assert not any("secret123" in r["message"] for r in applog.records())


def test_uvicorn_access_formatter_still_works():
    """uvicorn formats access lines itself from the five args: masking must keep
    them (an empty args tuple broke every access-log line)."""
    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("172.19.0.16:1", "GET", "/sabnzbd/api?mode=queue&apikey=secret9", "1.1", 200),
                               None)
    applog.RedactSecrets().filter(record)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert "secret9" not in line and "apikey=***" in line and "200" in line
