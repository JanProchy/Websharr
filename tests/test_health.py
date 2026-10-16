import asyncio
import os
import threading
import time

import pytest

import app.main as app_main
from app.config import config
from app.main import ACCOUNT_REFRESH, app
from app.webshare import WebshareError

from .conftest import wait_for

OLD, NEW = "ws@example.com", "new@example.com"


@pytest.fixture
def no_account(monkeypatch):
    # Keep the background monitor idle so it can't race the state set by tests.
    monkeypatch.setattr(config, "webshare_username", "")


@pytest.fixture
def healthy(no_account, client, monkeypatch):
    monkeypatch.setattr(config, "webshare_username", "ws@example.com")
    app.state.account = {"vip": True, "vip_days": 30}
    app.state.account_ts = time.time()
    app.state.account_refresh_ok = True
    return client


def test_ready_ok(healthy):
    resp = healthy.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["storage"]["status"] == "ok"
    assert body["checks"]["webshare"]["status"] == "ok"
    # Unauthenticated endpoint: no account name, no paths.
    assert "ws@example.com" not in resp.text
    assert str(config.complete_dir) not in resp.text


def test_ready_pending_at_startup(no_account, client, monkeypatch):
    # Not verified yet is not ready; /health is unaffected.
    monkeypatch.setattr(config, "webshare_username", "ws@example.com")
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "pending"
    assert resp.json()["checks"]["webshare"]["status"] == "pending"
    assert client.get("/health").status_code == 200


def test_ready_first_refresh_failed(no_account, client, monkeypatch):
    monkeypatch.setattr(config, "webshare_username", "ws@example.com")
    app.state.account_refresh_ok = False
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["webshare"]["status"] == "error"


def test_ready_not_configured(no_account, client):
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["checks"]["webshare"]["reason"]


def test_health_is_plain_liveness(no_account, client, monkeypatch, tmp_path):
    """/health stays the unconditional liveness the Docker HEALTHCHECK polls:
    same body as before, 200 even when /ready fails."""
    monkeypatch.setattr(config, "complete_dir", tmp_path / "gone")
    assert client.get("/ready").status_code == 503
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ready_stale_account(healthy):
    app.state.account_ts = time.time() - 4 * ACCOUNT_REFRESH
    app.state.account_refresh_ok = False
    resp = healthy.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"
    assert "not refreshed" in resp.json()["checks"]["webshare"]["reason"]


def test_ready_tolerates_one_failed_refresh(healthy):
    app.state.account_ts = time.time() - ACCOUNT_REFRESH
    app.state.account_refresh_ok = False
    assert healthy.get("/ready").status_code == 200


def test_ready_vip_expired(healthy):
    app.state.account = {"vip": False, "vip_days": 0}
    resp = healthy.get("/ready")
    assert resp.status_code == 503
    assert "VIP" in resp.json()["checks"]["webshare"]["reason"]


def test_ready_dir_not_writable(healthy, monkeypatch):
    # os.access instead of chmod: tests may run as root, which ignores modes.
    monkeypatch.setattr(app_main.os, "access", lambda path, mode: False)
    resp = healthy.get("/ready")
    assert resp.status_code == 503
    assert "not writable" in resp.json()["checks"]["storage"]["reason"]


def test_ready_checks_search_permission(healthy, monkeypatch):
    # Grant w but not x: the dir can't be traversed, so nothing can be created.
    real_access = app_main.os.access
    monkeypatch.setattr(app_main.os, "access",
                        lambda path, mode: not (mode & os.X_OK) and real_access(path, mode))
    resp = healthy.get("/ready")
    assert resp.status_code == 503
    assert "not writable" in resp.json()["checks"]["storage"]["reason"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root ignores directory permission bits")
def test_ready_dir_without_search_permission(healthy):
    """A real 0666 dir: writable on paper, but no file can be created in it."""
    path = config.incomplete_dir
    path.chmod(0o666)
    try:
        with pytest.raises(PermissionError):
            (path / "probe").touch()
        resp = healthy.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["checks"]["storage"]["reason"] == "incomplete dir not writable"
    finally:
        path.chmod(0o755)


def test_ready_dir_missing(healthy, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "complete_dir", tmp_path / "gone")
    resp = healthy.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["storage"]["reason"] == "complete dir missing"
    assert str(tmp_path) not in resp.text


def test_monitor_refreshes_right_away_when_woken(monkeypatch):
    """Saving new Webshare credentials wakes the monitor: /ready doesn't stay
    pending (or failed) until the next hourly refresh."""
    from types import SimpleNamespace

    calls = []

    class Client:
        async def account_status(self):
            calls.append(time.monotonic())
            return {"vip": True, "vip_days": 30, "vip_until": "2027-01-01"}

    monkeypatch.setattr(config, "webshare_username", "ws@example.com")
    fake = SimpleNamespace(state=SimpleNamespace(webshare=Client(), account=None, account_ts=0.0,
                                                 account_refresh_ok=None, account_gen=0))

    async def run():
        fake.state.account_refresh = asyncio.Event()
        task = asyncio.create_task(app_main._account_monitor(fake))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if calls:
                break
        fake.state.account_refresh.set()  # what the UI does after a credential change
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(calls) == 2:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert len(calls) == 2 and fake.state.account_refresh_ok is True
    assert ACCOUNT_REFRESH > 60  # the second call came from the wake-up, not the timer


class GatedAccountStatus:
    """account_status for the fake client: records which account each refresh
    ran as and holds its answer until the test releases that account."""

    def __init__(self, ws):
        self.ws = ws
        self.calls: list[str] = []
        self.failing: set[str] = set()
        self._gates: dict[str, threading.Event] = {}

    def _gate(self, user: str) -> threading.Event:
        return self._gates.setdefault(user, threading.Event())

    def release(self, user: str) -> None:
        self._gate(user).set()

    async def __call__(self) -> dict:
        user = self.ws.username  # the credentials this refresh started with
        self.calls.append(user)
        while not self._gate(user).is_set():  # runs on the app's loop thread
            await asyncio.sleep(0.005)
        if user in self.failing:
            raise WebshareError("Webshare /login/ failed: Invalid credentials")
        return {"username": user, "vip": True, "vip_days": 30, "vip_until": "2027-01-01"}


@pytest.fixture
def account(healthy, fake_webshare, monkeypatch):
    """Healthy OLD account whose refreshes the test controls."""
    # settings.apply() rewrites these; monkeypatch puts them back afterwards.
    monkeypatch.setattr(config, "webshare_password", "secret")
    monkeypatch.setattr(config, "webshare_password_digest", "")
    fake_webshare.set_credentials(OLD, "secret")
    gated = GatedAccountStatus(fake_webshare)
    monkeypatch.setattr(fake_webshare, "account_status", gated, raising=False)
    yield gated
    for user in (OLD, NEW):  # don't leave the monitor parked on a gate
        gated.release(user)


def _save(client, **body):
    resp = client.post("/ui/api/settings", params={"apikey": "testkey"}, json=body)
    assert resp.status_code == 200, resp.text


def _switch_to_new_account(client):
    _save(client, webshare_username=NEW, webshare_password="other-secret")


def _refresh_old_account_in_flight(client, account):
    # Wake the monitor on its own loop, as the hourly timer would.
    client.portal.call(app.state.account_refresh.set)
    assert wait_for(lambda: account.calls == [OLD])


def test_ready_credential_switch_to_failing_account(healthy, account):
    """The old account's recent success must not vouch for new credentials
    whose very first refresh fails (no grace across a credential change)."""
    account.failing.add(NEW)
    account.release(NEW)
    _switch_to_new_account(healthy)
    assert wait_for(lambda: app.state.account_refresh_ok is False)
    assert account.calls == [NEW]

    resp = healthy.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["webshare"]["reason"] == "Webshare account refresh failed"
    assert app.state.account is None  # the old VIP state is gone from /stats too
    resp = healthy.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


def test_ready_ignores_old_account_success_landing_after_switch(healthy, account):
    """A refresh started under the old credentials that succeeds after the
    switch must not mark the new account ready."""
    _refresh_old_account_in_flight(healthy, account)
    _switch_to_new_account(healthy)
    account.release(OLD)
    # The monitor finished (and dropped) the old refresh before starting the new one.
    assert wait_for(lambda: account.calls == [OLD, NEW])
    resp = healthy.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["webshare"]["status"] == "pending"
    assert app.state.account is None and app.state.account_ts == 0.0

    # And when the new account then fails, nothing old keeps /ready up.
    account.failing.add(NEW)
    account.release(NEW)
    assert wait_for(lambda: app.state.account_refresh_ok is False)
    assert healthy.get("/ready").status_code == 503


def test_ready_ignores_old_account_failure_landing_after_switch(healthy, account):
    """The other direction: the old account's refresh failing after the switch
    must not count against the new account, whose own result decides."""
    account.failing.add(OLD)
    _refresh_old_account_in_flight(healthy, account)
    _switch_to_new_account(healthy)
    account.release(OLD)
    assert wait_for(lambda: account.calls == [OLD, NEW])
    assert app.state.account_refresh_ok is None  # still pending, not failed
    assert healthy.get("/ready").json()["checks"]["webshare"]["status"] == "pending"

    account.release(NEW)
    assert wait_for(lambda: app.state.account_refresh_ok is True)
    assert app.state.account["username"] == NEW
    assert healthy.get("/ready").status_code == 200


def test_unchanged_settings_save_keeps_account_state(healthy, account):
    """Saving settings with the same Webshare account keeps the verified
    state: no reset to pending, no extra refresh."""
    ts, gen = app.state.account_ts, app.state.account_gen
    _save(healthy, webshare_username=OLD, notify_vip_days=5)
    assert app.state.account_gen == gen and app.state.account_ts == ts
    assert app.state.account_refresh_ok is True
    assert not app.state.account_refresh.is_set()
    assert healthy.get("/ready").status_code == 200
    assert account.calls == []
