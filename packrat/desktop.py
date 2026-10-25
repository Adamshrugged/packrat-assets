"""Opening the system file manager at a file or folder (Windows, Linux, macOS)."""
import os
import pathlib
import shutil
import subprocess
import sys


class NoFileManager(Exception):
    pass


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
        subprocess.Popen([opener, os.path.dirname(target) if select else target],
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
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
