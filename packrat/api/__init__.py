"""The web API. Each module builds one router from the shared Context."""
import sqlite3
import time
from contextlib import closing

from fastapi import HTTPException, Request

from .. import db
from ..auth import Auth
from ..guard import is_loopback
from ..jobs import Downloads, Jobs


class Context:
    """What the routers share: the settings, the database and the background jobs."""

    def __init__(self, cfg, quit=None):
        self.cfg = cfg
        self.quit = quit  # stops the desktop app; None when run as a server
        self.auth = Auth(required=not is_loopback(cfg.host))
        self.jobs = Jobs()
        self.downloads = Downloads(cfg, self.jobs)

    def conn(self):
        """FastAPI dependency: a database connection for one request, closed afterwards."""
        c = db.connect(self.cfg)
        try:
            yield c
        finally:
            c.close()

    def start_scan(self, message: str = "discovering packs…") -> dict:
        from ..scanner import scan
        return self.jobs.start("scan", lambda p: scan(self.cfg, p), message)

    def start_sync(self, message: str = "fetching purchases…") -> dict:
        """Sync itch.io purchases (and auto-download new ones if enabled). Records the attempt, so the
        scheduler waits a full interval even after a failed sync instead of retrying every minute."""
        from ..itch import sync_and_auto_download
        with closing(db.connect(self.cfg)) as c:
            db.set_kv(c, "itch_last_attempt", time.time())
        return self.jobs.start("itch", lambda p: sync_and_auto_download(self.cfg, p), message)


def is_local(request: Request) -> bool:
    """True for a browser on the computer running Packrat (only it can open a file manager here)."""
    return request.client is not None and is_loopback(request.client.host)


def get_file(c: sqlite3.Connection, file_id: int):
    row = c.execute("SELECT f.*, p.name AS pack_name FROM files f JOIN packs p ON p.id = f.pack_id WHERE f.id=?",
                    (file_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "file not found (try reloading)")
    return row


def split_tags(s: str) -> list[str]:
    return [t for t in (s or "").split(",") if t]
