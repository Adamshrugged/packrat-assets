"""Settings: library folders, the itch.io key and options, and the folder picker."""
import os
import sqlite3
import sys
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import autostart, config, db, itch, secrets, settings
from ..archives import tools as archive_tools
from ..guard import is_loopback
from . import Context, is_local


class RootIn(BaseModel):
    path: str
    label: str | None = None
    display: str | None = None
    containers: list[str] = []


class ItchIn(BaseModel):
    download_dir: str | None = None
    sync_every_hours: float = 0
    auto_download: bool = False


class SettingsIn(BaseModel):
    roots: list[RootIn]
    itch: ItchIn
    start_at_login: bool | None = None


class KeyIn(BaseModel):
    key: str


def router(ctx: Context) -> APIRouter:
    r = APIRouter(prefix="/api")
    cfg = ctx.cfg
    conn = Depends(ctx.conn)

    def settings_out(c: sqlite3.Connection, request: Request) -> dict:
        return {
            "first_run": not os.path.exists(cfg.source),
            "roots": [{"label": x.label, "path": x.path, "display": x.display if x.display != x.path else None,
                       "containers": x.containers} for x in cfg.roots if not x.auto],
            "itch": {"download_dir": cfg.itch_download_dir, "sync_every_hours": cfg.itch_sync_hours,
                     "auto_download": cfg.itch_auto_download, "has_key": bool(secrets.itch_key()),
                     "user": db.get_kv(c, "itch_user")},
            "config_path": cfg.source, "data_dir": cfg.data_dir, "archive_tools": list(archive_tools(cfg.sevenzip)),
            "on_network": not is_loopback(cfg.host),
            "can_reveal": is_local(request),
            # Desktop-app options, for a browser on the same computer only
            "start_at_login": autostart.enabled() if is_local(request) and autostart.supported() else None,
            "can_quit": ctx.quit is not None and is_local(request),
            "auth": {"required": ctx.auth.required, "password_set": ctx.auth.password_set(),
                     "local": is_local(request)},
            # For Claude on other computers (needs Packrat installed there too)
            "mcp_remote_command": (f"claude mcp add packrat --scope user -- packrat mcp --url "
                                   f"{str(request.base_url).rstrip('/')} --token {ctx.auth.api_token()}"
                                   if ctx.auth.required else None),
        }

    @r.get("/settings")
    def get_settings(request: Request, c: sqlite3.Connection = conn):
        return settings_out(c, request)

    @r.put("/settings")
    def put_settings(req: SettingsIn, request: Request, c: sqlite3.Connection = conn):
        roots, seen = [], set()
        for x in req.roots:
            path = os.path.normpath(os.path.expanduser(x.path.strip()))
            if not os.path.isdir(path):
                raise HTTPException(400, f"library folder not found: {path}")
            if os.path.normcase(path) not in seen:
                seen.add(os.path.normcase(path))
                roots.append({**x.model_dump(), "path": path})
        download_dir = os.path.normpath(os.path.expanduser((req.itch.download_dir or "").strip()
                                                           or config.default_download_dir()))
        try:
            if download_dir != config.default_download_dir():  # the default is made on first download
                os.makedirs(download_dir, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"can't create the download folder: {e.strerror}") from None
        if req.itch.sync_every_hours < 0:
            raise HTTPException(400, "sync interval can't be negative")
        if req.start_at_login is not None and is_local(request) and autostart.supported():
            autostart.set_enabled(req.start_at_login)
        old_roots = [x.path for x in cfg.roots]
        settings.save(cfg, roots, {**req.itch.model_dump(), "download_dir": download_dir})
        rescan = [x.path for x in cfg.roots] != old_roots
        if rescan:
            ctx.start_scan("scanning library folders…")
        return {**settings_out(c, request), "rescan": rescan}

    @r.post("/settings/itch-key")
    def set_itch_key(req: KeyIn, request: Request, c: sqlite3.Connection = conn):
        """Check the key with itch.io, store it, and start the first sync. The key is never sent back."""
        key = req.key.strip()
        if not key:
            raise HTTPException(400, "paste an API key first")
        try:
            user = itch.Client(key, {"itch_api_key": key}).profile()
        except itch.ItchError:
            raise HTTPException(400, "itch.io rejected this key. Copy it again from itch.io.") from None
        settings.save_secrets({**secrets.load(), "itch_api_key": key})
        db.set_kv(c, "itch_user", itch.user_name(user))
        ctx.start_sync()
        return settings_out(c, request)

    @r.delete("/settings/itch-key")
    def delete_itch_key(request: Request, c: sqlite3.Connection = conn):
        values = secrets.load()
        values.pop("itch_api_key", None)
        settings.save_secrets(values)
        with c:
            c.execute("DELETE FROM kv WHERE key='itch_user'")
        return settings_out(c, request)

    @r.post("/quit")
    def quit_app(request: Request):
        """Quit the desktop app (also in its tray menu). Answers first, then stops."""
        if ctx.quit is None or not is_local(request):
            raise HTTPException(403, "only the desktop app on this computer can be quit from here")
        threading.Timer(0.3, ctx.quit).start()
        return {"ok": True}

    @r.get("/browse")
    def browse(path: str = ""):
        """Folder listing for the folder picker."""
        try:
            return settings.browse(path or None)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from None

    @r.get("/mcp-command")
    def mcp_command():
        """The command that registers Packrat's MCP server with Claude Code. It names this Python by full
        path, so it works even when the packrat command isn't on PATH (a venv, or an installer)."""
        return {"command": f'claude mcp add packrat --scope user -- "{sys.executable}" -m packrat mcp'}

    return r
