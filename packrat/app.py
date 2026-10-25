"""The Packrat web app: the API routers, the web UI's files, the request guard and the sync schedule."""
import os
import threading
import time
from contextlib import closing

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, secrets
from .api import Context, library, settings, store
from .guard import HostGuard

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def create_app(cfg) -> FastAPI:
    db.init(cfg)
    ctx = Context(cfg)
    app = FastAPI(title="Packrat")
    for module in (library, store, settings):
        app.include_router(module.router(ctx))
    app.mount("/", WebFiles(directory=WEB_DIR, html=True), name="web")
    app.add_middleware(TextGZip)
    app.add_middleware(HostGuard, bind_host=cfg.host, allowed_hosts=cfg.allowed_hosts)

    threading.Thread(target=schedule_syncs, args=(ctx,), daemon=True).start()
    with closing(db.connect(cfg)) as c:
        empty = c.execute("SELECT count(*) FROM packs").fetchone()[0] == 0
    if empty:  # a new install indexes its library folders on its own
        ctx.start_scan("first-time scan…")
    return app


def schedule_syncs(ctx: Context) -> None:
    """Sync itch.io every [itch] sync_every_hours while Packrat runs, counted from the last attempt."""
    while True:
        time.sleep(60)
        if ctx.cfg.itch_sync_hours <= 0 or not secrets.itch_key():
            continue
        with closing(db.connect(ctx.cfg)) as c:
            last = max(float(db.get_kv(c, key, 0) or 0) for key in ("itch_last_sync", "itch_last_attempt"))
        if time.time() - last >= ctx.cfg.itch_sync_hours * 3600:
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
