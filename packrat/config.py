import os
import shutil
import tomllib
from dataclasses import dataclass, field

import platformdirs

APP = "packrat"


@dataclass
class Root:
    label: str
    path: str
    display: str
    containers: list[str]
    auto: bool = False
    ignore: list[str] = field(default_factory=list)  # folders inside it to leave out ('/'-separated, relative)  # added by Packrat (the itch.io download folder), not chosen in Settings


@dataclass
class Config:
    roots: list[Root]
    data_dir: str
    host: str
    port: int
    sevenzip: str | None
    itch_download_dir: str | None = None
    itch_sync_hours: float = 24
    itch_auto_download: bool = False
    allowed_hosts: list[str] = field(default_factory=list)
    source: str = ""  # the config.toml this was loaded from (it may not exist yet)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "packrat.db")

    @property
    def thumb_dir(self) -> str:
        return os.path.join(self.data_dir, "thumbs")

    def ensure_download_dir(self) -> None:
        """Make the default download folder on first use. A chosen folder must already exist: it may be on
        a drive that isn't connected, and creating it locally would hide that."""
        if self.itch_download_dir == default_download_dir():
            os.makedirs(self.itch_download_dir, exist_ok=True)
        elif not os.path.isdir(self.itch_download_dir):
            raise FileNotFoundError(f"download folder not reachable: {self.itch_download_dir}")

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


def inside(path: str, folder: str) -> bool:
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def find_7z() -> str | None:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":  # the 7-Zip installer doesn't add itself to PATH
        for base in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
            folder = os.environ.get(base)
            candidates = [os.path.join(folder, "7-Zip", "7z.exe")] if folder else []
            if base == "LOCALAPPDATA" and folder:
                candidates.append(os.path.join(folder, "Programs", "7-Zip", "7z.exe"))
            for candidate in candidates:
                if os.path.exists(candidate):
                    return candidate
    return None


def config_dir() -> str:
    """Per-user settings folder: ~/.config/packrat on Linux, %APPDATA%\\packrat on Windows."""
    return platformdirs.user_config_dir(APP, appauthor=False, roaming=True)


def data_dir() -> str:
    """Per-user data folder (database, thumbnails): ~/.local/share/packrat on Linux,
    %LOCALAPPDATA%\\packrat on Windows."""
    return platformdirs.user_data_dir(APP, appauthor=False)


def default_download_dir() -> str:
    return os.environ.get("PACKRAT_DOWNLOADS") or os.path.join(os.path.expanduser("~"), "Packrat", "itch.io")


def default_path() -> str:
    return os.environ.get("PACKRAT_CONFIG", os.path.join(config_dir(), "config.toml"))


def load(path: str | None = None) -> Config:
    path = os.path.abspath(path or default_path())
    base = os.path.dirname(os.path.abspath(path))
    raw = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    paths = raw.get("paths", {})
    srv = raw.get("server", {})

    data = os.path.expanduser(paths.get("data_dir") or data_dir())
    if not os.path.isabs(data):
        data = os.path.join(base, data)
    os.makedirs(data, exist_ok=True)

    roots = []
    for r in raw.get("roots", []):
        p = os.path.normpath(r["path"])
        roots.append(Root(
            label=r.get("label") or os.path.basename(p),
            path=p,
            display=os.path.normpath(r["display"]) if r.get("display") else p,
            containers=list(r.get("containers", [])),
            ignore=list(r.get("ignore", [])),
        ))

    itch = raw.get("itch", {})
    download_dir = os.path.normpath(os.path.expanduser(itch.get("download_dir") or default_download_dir()))
    if not any(inside(download_dir, r.path) for r in roots):
        # Downloads must be indexed, so the download folder is always part of the library.
        roots.append(Root(label="itch.io downloads", path=download_dir, display=download_dir, containers=[],
                          auto=True))
    # Packs are grouped by label, so two folders with the same name (e.g. two "assets") need distinct ones.
    seen: set[str] = set()
    for r in roots:
        base, n = r.label, 2
        while r.label.lower() in seen:
            r.label, n = f"{base} ({n})", n + 1
        seen.add(r.label.lower())
    return Config(
        roots=roots,
        data_dir=data,
        # Environment variables win, so a container can be set up without editing the file.
        host=os.environ.get("PACKRAT_HOST") or srv.get("host", "127.0.0.1"),
        port=int(os.environ.get("PACKRAT_PORT") or srv.get("port", 8765)),
        sevenzip=paths.get("sevenzip") or find_7z(),
        itch_download_dir=download_dir,
        itch_sync_hours=float(itch.get("sync_every_hours", 24)),
        itch_auto_download=bool(itch.get("auto_download", False)),
        allowed_hosts=[h.lower() for h in srv.get("allowed_hosts", [])],
        source=path,
    )
