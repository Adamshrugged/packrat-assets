import os
import shutil
import tomllib
from dataclasses import dataclass


@dataclass
class Root:
    label: str
    path: str
    display: str
    containers: list[str]


@dataclass
class Config:
    roots: list[Root]
    data_dir: str
    host: str
    port: int
    sevenzip: str | None
    itch_download_dir: str | None = None
    itch_sync_hours: float = 0
    itch_auto_download: bool = False

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "packrat.db")

    @property
    def thumb_dir(self) -> str:
        return os.path.join(self.data_dir, "thumbs")

    def display_path(self, path: str, member: str | None = None) -> str:
        """Translate a server-side path into the path the user sees (e.g. a mapped network drive)."""
        shown = path
        for src, display in [(r.path, r.display) for r in self.roots]:
            if os.path.normcase(path).startswith(os.path.normcase(src)):
                shown = display + path[len(src):]
                break
        sep = "\\" if "\\" in shown or ":" in shown[:3] else "/"
        shown = shown.replace("/", sep).replace("\\", sep)
        if member:
            shown += sep + member.replace("/", sep)
        return shown


def find_7z() -> str | None:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(candidate):
            return candidate
    return None


def default_path() -> str:
    return os.environ.get(
        "PACKRAT_CONFIG",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.toml"),
    )


def load(path: str | None = None) -> Config:
    path = path or default_path()
    base = os.path.dirname(os.path.abspath(path))
    raw = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    cat = raw.get("catalog", {})
    srv = raw.get("server", {})

    data_dir = cat.get("data_dir", "data")
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(base, data_dir)
    os.makedirs(data_dir, exist_ok=True)

    roots = []
    for r in raw.get("roots", []):
        p = os.path.normpath(r["path"])
        roots.append(Root(
            label=r.get("label") or os.path.basename(p),
            path=p,
            display=os.path.normpath(r["display"]) if r.get("display") else p,
            containers=list(r.get("containers", [])),
        ))

    itch = raw.get("itch", {})
    return Config(
        roots=roots,
        data_dir=data_dir,
        host=srv.get("host", "127.0.0.1"),
        port=int(srv.get("port", 8765)),
        sevenzip=cat.get("sevenzip") or find_7z(),
        itch_download_dir=os.path.normpath(itch["download_dir"]) if itch.get("download_dir") else None,
        itch_sync_hours=float(itch.get("sync_every_hours", 0)),
        itch_auto_download=bool(itch.get("auto_download", False)),
    )
