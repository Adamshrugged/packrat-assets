"""The web API end to end, against a small library on disk (no network)."""
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from packrat import config, db, itch
from packrat.app import create_app


def wait_idle(client, job, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        state = client.get("/api/activity").json()[job]
        if not state["running"]:
            return state
        time.sleep(0.05)
    raise AssertionError(f"{job} still running")


@pytest.fixture
def app(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    (lib / "Dice Pack").mkdir(parents=True)
    (lib / "Dice Pack" / "dice.png").write_bytes(b"\x89PNG not really")
    with zipfile.ZipFile(lib / "Forest Tiles.zip", "w") as z:
        z.writestr("Forest/tree.png", b"tree")
        z.writestr("Forest/License.txt", b"CC0")
    (tmp_path / "dl").mkdir()  # a chosen download folder must exist (it may be on a drive that's not connected)
    (tmp_path / "config.toml").write_text(
        f'[paths]\ndata_dir = "{tmp_path / "data"}"\n[itch]\ndownload_dir = "{tmp_path / "dl"}"\n'
        f'sync_every_hours = 0.0\n[[roots]]\npath = "{lib}"\n')
    monkeypatch.setenv("PACKRAT_SECRETS", str(tmp_path / "secrets.toml"))
    cfg = config.load(str(tmp_path / "config.toml"))
    client = TestClient(create_app(cfg), base_url="http://localhost")
    wait_idle(client, "scan")  # a new install scans on its own
    return client, cfg


def test_library_browse_and_search(app):
    client, _ = app
    facets = client.get("/api/facets").json()
    assert facets["totals"]["packs"] == 2 and facets["roots"] == [{"name": "lib", "count": 2}]
    packs = {p["name"]: p for p in client.get("/api/packs").json()}
    assert set(packs) == {"Dice Pack", "Forest Tiles"}

    forest = packs["Forest Tiles"]
    top = client.get(f"/api/packs/{forest['id']}/files").json()
    assert top["dirs"] == [{"name": "Forest", "path": "Forest", "count": 2, "archive": False}]
    inner = client.get(f"/api/packs/{forest['id']}/files", params={"dir": "Forest"}).json()
    tree = next(f for f in inner["files"] if f["name"] == "tree.png")
    assert tree["in_archive"] and client.get(tree["raw"]).content == b"tree"

    hits = client.get("/api/search", params={"q": "tree"}).json()
    assert [f["name"] for f in hits["files"]] == ["tree.png"]
    assert client.get("/api/search", params={"q": "tree nothing"}).json()["matched"] == "any word"


def test_notes_are_saved_and_searchable(app):
    client, _ = app
    dice = next(p for p in client.get("/api/packs").json() if p["name"] == "Dice Pack")
    out = client.patch(f"/api/packs/{dice['id']}", json={"tags": "Board, dice ", "rating": 9, "favorite": True}).json()
    assert out["tags"] == ["board", "dice"] and out["rating"] == 5 and out["favorite"]
    assert [p["name"] for p in client.get("/api/search", params={"q": "board"}).json()["packs"]] == ["Dice Pack"]


def test_settings_round_trip(app, tmp_path):
    client, cfg = app
    s = client.get("/api/settings").json()
    assert not s["first_run"] and [r["path"] for r in s["roots"]] == [str(tmp_path / "lib")]
    assert not s["can_reveal"]  # the test client isn't a browser on this computer (127.0.0.1)
    other = tmp_path / "more"
    other.mkdir()
    out = client.put("/api/settings", json={"roots": [{"path": str(tmp_path / "lib")}, {"path": str(other)}],
                                            "itch": {"sync_every_hours": 6, "auto_download": False}}).json()
    assert out["rescan"] and len(out["roots"]) == 2 and cfg.itch_sync_hours == 6
    assert client.put("/api/settings", json={"roots": [{"path": "/nope"}], "itch": {}}).status_code == 400
    wait_idle(client, "scan")


def test_purchases_download_and_cancel(app, monkeypatch):
    client, cfg = app
    c = db.connect(cfg)
    with c:
        c.execute("INSERT INTO store_items(store, store_id, key_id, title) VALUES ('itch', '1', 'k', 'Owned Pack')")
        for n in range(3):
            c.execute("INSERT INTO store_files(item_id, upload_id, filename, size) VALUES (1, ?, ?, 10)",
                      (str(n), f"part{n}.zip"))
        c.execute("INSERT INTO store_files(item_id, upload_id, filename, size) VALUES (1, 'x', 'readme.txt', 1)")
    file_ids = [r[0] for r in c.execute("SELECT id FROM store_files WHERE filename LIKE 'part%' ORDER BY id")]
    c.close()

    summary = client.get("/api/store/summary").json()
    assert summary["counts"] == {"missing": 1} and summary["pending_files"] == 3  # the readme is an extra
    item = client.get("/api/store/items").json()[0]
    assert item["status"] == "missing" and [f["extra"] for f in item["files"]] == [False, False, False, True]

    def fake_download(cfg, fid, dest, step):
        for got in (0, 5, 10):
            step(got, 10)
            time.sleep(0.15)
        path = os.path.join(dest, "Owned Pack", f"part{fid}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"x")
        return path
    monkeypatch.setattr(itch, "download", fake_download)

    assert not client.post("/api/store/download", json={"file_ids": file_ids[:2]}).json()["queued_behind"]
    assert client.post("/api/store/download", json={"file_ids": file_ids[2:]}).json()["queued_behind"]
    time.sleep(0.6)
    assert client.get("/api/activity").json()["items"] == {"1": "running"}
    client.post("/api/downloads/cancel")
    done = wait_idle(client, "download")
    assert done["result"]["itch"]["cancelled"]
    assert client.get("/api/activity").json()["items"] == {}
    assert os.listdir(os.path.join(cfg.itch_download_dir, "Owned Pack"))  # the finished file was kept


def test_failed_sync_waits_a_full_interval(app):
    client, cfg = app
    c = db.connect(cfg)
    assert db.get_kv(c, "itch_last_attempt") is None
    client.post("/api/store/sync")  # no key: fails at once
    assert "Settings" in wait_idle(client, "itch")["error"]
    assert float(db.get_kv(c, "itch_last_attempt")) > time.time() - 60  # the scheduler counts from this
    c.close()


def test_same_named_folders_get_distinct_labels(tmp_path):
    for d in ("a/assets", "b/assets"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "c.toml").write_text(f'[[roots]]\npath = "{tmp_path / "a/assets"}"\n[[roots]]\npath = "{tmp_path / "b/assets"}"\n')
    labels = [r.label for r in config.load(str(tmp_path / "c.toml")).roots if not r.auto]
    assert labels == ["assets", "assets (2)"]


def test_desktop_app_endpoints(app):
    client, _ = app
    assert client.get("/api/ping").json() == {"app": "packrat"}  # how a second launch finds this one
    s = client.get("/api/settings").json()
    assert not s["can_quit"] and s["start_at_login"] is None  # a server (and a remote browser) gets neither
    assert client.post("/api/quit").status_code == 403


def test_download_folder_inside_the_library(tmp_path):
    """Each purchase in the download folder is its own pack, even when that folder is inside a library folder."""
    from packrat.scanner import scan
    lib = tmp_path / "lib"
    for d in ("itch.io/Coins/coin.png", "itch.io/Swords/sword.png", "Other Pack/x.png"):
        (lib / d).parent.mkdir(parents=True, exist_ok=True)
        (lib / d).write_bytes(b"x")
    (tmp_path / "c.toml").write_text(f'[paths]\ndata_dir = "{tmp_path / "data"}"\n[itch]\ndownload_dir = '
                                     f'"{lib / "itch.io"}"\n[[roots]]\npath = "{lib}"\n')
    cfg = config.load(str(tmp_path / "c.toml"))
    db.init(cfg)
    scan(cfg)
    c = db.connect(cfg)
    assert sorted(r[0] for r in c.execute("SELECT name FROM packs")) == ["Coins", "Other Pack", "Swords"]


def test_removed_library_folder_packs_are_flagged_missing(app, tmp_path):
    client, cfg = app
    other = tmp_path / "other"
    (other / "Extra Pack").mkdir(parents=True)
    (other / "Extra Pack" / "e.png").write_bytes(b"x")
    roots = [{"path": str(tmp_path / "lib")}, {"path": str(other)}]
    client.put("/api/settings", json={"roots": roots, "itch": {}})
    wait_idle(client, "scan")
    assert "Extra Pack" in [p["name"] for p in client.get("/api/packs").json()]
    client.put("/api/settings", json={"roots": roots[:1], "itch": {}})
    wait_idle(client, "scan")
    assert "Extra Pack" not in [p["name"] for p in client.get("/api/packs").json()]
    assert "Extra Pack" in [p["name"] for p in client.get("/api/packs", params={"missing": True}).json()]  # kept
