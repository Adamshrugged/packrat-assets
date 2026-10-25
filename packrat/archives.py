"""List and read archive contents without extracting them to disk."""
import functools
import os
import shutil
import subprocess
import threading
import zipfile
from collections import OrderedDict
from datetime import datetime

from .kinds import ext_of, is_junk_member

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def list_members(path: str, sevenzip: str | None) -> list[tuple[str, int, float | None]]:
    """Return (member, size, mtime) for every file in the archive."""
    ext = ext_of(path)
    if ext == ".zip":
        out = []
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or is_junk_member(info.filename):
                    continue
                try:
                    ts = datetime(*info.date_time).timestamp()
                except ValueError:
                    ts = None
                out.append((info.filename, info.file_size, ts))
        return out
    return _try_tools(path, sevenzip, "list")


def _run(cmd: list[str]) -> bytes:
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"{os.path.basename(cmd[0])} failed")
    return proc.stdout


def _parse_date(text: str) -> float | None:
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _list_7z(tool: str, path: str) -> list[tuple[str, int, float | None]]:
    out = []
    raw = _run([tool, "l", "-slt", "-ba", "-sccUTF-8", path])
    for block in raw.decode("utf-8", "replace").replace("\r", "").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.split("\n") if " = " in line)
        member = fields.get("Path")
        if not member or fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D"):
            continue
        out.append((member.replace("\\", "/"), int(fields.get("Size") or 0), _parse_date(fields.get("Modified", ""))))
    return out


def _list_unrar(tool: str, path: str) -> list[tuple[str, int, float | None]]:
    out = []
    raw = _run([tool, "lt", "-scu", path])
    for block in raw.decode("utf-8", "replace").replace("\r", "").split("\n\n"):
        fields = dict((k.strip(), v.strip()) for k, v in
                      (line.split(":", 1) for line in block.split("\n") if ":" in line))
        if "Name" in fields and fields.get("Type") == "File":
            out.append((fields["Name"].replace("\\", "/"), int(fields.get("Size") or 0),
                        _parse_date(fields.get("mtime", ""))))
    return out


def _list_bsdtar(tool: str, path: str) -> list[tuple[str, int, float | None]]:
    out = []
    raw = _run([tool, "-tvf", path])
    for line in raw.decode("utf-8", "replace").replace("\r", "").split("\n"):
        parts = line.split(None, 8)  # mode links owner group size month day time-or-year name
        if len(parts) == 9 and not parts[0].startswith("d") and parts[4].isdigit():
            out.append((parts[8], int(parts[4]), None))
    return out


def _bsdtar_pattern(member: str) -> str:
    """bsdtar treats member names as patterns; escape the pattern characters."""
    return "".join("\\" + c if c in "*?[\\" else c for c in member)


READERS = {
    "7z": (_list_7z, lambda t, p, m: _run([t, "e", "-so", "-spd", "-sccUTF-8", p, m])),
    "unrar": (_list_unrar, lambda t, p, m: _run([t, "p", "-inul", "-scu", p, m])),
    "bsdtar": (_list_bsdtar, lambda t, p, m: _run([t, "-xOf", p, _bsdtar_pattern(m)])),
}


@functools.cache
def _find(name: str) -> str | None:
    if name == "bsdtar":  # Windows 10+ ships bsdtar as tar.exe; on Linux, tar is GNU tar
        return shutil.which("bsdtar") or (shutil.which("tar") if os.name == "nt" else None)
    return shutil.which(name)


def tools(sevenzip: str | None) -> dict[str, str]:
    """Archive tools Packrat can use, in the order it tries them."""
    found = {"7z": sevenzip, "unrar": _find("unrar"), "bsdtar": _find("bsdtar")}
    return {name: path for name, path in found.items() if path}


_worked: dict[str, str] = {}  # archive path -> the tool that last read it


def _try_tools(path: str, sevenzip: str | None, action: str, member: str | None = None):
    """Some 7-Zip builds can't read RAR (Fedora's, for licensing reasons), so fall back to other tools."""
    available = tools(sevenzip)
    if ext_of(path) != ".rar":
        available.pop("unrar", None)
    if not available:
        raise RuntimeError("no archive tool found: install 7-Zip")
    order = sorted(available, key=lambda name: name != _worked.get(path))
    errors = []
    for name in order:
        lister, reader = READERS[name]
        try:
            if action == "list":
                result = [m for m in lister(available[name], path) if not is_junk_member(m[0])]
            else:
                result = reader(available[name], path, member)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"{name}: {str(e).splitlines()[-1] if str(e) else e}")
            continue
        _worked[path] = name
        return result
    hint = " (install unrar or bsdtar for .rar)" if ext_of(path) == ".rar" else ""
    raise RuntimeError(f"can't read {os.path.basename(path)}{hint}: " + "; ".join(errors))


class _ZipPool:
    """Keeps recently used zips open; re-reading a big zip's directory over SMB is slow."""

    def __init__(self, size: int = 8):
        self.size = size
        self.items: OrderedDict[str, tuple[tuple, zipfile.ZipFile, threading.Lock]] = OrderedDict()
        self.lock = threading.Lock()

    def read(self, path: str, member: str) -> bytes:
        st = os.stat(path)
        sig = (st.st_size, st.st_mtime)
        with self.lock:
            entry = self.items.get(path)
            if entry and entry[0] != sig:
                self._close(path)
                entry = None
            if entry is None:
                entry = (sig, zipfile.ZipFile(path), threading.Lock())
                self.items[path] = entry
                while len(self.items) > self.size:
                    self._close(next(iter(self.items)))
            self.items.move_to_end(path)
        _, zf, zlock = entry
        with zlock:
            return zf.read(member)

    def _close(self, path: str) -> None:
        _, zf, zlock = self.items.pop(path)
        with zlock:
            zf.close()


_zips = _ZipPool()


def read_member(path: str, member: str, sevenzip: str | None) -> bytes:
    if ext_of(path) == ".zip":
        return _zips.read(path, member)
    return _try_tools(path, sevenzip, "read", member)


def read_file(path: str, member: str | None, sevenzip: str | None) -> bytes:
    if member is None:
        with open(path, "rb") as f:
            return f.read()
    return read_member(path, member, sevenzip)
