"""Settings files, default folders and file names."""
import os
import sys
import tomllib

import pytest

from packrat import config, settings
from packrat.itch import safe_name
from conftest import toml_str


def test_toml_round_trip():
    data = {"itch": {"download_dir": 'C:\\Users\\me\\"odd" folder', "sync_every_hours": 24.0, "auto_download": False},
            "roots": [{"path": "/a/b", "containers": ["x", "y z"]}, {"path": "/c", "label": "Ünïcode"}]}
    assert tomllib.loads(settings.to_toml(data)) == data


def test_save_and_reload(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = config.load(str(tmp_path / "config.toml"))  # not created yet
    settings.save(cfg, [{"path": str(lib)}], {"download_dir": None, "sync_every_hours": 6, "auto_download": True})
    if os.name != "nt":  # Windows has no Unix permissions; the user profile folder keeps it private there
        assert oct(os.stat(cfg.source).st_mode & 0o777) == "0o600"
    again = config.load(cfg.source)
    assert [r.path for r in again.roots if not r.auto] == [str(lib)]
    assert again.itch_sync_hours == 6 and again.itch_auto_download
    assert again.itch_download_dir == config.default_download_dir()  # the default isn't written out


def test_download_folder_is_always_indexed(tmp_path):
    (tmp_path / "config.toml").write_text(f'[itch]\ndownload_dir = {toml_str(tmp_path / "dl")}\n')
    cfg = config.load(str(tmp_path / "config.toml"))
    assert [(r.path, r.auto) for r in cfg.roots] == [(str(tmp_path / "dl"), True)]


def test_download_folder_inside_a_root_is_not_added_twice(tmp_path):
    (tmp_path / "config.toml").write_text(
        f'[itch]\ndownload_dir = {toml_str(tmp_path / "lib" / "itch")}\n[[roots]]\npath = {toml_str(tmp_path / "lib")}\n')
    assert len(config.load(str(tmp_path / "config.toml")).roots) == 1


@pytest.mark.parametrize("name, safe", [
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("console", "console"), ("a:b/c?.", "a_b_c_"),
    (" Pack v2. ", "Pack v2"), ("", "untitled"), ("x" * 130, "x" * 120),
])
def test_safe_name(name, safe):
    assert safe_name(name) == safe


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux autostart file")
def test_start_at_login_file(tmp_path, monkeypatch):
    from packrat import autostart
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "command", lambda: ["/opt/my apps/python", "-m", "packrat", "--background"])
    assert not autostart.enabled()
    autostart.set_enabled(True)
    text = (tmp_path / "autostart" / "packrat.desktop").read_text()
    assert 'Exec="/opt/my apps/python" -m packrat --background' in text
    assert autostart.enabled()
    autostart.set_enabled(False)
    assert not autostart.enabled()
