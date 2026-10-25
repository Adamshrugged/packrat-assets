"""The request guard: Packrat has no login, so only this computer's pages may use it."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from packrat.guard import HostGuard


def client(bind="127.0.0.1", allowed=()):
    app = FastAPI()
    app.get("/x")(lambda: {"ok": True})
    app.post("/x")(lambda: {"ok": True})
    app.add_middleware(HostGuard, bind_host=bind, allowed_hosts=list(allowed))
    return TestClient(app, base_url="http://localhost:8765")


@pytest.mark.parametrize("host, code", [
    ("localhost:8765", 200), ("127.0.0.1:8765", 200), ("[::1]:8765", 200),
    ("evil.example:8765", 403), ("192.168.1.5:8765", 403),  # DNS rebinding / LAN, while local-only
])
def test_host_local_only(host, code):
    assert client().get("/x", headers={"Host": host}).status_code == code


@pytest.mark.parametrize("host, code", [("192.168.1.5:8765", 200), ("nas.local:8765", 200), ("evil.example:8765", 403)])
def test_host_network_mode(host, code):
    c = client(bind="0.0.0.0", allowed=["nas.local"])
    assert c.get("/x", headers={"Host": host}).status_code == code


@pytest.mark.parametrize("origin, code", [
    (None, 200), ("http://localhost:8765", 200), ("https://evil.example", 403), ("null", 403),
])
def test_origin_on_changes(origin, code):
    headers = {"Origin": origin} if origin else {}
    assert client().post("/x", headers=headers).status_code == code


def test_reads_from_other_origins_are_allowed():
    # the browser itself stops other sites from reading the response; only changes need blocking
    assert client().get("/x", headers={"Origin": "https://evil.example"}).status_code == 200
