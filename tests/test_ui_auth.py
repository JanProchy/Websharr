"""Tests for the first-run setup, login sessions and the settings API."""

from .conftest import FakeWebshareClient


def _setup(client, **over):
    body = {"username": "morte", "password": "hunter2",
            "webshare_username": "ws@example.com", "webshare_password": "wspass"}
    body.update(over)
    return client.post("/ui/api/setup", json=body)


def test_first_run_serves_setup_page(client):
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert 'id="setup-form"' in resp.text
    # The main app (and its API key) must not leak before setup.
    assert "testkey" not in resp.text


def test_setup_creates_account_and_signs_in(client):
    resp = _setup(client)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Session cookie set -> the app page is served.
    page = client.get("/ui")
    assert 'id="view-queue"' in page.text

    # Setup is one-shot.
    assert _setup(client).status_code == 403


def test_setup_validates_input(client):
    assert _setup(client, username="").status_code == 400
    assert _setup(client, password="abc").status_code == 400


def test_login_logout_flow(client):
    _setup(client)
    client.cookies.clear()
    assert 'id="login-form"' in client.get("/ui").text

    bad = client.post("/ui/api/login", json={"username": "morte", "password": "wrong"})
    assert bad.status_code == 403

    ok = client.post("/ui/api/login", json={"username": "morte", "password": "hunter2"})
    assert ok.json()["ok"] is True
    assert 'id="view-queue"' in client.get("/ui").text

    client.post("/ui/api/logout")
    assert 'id="login-form"' in client.get("/ui").text


def test_session_survives_restart(client, tmp_path):
    """Settings (incl. the signing secret) persist, so cookies stay valid."""
    _setup(client)
    from app.settings import settings
    token = client.cookies.get("websharr_session")
    settings.load()  # simulates a fresh process reading the settings file
    assert settings.verify_session(token)


def test_session_auth_works_for_data_endpoints(client):
    _setup(client)
    # No apikey — the session cookie alone must be enough.
    assert client.get("/ui/api/queue").json() == {"jobs": []}
    client.cookies.clear()
    assert client.get("/ui/api/queue").status_code == 403


def test_settings_roundtrip_and_live_client_update(client, fake_webshare):
    _setup(client)

    s = client.get("/ui/api/settings").json()
    assert s["ui_username"] == "morte"
    assert s["webshare_username"] == "ws@example.com"
    assert s["webshare_password_set"] is True
    assert s["api_key"]  # generated (or kept from env) and visible for copying

    resp = client.post("/ui/api/settings", json={
        "webshare_username": "jina@example.com", "webshare_password": "novepass"})
    assert resp.json()["ok"] is True
    # The live client used by the download manager got the new credentials.
    assert fake_webshare.username == "jina@example.com"
    assert fake_webshare.password == "novepass"

    # Empty password field means "keep the current password".
    client.post("/ui/api/settings", json={"webshare_username": "jina@example.com"})
    assert fake_webshare.password == "novepass"


def test_change_ui_password(client):
    _setup(client)

    bad = client.post("/ui/api/settings", json={
        "current_password": "spatne", "new_password": "novejpass"})
    assert bad.status_code == 403

    ok = client.post("/ui/api/settings", json={
        "current_password": "hunter2", "new_password": "novejpass"})
    assert ok.json()["ok"] is True

    client.cookies.clear()
    assert client.post("/ui/api/login",
                       json={"username": "morte", "password": "hunter2"}).status_code == 403
    assert client.post("/ui/api/login",
                       json={"username": "morte", "password": "novejpass"}).json()["ok"] is True


def test_webshare_connection_test(client, monkeypatch):
    # Open during first-run so the setup page can use it.
    ok = client.post("/ui/api/test-webshare",
                     json={"username": "u@example.com", "password": "p"})
    assert ok.json() == {"ok": True, "username": "u@example.com"}

    monkeypatch.setattr(FakeWebshareClient, "fail_login", True)
    bad = client.post("/ui/api/test-webshare",
                      json={"username": "u@example.com", "password": "wrong"})
    body = bad.json()
    assert body["ok"] is False
    assert "Invalid credentials" in body["error"]

    # Once configured, the endpoint requires auth.
    monkeypatch.setattr(FakeWebshareClient, "fail_login", False)
    _setup(client)
    client.cookies.clear()
    assert client.post("/ui/api/test-webshare", json={}).status_code == 403
