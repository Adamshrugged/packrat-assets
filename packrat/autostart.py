"""Start Packrat when the user logs in (Settings → Start at login): a Run registry value on Windows, an
autostart .desktop file on Linux. It starts in the background: tray icon, no browser window."""
import os
import re
import sys

NAME = "Packrat"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def command() -> list[str]:
    """How to start Packrat in the background: the desktop app, without opening the browser."""
    from .program import command as program
    return program(windowed=True) + ["--background"]


def supported() -> bool:
    return os.name == "nt" or sys.platform.startswith("linux")


def _desktop_exec(args: list[str]) -> str:
    """An Exec= line: the .desktop spec quotes with double quotes and backslash-escapes " ` $ and \\."""
    def quote(a: str) -> str:
        return a if re.fullmatch(r"[\w@%+=:,./-]+", a) else '"' + re.sub(r'(["`$\\])', r"\\\1", a) + '"'
    return " ".join(quote(a) for a in args)


def _desktop_file() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", "packrat.desktop")


def enabled() -> bool:
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, NAME)
            return True
        except OSError:
            return False
    return os.path.exists(_desktop_file())


def set_enabled(on: bool) -> None:
    if os.name == "nt":
        import subprocess
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if on:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(command()))
            else:
                try:
                    winreg.DeleteValue(key, NAME)
                except FileNotFoundError:
                    pass
        return
    path = _desktop_file()
    if not on:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Packrat\nComment=Your game asset library\n"
                f"Exec={_desktop_exec(command())}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")
