"""Runtime configuration loaded from environment variables."""

import os
import re
from pathlib import Path


class Config:
    def __init__(self) -> None:
        self.webshare_username = os.environ.get("WEBSHARE_USERNAME", "")
        self.webshare_password = os.environ.get("WEBSHARE_PASSWORD", "")
        self.webshare_password_digest = ""  # filled from settings.json, never from env
        # Single shared key used both as the Torznab apikey and the SABnzbd apikey.
        # Empty env var counts as unset (compose passes empty defaults).
        self.api_key = os.environ.get("WEBSHARR_API_KEY") or "websharr"
        # TMDB API Read Access Token (v4 bearer) for Czech-title lookups; the UI
        # value in settings.json overrides this.
        self.tmdb_token = os.environ.get("TMDB_TOKEN", "")
        # Drop files whose duration is far off the TMDB runtime (opt-in, off by
        # default); the UI value in settings.json overrides this.
        self.runtime_check = os.environ.get("RUNTIME_CHECK", "").strip().lower() in ("1", "true", "yes", "on")
        # Measure every shown file and add the reported codecs and audio-track
        # languages to the feed (opt-in, off by default); the UI value wins.
        self.measured_metadata = os.environ.get("MEASURED_METADATA", "").strip().lower() in (
            "1", "true", "yes", "on")
        # Inferences from the measurement: correct an overstated resolution and
        # mark upscales (opt-in, off by default); the UI value wins.
        self.quality_heuristics = os.environ.get("QUALITY_HEURISTICS", "").strip().lower() in (
            "1", "true", "yes", "on")
        # Drop files measured under 3 MiB per minute (stubs; opt-in, off by
        # default); the UI value wins.
        self.reject_stubs = os.environ.get("REJECT_STUBS", "").strip().lower() in ("1", "true", "yes", "on")
        # Apprise notification URLs (comma/newline separated) and the VIP-expiry
        # warning threshold in days; UI values override these.
        self.notify_urls = [u.strip() for u in re.split(r"[,\n]", os.environ.get("NOTIFY_URLS", "")) if u.strip()]
        self.notify_vip_days = int(os.environ.get("NOTIFY_VIP_DAYS", "7"))
        self.complete_dir = Path(os.environ.get("COMPLETE_DIR", "/downloads/complete"))
        self.incomplete_dir = Path(os.environ.get("INCOMPLETE_DIR", "/downloads/incomplete"))
        self.state_file = Path(os.environ.get("STATE_FILE", "/config/state.json"))
        self.settings_file = Path(os.environ.get("SETTINGS_FILE", "/config/settings.json"))
        self.max_concurrent = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
        self.search_limit = int(os.environ.get("SEARCH_LIMIT", "60"))
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()


config = Config()
