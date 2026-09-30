import time

import pytest

import app.main as app_main
from app.config import config
from app.main import ACCOUNT_REFRESH, app


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


def test_health_ok(healthy):
    resp = healthy.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["storage"]["status"] == "ok"
    assert body["checks"]["webshare"]["status"] == "ok"
    # Unauthenticated endpoint: no account name, no paths.
    assert "ws@example.com" not in resp.text
    assert str(config.complete_dir) not in resp.text


def test_health_pending_at_startup(no_account, client, monkeypatch):
    monkeypatch.setattr(config, "webshare_username", "ws@example.com")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["checks"]["webshare"]["status"] == "pending"


def test_health_first_refresh_failed(no_account, client, monkeypatch):
    monkeypatch.setattr(config, "webshare_username", "ws@example.com")
    app.state.account_refresh_ok = False
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["checks"]["webshare"]["status"] == "error"


def test_health_not_configured(no_account, client):
    resp = client.get("/health")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["checks"]["webshare"]["reason"]


def test_health_stale_account(healthy):
    app.state.account_ts = time.time() - 4 * ACCOUNT_REFRESH
    app.state.account_refresh_ok = False
    resp = healthy.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"
    assert "not refreshed" in resp.json()["checks"]["webshare"]["reason"]


def test_health_tolerates_one_failed_refresh(healthy):
    app.state.account_ts = time.time() - ACCOUNT_REFRESH
    app.state.account_refresh_ok = False
    assert healthy.get("/health").status_code == 200


def test_health_vip_expired(healthy):
    app.state.account = {"vip": False, "vip_days": 0}
    resp = healthy.get("/health")
    assert resp.status_code == 503
    assert "VIP" in resp.json()["checks"]["webshare"]["reason"]


def test_health_dir_not_writable(healthy, monkeypatch):
    # os.access instead of chmod: tests may run as root, which ignores modes.
    monkeypatch.setattr(app_main.os, "access", lambda path, mode: False)
    resp = healthy.get("/health")
    assert resp.status_code == 503
    assert "not writable" in resp.json()["checks"]["storage"]["reason"]


def test_health_dir_missing(healthy, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "complete_dir", tmp_path / "gone")
    resp = healthy.get("/health")
    assert resp.status_code == 503
    assert resp.json()["checks"]["storage"]["reason"] == "complete dir missing"
    assert str(tmp_path) not in resp.text
