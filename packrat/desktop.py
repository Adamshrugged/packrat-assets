"""Opening web pages and the system file manager (Windows, Linux, macOS)."""
import os
import pathlib
import shutil
import subprocess
import sys
import webbrowser

from .program import external_env


class NoFileManager(Exception):
    pass


def open_url(url: str) -> None:
    """Open a page in the default browser."""
    opener = shutil.which("xdg-open") if sys.platform.startswith("linux") else None
    if opener and getattr(sys, "frozen", False):  # webbrowser would pass on the bundle's library path
        subprocess.Popen([opener, url], env=external_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        webbrowser.open(url)


def reveal(target: str, select: bool) -> None:
    """Show target in the file manager. With select, open its folder with it highlighted (where the file
    manager supports that), else open target itself."""
    if os.name == "nt":
        subprocess.Popen(f'explorer /select,"{target}"' if select else f'explorer "{target}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", target] if select else ["open", target])
    elif not (select and _show_items(target)):
        opener = shutil.which("xdg-open")
        if opener is None:
            raise NoFileManager("no file manager found (xdg-open is missing)")
        subprocess.Popen([opener, os.path.dirname(target) if select else target], env=external_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _show_items(target: str) -> bool:
    """Linux: ask the file manager (Files, Dolphin, Nemo, ...) to highlight target, over D-Bus."""
    gdbus = shutil.which("gdbus")
    if gdbus is None:
        return False
    try:
        return subprocess.run(
            [gdbus, "call", "--session", "--dest", "org.freedesktop.FileManager1",
             "--object-path", "/org/freedesktop/FileManager1",
             "--method", "org.freedesktop.FileManager1.ShowItems", f"['{pathlib.Path(target).as_uri()}']", ""],
            capture_output=True, timeout=5, env=external_env(),
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
