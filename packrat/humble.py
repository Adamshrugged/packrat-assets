"""Humble Bundle (experimental): import game-asset purchases and their files, match them to the library, and
download what's missing.

Humble has no public API. This uses the same JSON endpoints as Humble's own library page, signed in with the
browser's session cookie (_simpleauth_sess), pasted in Settings. The cookie is only sent to humblebundle.com
and expires when you sign out of Humble, so it needs pasting again now and then. If Humble changes these
endpoints, syncing breaks until Packrat is updated.

Only game assets are listed: products whose bundle or title looks like assets, or whose files are already in
the library. Games, books and software are skipped.
"""
import json
import logging
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import db, net, secrets
from .stores import StoreError, match

log = logging.getLogger("packrat.humble")

API = "https://www.humblebundle.com/api/v1"
COOKIE_HELP = "Paste a fresh one in Settings (see the Guide)."


class HumbleError(StoreError):
    pass


class Client(net.Client):
    error = HumbleError
    gap = 0.4  # seconds between requests: Humble's own site is the API

    def __init__(self, cookie: str, secret_values: dict):
        super().__init__(secret_values)
        self._cookie = cookie

    def headers(self, auth: bool) -> dict:
        return {**super().headers(auth), "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                "Cookie": f"_simpleauth_sess={self._cookie}"}

    def check(self, response) -> None:
        if "json" not in (response.headers.get("Content-Type") or ""):
            raise HumbleError(f"the cookie expired. {COOKIE_HELP}")

    def http_error(self, e):
        if e.code in (401, 403):
            return HumbleError(f"the cookie expired or you signed out. {COOKIE_HELP}")
        return None

    def order_keys(self) -> list[str]:
        return [o["gamekey"] if isinstance(o, dict) else str(o) for o in self.get(f"{API}/user/order")]

    def order(self, gamekey: str) -> dict:
        return self.get(f"{API}/order/{urllib.parse.quote(gamekey)}?all_tcs=true")


def client_from_secrets() -> Client:
    values = secrets.load()
    cookie = values.get("humble_session_cookie")
    if not cookie:
        raise HumbleError("connect Humble in Settings first")
    return Client(cookie, values)


# ------------------------------------------------------------------ orders

def _filename(url: str) -> str:
    return urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))


def items_from_order(order: dict) -> list[dict]:
    """Flatten an order into purchased products with their downloadable files (key-only games are skipped)."""
    gamekey = order.get("gamekey", "")
    bundle = (order.get("product") or {}).get("human_name", "")
    out = []
    for sub in order.get("subproducts") or []:
        files = []
        for dl in sub.get("downloads") or []:
            platform = dl.get("platform") or ""
            for st in dl.get("download_struct") or []:
                web = (st.get("url") or {}).get("web")
                if not web:
                    continue  # external links, streaming-only, etc.
                name = _filename(web)
                files.append({"upload_id": st.get("md5") or st.get("sha1") or f"{platform}:{name}",
                              "filename": name, "display_name": st.get("name") or "", "size": st.get("file_size"),
                              "md5": st.get("md5"), "type": platform})
        if not files:
            continue
        out.append({
            "store_id": sub.get("machine_name") or f"{gamekey}:{sub.get('human_name')}",
            "key_id": gamekey, "title": sub.get("human_name") or "?",
            "author": (sub.get("payee") or {}).get("human_name", ""), "url": sub.get("url") or "",
            "cover_url": sub.get("icon") or "", "purchased_at": order.get("created") or "",
            "bundle": bundle, "files": files,
        })
    return out


# Bundles and products that are game-development assets; anything else is skipped unless its files are
# already in the library (stores.match marks those as assets).
ASSET_BUNDLE_RE = re.compile(r"asset|game ?dev|game art|gamemaker|rpg maker|sprite|tileset|pixel art|"
                             r"sound effects|\bsfx\b|music pack|game ?maker", re.I)
ASSET_TITLE_RE = re.compile(r"\b(tile ?sets?|sprites?|asset pack|assets|sfx|sound effects|music pack|"
                            r"icons? pack|ui pack|gui pack|game art|textures?)\b", re.I)


def looks_like_assets(bundle: str, title: str) -> bool:
    return bool(ASSET_BUNDLE_RE.search(bundle or "") or ASSET_TITLE_RE.search(title or ""))


def sync(cfg, progress=None, force: bool = False) -> dict:
    """Import Humble purchases. Orders already imported are skipped unless force (they don't change)."""
    db.init(cfg)
    client = client_from_secrets()
    conn = db.connect(cfg)
    if progress:
        progress(0, 0, "fetching Humble orders…")
    keys = client.order_keys()
    done = set(json.loads(db.get_kv(conn, "humble_orders_done", "[]")))
    todo = keys if force else [k for k in keys if k not in done]
    first_import = db.get_kv(conn, "humble_first_sync") is None

    def fetch(key):
        try:
            return key, client.order(key), None
        except HumbleError as e:
            return key, None, str(e)

    errors, fetched = [], 0
    with ThreadPoolExecutor(3) as pool:
        for key, order, err in pool.map(fetch, todo):
            fetched += 1
            if progress:
                progress(fetched, len(todo), "fetching Humble orders")
            if err:
                errors.append(err)
                if "cookie" in err:
                    break  # every other order fails the same way
                continue
            with conn:
                for it in items_from_order(order):
                    _store(conn, it, first_import)
            done.add(key)
    db.set_kv(conn, "humble_orders_done", json.dumps(sorted(done)))
    db.set_kv(conn, "humble_last_sync", time.time())
    db.set_kv(conn, "humble_first_sync", db.get_kv(conn, "humble_first_sync") or time.time())
    db.set_kv(conn, "humble_last_errors", json.dumps([f"Humble: {e}" for e in sorted(set(errors))][:200]))
    stats = match(cfg, conn)
    items = conn.execute("SELECT count(*) FROM store_items WHERE store='humble' AND asset=1").fetchone()[0]
    conn.close()
    if errors and fetched == len(errors):  # nothing worked: say why instead of "0 new purchases"
        raise HumbleError(errors[0])
    return {"orders": len(keys), "fetched": len(todo), "purchases": items, "errors": len(errors),
            "error_samples": errors[:3], **stats}


def _store(conn, it: dict, first_import: bool) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO store_items(store, store_id, key_id, title, author, url, cover_url, purchased_at, info, "
        "first_seen, asset) VALUES ('humble',?,?,?,?,?,?,?,?,?,?) ON CONFLICT(store, store_id) DO UPDATE SET "
        "key_id=excluded.key_id, title=excluded.title, author=excluded.author, url=excluded.url, "
        "cover_url=excluded.cover_url, info=excluded.info",
        (it["store_id"], it["key_id"], it["title"], it["author"], it["url"], it["cover_url"], it["purchased_at"],
         json.dumps({"Bundle": it["bundle"]}), None if first_import else now,  # the first import is backlog
         int(looks_like_assets(it["bundle"], it["title"]))),
    )
    item_id = conn.execute("SELECT id FROM store_items WHERE store='humble' AND store_id=?",
                           (it["store_id"],)).fetchone()[0]
    for f in it["files"]:
        conn.execute(
            "INSERT INTO store_files(item_id, upload_id, filename, display_name, size, md5, type) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(item_id, upload_id) DO UPDATE SET filename=excluded.filename, "
            "display_name=excluded.display_name, size=excluded.size, md5=excluded.md5, type=excluded.type",
            (item_id, f["upload_id"], f["filename"], f["display_name"], f["size"], f["md5"], f["type"]),
        )


# ------------------------------------------------------------------ downloading

def download_link(row, session: dict) -> tuple[str, None]:
    """A fresh link for one purchased file (they expire). No MD5 to check: Humble's listed MD5 (and size)
    go stale when a publisher replaces a file."""
    if "humble" not in session:  # one client and set of orders per download batch
        session["humble"] = (client_from_secrets(), {})
    client, orders = session["humble"]
    if row["key_id"] not in orders:
        orders[row["key_id"]] = client.order(row["key_id"])
    return _fresh_url(orders[row["key_id"]], row), None


def _fresh_url(order: dict, row) -> str:
    for sub in order.get("subproducts") or []:
        for dl in sub.get("downloads") or []:
            for st in dl.get("download_struct") or []:
                web = (st.get("url") or {}).get("web")
                if web and ((row["md5"] and st.get("md5") == row["md5"]) or _filename(web) == row["filename"]):
                    return web
    raise HumbleError("this file is no longer listed in the Humble order")
