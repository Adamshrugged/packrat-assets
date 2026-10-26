"""Signing in when Packrat listens on the network. The test client counts as another computer."""
import pytest
from fastapi.testclient import TestClient

from packrat import config
from packrat.app import create_app
from packrat.auth import MAX_FAILURES, hash_password, verify_password
from conftest import toml_str


@pytest.fixture
def remote(tmp_path, monkeypatch):
    (tmp_path / "lib").mkdir()
    (tmp_path / "config.toml").write_text(f'[paths]\ndata_dir = {toml_str(tmp_path / "data")}\n[server]\nhost = "0.0.0.0"\n'
                                          f'[itch]\nsync_every_hours = 0.0\n[[roots]]\npath = {toml_str(tmp_path / "lib")}\n')
    monkeypatch.setenv("PACKRAT_SECRETS", str(tmp_path / "secrets.toml"))
    app = create_app(config.load(str(tmp_path / "config.toml")))
    return TestClient(app, base_url="http://localhost"), app.state.ctx.auth


def test_password_hashing():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt:") and "correct horse" not in stored
    assert verify_password("correct horse", stored) and not verify_password("wrong", stored)


def test_everything_needs_signing_in(remote):
    client, _ = remote
    assert client.get("/api/facets").status_code == 401
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login.html"
    assert client.get("/login.html").status_code == 200  # the sign-in page itself is open
    assert client.get("/api/auth/status").json() == {"required": True, "password_set": False, "signed_in": False,
                                                     "min_length": 8}


def test_first_password_needs_the_setup_code(remote):
    client, auth = remote
    assert auth.setup_code and len(auth.setup_code) == 8
    assert client.post("/api/auth/setup", json={"code": "00000000", "password": "long enough"}).status_code == 403
    code = f"{auth.setup_code[:4]}-{auth.setup_code[4:]}"
    assert client.post("/api/auth/setup", json={"code": code, "password": "short"}).status_code == 400
    assert client.post("/api/auth/setup", json={"code": code, "password": "long enough"}).status_code == 200
    assert auth.setup_code is None  # used up
    assert client.get("/api/facets").status_code == 200  # signed in by the cookie
    assert client.post("/api/auth/setup", json={"code": code, "password": "another one"}).status_code == 409


def test_sign_in_out_and_guessing_limit(remote):
    client, auth = remote
    auth.set_password("long enough")
    assert client.post("/api/auth/login", json={"password": "long enough"}).status_code == 200
    assert client.get("/api/facets").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/facets").status_code == 401
    for _ in range(MAX_FAILURES):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "long enough"}).status_code == 429  # even the right one


def test_changing_the_password_signs_others_out(remote, tmp_path):
    client, auth = remote
    auth.set_password("first password")
    other = TestClient(client.app, base_url="http://localhost")
    other.post("/api/auth/login", json={"password": "first password"})
    client.post("/api/auth/login", json={"password": "first password"})
    assert client.post("/api/auth/password", json={"current": "wrong", "password": "second password"}).status_code == 401
    assert client.post("/api/auth/password", json={"current": "first password", "password": "second password"}).status_code == 200
    assert client.get("/api/facets").status_code == 200  # this browser got a new session
    assert other.get("/api/facets").status_code == 401  # the other one was signed out


def test_api_token_for_mcp(remote):
    client, auth = remote
    auth.set_password("long enough")
    token = auth.api_token()
    assert client.get("/api/facets", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/facets", headers={"Authorization": "Bearer wrong"}).status_code == 401
    client.post("/api/auth/login", json={"password": "long enough"})
    s = client.get("/api/settings").json()
    assert s["mcp_remote_command"].endswith(f"-- packrat mcp --url http://localhost --token {token}")
    assert "packrat-cli.exe\" mcp --url" in s["mcp_remote_command_windows"]  # the installer's program, by path
    client.post("/api/auth/token")
    mcp = TestClient(client.app, base_url="http://localhost")  # no session cookie, like the MCP server
    assert mcp.get("/api/facets", headers={"Authorization": f"Bearer {token}"}).status_code == 401  # replaced
    assert mcp.get("/api/facets", headers={"Authorization": f"Bearer {auth.api_token()}"}).status_code == 200
