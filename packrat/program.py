"""How to start this Packrat from outside its process: for start at login and for the Claude (MCP) command.

Handles a Python install (python -m packrat), the Windows app (Packrat.exe opens the app, packrat-cli.exe runs
commands such as mcp, which need a console) and the Linux AppImage (the .AppImage file itself: the folder it
runs from is different on every launch)."""
import os
import shlex
import subprocess
import sys


def command(windowed: bool = False) -> list[str]:
    """The program to run, without arguments. windowed: no console window on Windows (the desktop app)."""
    if os.environ.get("APPIMAGE"):
        return [os.environ["APPIMAGE"]]
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            return [os.path.join(os.path.dirname(sys.executable), "Packrat.exe" if windowed else "packrat-cli.exe")]
        return [sys.executable]
    python = sys.executable
    if windowed and os.name == "nt":
        pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
        python = pythonw if os.path.exists(pythonw) else python
    return [python, "-m", "packrat"]


def shell_line(args: list[str]) -> str:
    """args as one line to paste into a terminal (PowerShell on Windows)."""
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
