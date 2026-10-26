# PyInstaller recipe for the bundled app: Packrat with Python and its dependencies, no install needed.
# On Windows it makes two programs sharing one folder: Packrat.exe (the desktop app, no console window) and
# packrat-cli.exe (commands such as `mcp` and `serve`, which talk over the console). On Linux one `packrat`.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

windows = sys.platform == "win32"
here = os.path.abspath(SPECPATH)
icon = os.path.join(here, "..", "build", "icons", "packrat.ico" if windows else "packrat.png")

hidden = collect_submodules("packrat") + collect_submodules("uvicorn") + ["platformdirs.unix", "platformdirs.windows"]
if windows:
    hidden += ["pystray._win32"]

a = Analysis(
    [os.path.join(here, "packrat_main.py")],
    datas=collect_data_files("packrat") + copy_metadata("packrat-assets"),  # the web UI; the version number
    hiddenimports=hidden,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
programs = [EXE(pyz, a.scripts, exclude_binaries=True, name="packrat-cli" if windows else "packrat",
                console=True, icon=icon)]
if windows:
    programs.append(EXE(pyz, a.scripts, exclude_binaries=True, name="Packrat", console=False, icon=icon))
COLLECT(*programs, a.binaries, a.datas, name="Packrat")
