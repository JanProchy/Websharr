"""Persisted runtime settings — UI account, Webshare credentials, API key.

Stored as JSON (SETTINGS_FILE, default /config/settings.json) so values
survive restarts. Saved settings override environment variables; env vars
remain the initial defaults so existing deployments keep working.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from .config import config

logger = logging.getLogger("websharr.settings")

PBKDF2_ITERATIONS = 200_000
SESSION_TTL = 14 * 86400  # seconds


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"pbkdf2:{PBKDF2_ITERATIONS}:{salt}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, expected = stored.split(":")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


class Settings:
    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.auth_username = ""
        self.auth_password_hash = ""
        self.webshare_username = ""
        self.webshare_password = ""
        self.api_key = ""
        self.secret = secrets.token_hex(32)

    @property
    def configured(self) -> bool:
        return bool(self.auth_username and self.auth_password_hash)

    def load(self) -> None:
        self._reset()
        path = config.settings_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not read %s: %s", path, exc)
            return
        auth = data.get("auth", {})
        self.auth_username = auth.get("username", "")
        self.auth_password_hash = auth.get("password_hash", "")
        ws = data.get("webshare", {})
        self.webshare_username = ws.get("username", "")
        self.webshare_password = ws.get("password", "")
        self.api_key = data.get("api_key", "")
        self.secret = data.get("secret") or self.secret

    def save(self) -> None:
        path = config.settings_file
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "auth": {"username": self.auth_username, "password_hash": self.auth_password_hash},
            "webshare": {"username": self.webshare_username, "password": self.webshare_password},
            "api_key": self.api_key,
            "secret": self.secret,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        path.chmod(0o600)

    def apply(self) -> None:
        """Push saved values into the runtime config (settings win over env)."""
        if self.api_key:
            config.api_key = self.api_key
        if self.webshare_username:
            config.webshare_username = self.webshare_username
            config.webshare_password = self.webshare_password

    # -- session cookies ----------------------------------------------------

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def issue_session(self) -> str:
        payload = f"{self.auth_username}|{int(time.time()) + SESSION_TTL}"
        # Strip base64 padding so the token needs no cookie quoting.
        payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return payload_b64 + "." + self._sign(payload)

    def verify_session(self, token: str) -> bool:
        try:
            payload_b64, sig = token.split(".", 1)
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = base64.urlsafe_b64decode(payload_b64.encode()).decode()
            username, exp_raw = payload.rsplit("|", 1)
            expires = int(exp_raw)
        except (ValueError, UnicodeDecodeError):
            return False
        if not hmac.compare_digest(self._sign(payload), sig):
            return False
        return username == self.auth_username and time.time() < expires


settings = Settings()
