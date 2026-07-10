import time

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import config
from app.main import app
from app.webshare import SearchResult


class FakeWebshareClient:
    """Stand-in for WebshareClient: serves canned search results and links."""

    fail_login = False  # class-level so tests can flip it for new instances

    def __init__(self, username: str = "", password: str = ""):
        self.username = username
        self.password = password
        self.results: list[SearchResult] = []
        self.file_link_url = "http://127.0.0.1:1/file.mkv"  # unreachable by default

    def set_credentials(self, username: str, password: str):
        self.username = username
        self.password = password

    async def check_login(self) -> str:
        from app.webshare import WebshareError
        if self.fail_login:
            raise WebshareError("Webshare /login/ failed: Invalid credentials")
        return self.username

    async def search(self, query: str, limit: int = 60, offset: int = 0):
        return [r for r in self.results if all(
            part.lower() in r.name.lower() for part in query.split()
        )]

    async def file_link(self, ident: str) -> str:
        return self.file_link_url

    async def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "api_key", "testkey")
    monkeypatch.setattr(config, "complete_dir", tmp_path / "complete")
    monkeypatch.setattr(config, "incomplete_dir", tmp_path / "incomplete")
    monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(config, "settings_file", tmp_path / "settings.json")
    monkeypatch.setattr(app_main, "WebshareClient", FakeWebshareClient)

    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def fake_webshare(client) -> FakeWebshareClient:
    return app.state.webshare


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False
