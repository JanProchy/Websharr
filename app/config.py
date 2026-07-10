"""Runtime configuration loaded from environment variables."""

import os
from pathlib import Path


class Config:
    def __init__(self) -> None:
        self.webshare_username = os.environ.get("WEBSHARE_USERNAME", "")
        self.webshare_password = os.environ.get("WEBSHARE_PASSWORD", "")
        # Single shared key used both as the Torznab apikey and the SABnzbd apikey.
        # Empty env var counts as unset (compose passes empty defaults).
        self.api_key = os.environ.get("WEBSHARR_API_KEY") or "websharr"
        self.complete_dir = Path(os.environ.get("COMPLETE_DIR", "/downloads/complete"))
        self.incomplete_dir = Path(os.environ.get("INCOMPLETE_DIR", "/downloads/incomplete"))
        self.state_file = Path(os.environ.get("STATE_FILE", "/config/state.json"))
        self.settings_file = Path(os.environ.get("SETTINGS_FILE", "/config/settings.json"))
        self.max_concurrent = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
        self.search_limit = int(os.environ.get("SEARCH_LIMIT", "60"))
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()


config = Config()
