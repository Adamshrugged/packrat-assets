"""Settings files, default folders and file names."""
import os
import sys
import tomllib

import pytest

from packrat import config, settings
from packrat.stores import safe_name
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


def test_download_folders_are_always_indexed(tmp_path):
    (tmp_path / "config.toml").write_text(f'[itch]\ndownload_dir = {toml_str(tmp_path / "dl")}\n'
                                          f'[humble]\ndownload_dir = {toml_str(tmp_path / "hb")}\n')
    cfg = config.load(str(tmp_path / "config.toml"))
    assert [(r.path, r.auto) for r in cfg.roots] == [(str(tmp_path / "dl"), True), (str(tmp_path / "hb"), True)]


def test_download_folder_inside_a_root_is_not_added_twice(tmp_path):
    (tmp_path / "config.toml").write_text(
        f'[itch]\ndownload_dir = {toml_str(tmp_path / "lib" / "itch")}\n[humble]\ndownload_dir = '
        f'{toml_str(tmp_path / "lib" / "humble")}\n[[roots]]\npath = {toml_str(tmp_path / "lib")}\n')
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


@pytest.mark.skipif(os.name != "nt", reason="Windows Run registry value")
def test_start_at_login_registry(monkeypatch):
    import winreg
    from packrat import autostart
    monkeypatch.setattr(autostart, "RUN_KEY", r"Software\PackratTest\Run")  # not the real Run key
    try:
        assert not autostart.enabled()
        autostart.set_enabled(True)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY) as key:
            value = winreg.QueryValueEx(key, "Packrat")[0]
        assert value.endswith("-m packrat --background") and autostart.enabled()
        autostart.set_enabled(False)
        assert not autostart.enabled()
    finally:
        for sub in (r"Software\PackratTest\Run", r"Software\PackratTest"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass


def test_program_command_for_each_install(monkeypatch):
    from packrat import program
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert program.command()[-2:] == ["-m", "packrat"]  # a Python install
    monkeypatch.setenv("APPIMAGE", "/home/me/Apps/Packrat.AppImage")
    assert program.command() == ["/home/me/Apps/Packrat.AppImage"]  # not the folder it runs from
    monkeypatch.delenv("APPIMAGE")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", os.path.join("C:", "Apps", "Packrat", "Packrat.exe"))
    if os.name == "nt":
        assert program.command().pop().endswith("packrat-cli.exe")  # commands need a console
        assert program.command(windowed=True).pop().endswith("Packrat.exe")
    else:
        assert program.command() == [sys.executable]


def test_bundled_linux_app_starts_programs_with_system_libraries(monkeypatch):
    from packrat import program
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_x/usr/packrat/_internal")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in program.external_env()  # nothing was set before the app started
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/mylibs")
    assert program.external_env()["LD_LIBRARY_PATH"] == "/opt/mylibs"  # the user's own value comes back
