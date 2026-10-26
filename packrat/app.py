"""The Packrat web app: the API routers, the web UI's files, the request guard and the sync schedule."""
import os
import threading
import time
from contextlib import closing

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, stores
from .api import Context, auth, library, settings, store
from .auth import AuthGuard
from .guard import HostGuard

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def create_app(cfg, quit=None) -> FastAPI:
    """quit: set by the desktop app (launcher.py) so Settings can offer Quit; None for `packrat serve`."""
    db.init(cfg)
    ctx = Context(cfg, quit)
    app = FastAPI(title="Packrat")
    app.state.ctx = ctx
    for module in (library, store, settings, auth):
        app.include_router(module.router(ctx))
    app.mount("/", WebFiles(directory=WEB_DIR, html=True), name="web")
    # Outermost first: right host and origin, then signed in (network mode), then compression.
    app.add_middleware(TextGZip)
    app.add_middleware(AuthGuard, auth=ctx.auth)
    app.add_middleware(HostGuard, bind_host=cfg.host, allowed_hosts=cfg.allowed_hosts)

    threading.Thread(target=schedule_syncs, args=(ctx,), daemon=True).start()
    with closing(db.connect(cfg)) as c:
        empty = c.execute("SELECT count(*) FROM packs").fetchone()[0] == 0
    if empty:  # a new install indexes its library folders on its own
        ctx.start_scan("first-time scan…")
    return app


def schedule_syncs(ctx: Context) -> None:
    """Sync the connected stores every [sync] every_hours while Packrat runs, counted from the last attempt."""
    while True:
        time.sleep(60)
        if ctx.cfg.sync_hours <= 0 or not any(stores.connected().values()):
            continue
        with closing(db.connect(ctx.cfg)) as c:
            last = max(float(db.get_kv(c, key, 0) or 0)
                       for key in ("itch_last_sync", "humble_last_sync", "last_sync_attempt"))
        if time.time() - last >= ctx.cfg.sync_hours * 3600:
            ctx.start_sync("scheduled sync…")


class WebFiles(StaticFiles):
    """The UI's own files. Browsers must re-check them on every load (a cheap 304 if unchanged), or an
    upgraded Packrat would keep running the old page from the browser cache."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class TextGZip:
    """gzip API data and the page's own code (the pack list shrinks ~12x), but not file contents: images
    and archives are already compressed, and audio playback needs byte-range requests to seek."""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        text = (path.startswith("/api/") and not path.endswith(("/raw", "/thumb"))) or \
            path == "/" or path.endswith((".js", ".css", ".html"))
        await (self.gzip if text else self.app)(scope, receive, send)
