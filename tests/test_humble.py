"""Humble Bundle (experimental), against fake Humble orders shaped like the real ones (no network)."""
import os
import time

import pytest
from fastapi.testclient import TestClient

from conftest import toml_str
from packrat import config, db, humble, settings, stores
from packrat.app import create_app

ORDER = {
    "gamekey": "KEY1", "created": "2026-01-02T10:00:00", "product": {"human_name": "Game Dev Asset Bundle"},
    "subproducts": [
        {"machine_name": "forest_tiles", "human_name": "Forest Tiles", "payee": {"human_name": "Pixel Co"},
         "downloads": [{"platform": "other", "download_struct": [
             {"name": "Download", "md5": "aaa", "file_size": 5000,
              "url": {"web": "https://dl.humble.com/forest_tiles.zip?key=signed"}}]}]},
        {"machine_name": "tool_app", "human_name": "Sprite Tool", "payee": {"human_name": "Tools Inc"},
         "downloads": [
             {"platform": "windows", "download_struct": [{"md5": "w1", "file_size": 10,
                                                          "url": {"web": "https://dl.humble.com/tool-win.zip"}}]},
             {"platform": "linux", "download_struct": [{"md5": "l1", "file_size": 10,
                                                        "url": {"web": "https://dl.humble.com/tool-linux.zip"}}]}]},
        {"machine_name": "steam_only", "human_name": "Key Only Game", "downloads": []},
    ],
}
BOOKS = {
    "gamekey": "KEY2", "created": "2026-02-02", "product": {"human_name": "Fantasy Novels Bundle"},
    "subproducts": [{"machine_name": "novel", "human_name": "A Novel", "downloads": [
        {"platform": "ebook", "download_struct": [{"md5": "b1", "file_size": 7,
                                                   "url": {"web": "https://dl.humble.com/novel.epub"}}]}]}],
}


class FakeClient:
    """Stands in for humble.Client: a fixed set of orders, or an expired cookie."""
    orders = {"KEY1": ORDER, "KEY2": BOOKS}
    expired = False

    def __init__(self, cookie, values):
        pass

    def order_keys(self):
        if FakeClient.expired:
            raise humble.HumbleError("Humble rejected the cookie (expired, or you signed out).")
        return list(self.orders)

    def order(self, key):
        return self.orders[key]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    (tmp_path / "lib").mkdir()
    (tmp_path / "hb").mkdir()
    (tmp_path / "c.toml").write_text(
        f"[paths]\ndata_dir = {toml_str(tmp_path / 'data')}\n[itch]\nsync_every_hours = 0.0\n"
        f"[humble]\ndownload_dir = {toml_str(tmp_path / 'hb')}\n[[roots]]\npath = {toml_str(tmp_path / 'lib')}\n")
    monkeypatch.setenv("PACKRAT_SECRETS", str(tmp_path / "secrets.toml"))
    monkeypatch.setattr(humble, "Client", FakeClient)
    FakeClient.expired = False
    cfg = config.load(str(tmp_path / "c.toml"))
    db.init(cfg)
    settings.save_secrets({"humble_session_cookie": "cookie-value"})
    return cfg


def listed(cfg):
    with db.connect(cfg) as c:
        return sorted(r[0] for r in c.execute("SELECT title FROM store_items WHERE store='humble' AND asset=1"))


def test_order_parsing():
    items = humble.items_from_order(ORDER)
    assert [i["title"] for i in items] == ["Forest Tiles", "Sprite Tool"]  # the key-only game has no files
    forest = items[0]
    assert forest["bundle"] == "Game Dev Asset Bundle" and forest["author"] == "Pixel Co"
    assert forest["files"] == [{"upload_id": "aaa", "filename": "forest_tiles.zip", "display_name": "Download",
                                "size": 5000, "md5": "aaa", "type": "other"}]


def test_only_game_assets_are_listed(cfg):
    out = humble.sync(cfg)
    assert out["orders"] == 2 and out["errors"] == 0
    assert listed(cfg) == ["Forest Tiles", "Sprite Tool"]  # from an asset bundle; the novel is skipped


def test_a_product_already_in_the_library_counts_as_an_asset(cfg, tmp_path):
    from packrat.scanner import scan
    humble.sync(cfg)
    os.makedirs(tmp_path / "lib" / "My Novel")
    (tmp_path / "lib" / "My Novel" / "novel.epub").write_bytes(b"x" * 7)  # same name and size as the purchase
    scan(cfg)  # re-matches purchases
    assert "A Novel" in listed(cfg)


def test_only_this_computers_build_is_downloaded(cfg):
    humble.sync(cfg)
    with db.connect(cfg) as c:
        names = sorted(r["filename"] for r in stores.pending_files(c))
    builds = {"windows": ["tool-win.zip"], "linux": ["tool-linux.zip"],
              "mac": ["tool-linux.zip", "tool-win.zip"]}[stores.THIS_PLATFORM]  # no Mac build offered: keep all
    assert names == sorted(["forest_tiles.zip", *builds])


def test_download_into_the_humble_folder(cfg, monkeypatch, tmp_path):
    humble.sync(cfg)

    def fake_fetch(url, target, progress=None, md5=None, size_hint=None):
        assert "signed" in url  # a fresh link from the order, not a stored one
        open(target, "wb").write(b"z" * 5000)
        return 5000
    monkeypatch.setattr(stores, "fetch_to", fake_fetch)
    with db.connect(cfg) as c:
        fid = c.execute("SELECT id FROM store_files WHERE filename='forest_tiles.zip'").fetchone()[0]
    out = stores.download_many(cfg, [fid])
    assert out["downloaded"] == 1 and out["failed"] == 0
    assert (tmp_path / "hb" / "Forest Tiles" / "forest_tiles.zip").exists()
    with db.connect(cfg) as c:  # indexed and matched straight away
        assert c.execute("SELECT match_pack_id FROM store_files WHERE id=?", (fid,)).fetchone()[0]


def test_expired_cookie_doesnt_stop_itch(cfg, monkeypatch):
    FakeClient.expired = True
    settings.save_secrets({"humble_session_cookie": "old", "itch_api_key": "key"})
    from packrat import itch
    monkeypatch.setattr(itch, "sync", lambda cfg, progress=None: {"purchases": 3, "errors": 0, "error_samples": []})
    out = stores.sync_all(cfg)
    assert out["stores"]["itch"]["purchases"] == 3 and out["errors"] == 1
    assert "expired" in out["error_samples"][0]
    with db.connect(cfg) as c:  # shown on the Purchases page
        assert "expired" in db.get_kv(c, "humble_last_errors")


def test_connect_from_settings(cfg):
    settings.save_secrets({})
    client = TestClient(create_app(cfg), base_url="http://localhost")
    assert client.post("/api/settings/humble-cookie", json={"cookie": "_simpleauth_sess=abc"}).status_code == 200
    s = client.get("/api/settings").json()
    assert s["humble"]["connected"] and "abc" not in str(s)  # the cookie is never sent back
    for _ in range(100):  # connecting starts a sync
        if not client.get("/api/activity").json()["sync"]["running"]:
            break
        time.sleep(0.05)
    titles = [i["title"] for i in client.get("/api/store/items").json()]
    assert sorted(titles) == ["Forest Tiles", "Sprite Tool"]
    FakeClient.expired = True
    assert client.post("/api/settings/humble-cookie", json={"cookie": "bad"}).status_code == 400


def test_count_a_skipped_product_as_an_asset(cfg):
    humble.sync(cfg)
    client = TestClient(create_app(cfg), base_url="http://localhost")
    assert client.get("/api/store/summary").json()["skipped"] == 1
    skipped = client.get("/api/store/items?skipped=1").json()
    assert [i["title"] for i in skipped] == ["A Novel"] and not skipped[0]["asset"]
    assert client.post(f"/api/store/items/{skipped[0]['id']}/asset").status_code == 200
    humble.sync(cfg, force=True)  # a later sync keeps the choice
    assert "A Novel" in listed(cfg)
    assert client.get("/api/store/summary").json()["skipped"] == 0
