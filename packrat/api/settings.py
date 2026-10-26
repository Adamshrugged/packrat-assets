"""Settings: library folders, the itch.io key and options, and the folder picker."""
import os
import sqlite3
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import platform
from urllib.parse import urlencode

from .. import ISSUES_URL, __version__, autostart, config, db, humble, itch, program, secrets, settings, stores
from ..archives import tools as archive_tools
from ..guard import is_loopback
from . import Context, is_local


WINDOWS_CLI = '"$env:LOCALAPPDATA\\Programs\\Packrat\\packrat-cli.exe"'  # where the installer puts it


class RootIn(BaseModel):
    path: str
    label: str | None = None
    display: str | None = None
    containers: list[str] = []
    ignore: list[str] = []  # folders inside it to leave out, absolute or relative to it


class StoreIn(BaseModel):
    download_dir: str | None = None


class SyncIn(BaseModel):
    every_hours: float = 0
    auto_download: bool = False


class SettingsIn(BaseModel):
    roots: list[RootIn]
    itch: StoreIn = StoreIn()
    humble: StoreIn = StoreIn()
    sync: SyncIn = SyncIn()
    start_at_login: bool | None = None


class CookieIn(BaseModel):
    cookie: str


class KeyIn(BaseModel):
    key: str


def router(ctx: Context) -> APIRouter:
    r = APIRouter(prefix="/api")
    cfg = ctx.cfg
    conn = Depends(ctx.conn)

    def remote_mcp_command(request: Request, program_path: str) -> str:
        return (f"claude mcp add packrat --scope user -- {program_path} mcp --url "
                f"{str(request.base_url).rstrip('/')} --token {ctx.auth.api_token()}")

    def settings_out(c: sqlite3.Connection, request: Request) -> dict:
        return {
            "first_run": not os.path.exists(cfg.source),
            "roots": [{"label": x.label, "path": x.path, "display": x.display if x.display != x.path else None,
                       "containers": x.containers, "ignore": x.ignore} for x in cfg.roots if not x.auto],
            "itch": {"download_dir": cfg.itch_download_dir, "has_key": bool(secrets.itch_key()),
                     "user": db.get_kv(c, "itch_user")},
            "humble": {"download_dir": cfg.humble_download_dir, "connected": stores.connected()["humble"]},
            "sync": {"every_hours": cfg.sync_hours, "auto_download": cfg.auto_download},
            "version": __version__,
            "log_path": log if os.path.exists(log := os.path.join(cfg.data_dir, "packrat.log")) else None,
            # Opens GitHub's issue form with the version and system filled in; nothing is sent automatically.
            "report_url": f"{ISSUES_URL}/new?" + urlencode({"template": "bug.yml", "version": __version__,
                                                             "os": f"{platform.system()} {platform.release()}"}),
            "config_path": cfg.source, "data_dir": cfg.data_dir, "archive_tools": list(archive_tools(cfg.sevenzip)),
            "on_network": not is_loopback(cfg.host),
            "can_reveal": is_local(request),
            # Desktop-app options, for a browser on the same computer only
            "start_at_login": autostart.enabled() if is_local(request) and autostart.supported() else None,
            "can_quit": ctx.quit is not None and is_local(request),
            "auth": {"required": ctx.auth.required, "password_set": ctx.auth.password_set(),
                     "local": is_local(request)},
            # For Claude on other computers (needs Packrat installed there too). The Windows installer doesn't
            # put packrat on PATH, so that command names the installed program (for PowerShell).
            **({"mcp_remote_command": remote_mcp_command(request, "packrat"),
                "mcp_remote_command_windows": remote_mcp_command(request, WINDOWS_CLI)}
               if ctx.auth.required else {"mcp_remote_command": None, "mcp_remote_command_windows": None}),
        }

    @r.get("/settings")
    def get_settings(request: Request, c: sqlite3.Connection = conn):
        return settings_out(c, request)

    def inner_folders(root: str, folders: list[str]) -> list[str]:
        """Folders inside a library folder (absolute or relative to it), as sorted '/'-separated relative paths."""
        out = set()
        for f in folders:
            full = os.path.normpath(os.path.join(root, os.path.expanduser(f.strip())))
            if not config.inside(full, root) or os.path.normcase(full) == os.path.normcase(root):
                raise HTTPException(400, f"choose a folder inside {root}: {f}")
            out.add(os.path.relpath(full, root).replace(os.sep, "/"))
        return sorted(out)

    def download_folder(chosen: str | None, default: str) -> str:
        folder = os.path.normpath(os.path.expanduser((chosen or "").strip() or default))
        try:
            if folder != default:  # the default is made on first download
                os.makedirs(folder, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"can't create the download folder: {e.strerror}") from None
        return folder

    @r.put("/settings")
    def put_settings(req: SettingsIn, request: Request, c: sqlite3.Connection = conn):
        roots, seen = [], set()
        for x in req.roots:
            path = os.path.normpath(os.path.expanduser(x.path.strip()))
            if not os.path.isdir(path):
                raise HTTPException(400, f"library folder not found: {path}")
            if os.path.normcase(path) in seen:
                continue
            seen.add(os.path.normcase(path))
            roots.append({**x.model_dump(), "path": path,
                          "containers": inner_folders(path, x.containers), "ignore": inner_folders(path, x.ignore)})
        download_dirs = {store: download_folder(getattr(req, store).download_dir, config.DEFAULT_DOWNLOADS[store]())
                         for store in config.DEFAULT_DOWNLOADS}
        if req.sync.every_hours < 0:
            raise HTTPException(400, "sync interval can't be negative")
        if req.start_at_login is not None and is_local(request) and autostart.supported():
            autostart.set_enabled(req.start_at_login)
        old_roots = [(x.path, x.containers, x.ignore) for x in cfg.roots]
        settings.save(cfg, roots, download_dirs, req.sync.model_dump())
        rescan = [(x.path, x.containers, x.ignore) for x in cfg.roots] != old_roots
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

    def forget_secret(name: str) -> None:
        values = secrets.load()
        values.pop(name, None)
        settings.save_secrets(values)

    @r.delete("/settings/itch-key")
    def delete_itch_key(request: Request, c: sqlite3.Connection = conn):
        forget_secret("itch_api_key")
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

    @r.post("/settings/humble-cookie")
    def set_humble_cookie(req: CookieIn, request: Request, c: sqlite3.Connection = conn):
        """Check the cookie with Humble, store it, and start a sync. The cookie is never sent back."""
        cookie = req.cookie.strip().removeprefix("_simpleauth_sess=").strip('"')
        if not cookie:
            raise HTTPException(400, "paste the cookie first")
        try:
            humble.Client(cookie, {"humble_session_cookie": cookie}).order_keys()
        except humble.HumbleError:
            raise HTTPException(400, "Humble didn't accept this cookie. Sign in at humblebundle.com and copy it again.") from None
        settings.save_secrets({**secrets.load(), "humble_session_cookie": cookie})
        ctx.start_sync()
        return settings_out(c, request)

    @r.delete("/settings/humble-cookie")
    def delete_humble_cookie(request: Request, c: sqlite3.Connection = conn):
        forget_secret("humble_session_cookie")
        return settings_out(c, request)

    @r.get("/browse")
    def browse(path: str = ""):
        """Folder listing for the folder picker."""
        try:
            return settings.browse(path or None)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from None

    @r.get("/mcp-command")
    def mcp_command():
        """The command that registers Packrat's MCP server with Claude Code. It names this install's program by
        full path, so it works even when the packrat command isn't on PATH (a venv, or the installed app)."""
        return {"command": "claude mcp add packrat --scope user -- " + program.shell_line(program.command() + ["mcp"])}

    return r
