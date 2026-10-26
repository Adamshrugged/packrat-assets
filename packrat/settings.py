"""Settings edited from the web UI: writes config.toml and secrets.toml, and lists folders for the folder picker."""
import getpass
import json
import os
import string

from . import config

HEADER = "# Packrat settings. Edit them in Packrat (Settings), or here while Packrat is stopped.\n"


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    return json.dumps(str(v))  # a JSON string is a valid TOML basic string


def to_toml(data: dict) -> str:
    """Enough TOML for Packrat's settings: tables of plain values, and arrays of tables."""
    out = [HEADER]
    for name, table in data.items():
        rows = table if isinstance(table, list) else [table]
        for row in rows:
            out.append(f"\n[[{name}]]\n" if isinstance(table, list) else f"\n[{name}]\n")
            out += [f"{k} = {_value(v)}\n" for k, v in row.items() if v is not None]
    return "".join(out)


def write_private(path: str, text: str) -> None:
    """Write via a temp file so a crash never leaves half a file; readable only by this user."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save(cfg, roots: list[dict], download_dirs: dict, sync: dict) -> None:
    """Write config.toml from the given settings, then reload it into cfg (in place, so the running app
    sees the change). Settings not edited in the UI (data folder, server) are kept as they are."""
    data = {
        "paths": {"data_dir": cfg.data_dir if cfg.data_dir != config.data_dir() else None},
        "server": {"host": cfg.host if cfg.host != "127.0.0.1" else None,
                   "port": cfg.port if cfg.port != 8765 else None,
                   "allowed_hosts": cfg.allowed_hosts or None},
        # download folders are only written when they aren't the default
        **{store: {"download_dir": folder if folder not in (None, config.DEFAULT_DOWNLOADS[store]()) else None}
           for store, folder in download_dirs.items()},
        "sync": {"every_hours": float(sync.get("every_hours") or 0),  # 0 = off
                 "auto_download": bool(sync.get("auto_download"))},
        "roots": [{"label": r.get("label") or None, "path": r["path"], "display": r.get("display") or None,
                   "containers": r.get("containers") or None, "ignore": r.get("ignore") or None}
                  for r in roots],
    }
    data = {k: v for k, v in data.items() if v and (isinstance(v, list) or any(x is not None for x in v.values()))}
    write_private(cfg.source, to_toml(data))
    cfg.__dict__.update(config.load(cfg.source).__dict__)


def save_secrets(values: dict) -> None:
    from . import secrets
    write_private(secrets.path(), to_toml({}) + "".join(f"{k} = {_value(v)}\n" for k, v in values.items()))


def browse(path: str | None) -> dict:
    """Subfolders of path, for the folder picker. With no path: the home folder plus drives or /."""
    home = os.path.expanduser("~")
    if not path:
        if os.name == "nt":
            places = [home] + [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
        else:
            places = [home, "/"] + [p for p in ("/library", "/mnt", "/media", f"/run/media/{getpass.getuser()}")
                                    if os.path.isdir(p)]  # /library: where the Docker image mounts it
        return {"path": None, "parent": None, "dirs": [{"name": p, "path": p} for p in places]}
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise FileNotFoundError(f"not a folder: {path}")
    try:
        names = sorted((e.name for e in os.scandir(path) if e.is_dir() and not e.name.startswith(".")),
                       key=str.lower)
    except PermissionError:
        names = []
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None,
            "dirs": [{"name": n, "path": os.path.join(path, n)} for n in names]}
