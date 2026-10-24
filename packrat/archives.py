"""List and read archive contents without extracting them to disk."""
import os
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
    if sevenzip is None:
        raise RuntimeError("7-Zip not found; cannot list " + path)
    proc = subprocess.run(
        [sevenzip, "l", "-slt", "-ba", "-sccUTF-8", path],
        capture_output=True, creationflags=_NO_WINDOW, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "7z failed")
    out = []
    for block in proc.stdout.decode("utf-8", "replace").replace("\r", "").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.split("\n") if " = " in line)
        member = fields.get("Path")
        if not member or fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D"):
            continue
        member = member.replace("\\", "/")
        if is_junk_member(member):
            continue
        try:
            ts = datetime.strptime(fields.get("Modified", "")[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            ts = None
        out.append((member, int(fields.get("Size") or 0), ts))
    return out


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
    if sevenzip is None:
        raise RuntimeError("7-Zip not found")
    proc = subprocess.run(
        [sevenzip, "e", "-so", "-spd", "-sccUTF-8", path, member],
        capture_output=True, creationflags=_NO_WINDOW, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "7z failed")
    return proc.stdout


def read_file(path: str, member: str | None, sevenzip: str | None) -> bytes:
    if member is None:
        with open(path, "rb") as f:
            return f.read()
    return read_member(path, member, sevenzip)
