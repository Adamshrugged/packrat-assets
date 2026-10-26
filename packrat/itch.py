"""itch.io integration: import purchases, their files and page metadata; match them to the library.

Uses the official API (https://api.itch.io) with the personal API key (Settings, stored in secrets.toml),
sent as a header so it never appears in URLs or error messages. Public asset pages are
fetched without credentials for tags/license.
"""
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import db, net, secrets
from .stores import StoreError, fetch_to, match, pending_files, safe_name

log = logging.getLogger("packrat.itch")

API = "https://api.itch.io"
UPLOADS_MAX_AGE = 7 * 86400
META_MAX_AGE = 30 * 86400


class ItchError(StoreError):
    pass


class KeyRejected(ItchError):
    """The API key was revoked or is wrong. Every request will fail, so a sync stops at the first one."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx as an HTTPError so we can read Location


_NO_REDIRECT = urllib.request.build_opener(_NoRedirectHandler)


class Client(net.Client):
    error = ItchError

    def __init__(self, key: str, secret_values: dict):
        super().__init__(secret_values)
        self._key = key

    def headers(self, auth: bool) -> dict:
        h = super().headers(auth)
        if auth:
            h["Authorization"] = f"Bearer {self._key}"
        return h

    def http_error(self, e: urllib.error.HTTPError) -> Exception | None:
        # 403 on a game's uploads can mean just that purchase is gone (refunded); on /profile it's the key.
        if e.code == 401 or (e.code == 403 and "/profile" in (e.url or "")):
            return KeyRejected("itch.io rejected your API key. Reconnect in Settings.")
        return None

    def profile(self) -> dict:
        return self.get(f"{API}/profile").get("user") or {}

    def owned_keys(self) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self.get(f"{API}/profile/owned-keys?page={page}").get("owned_keys", [])
            if not batch:
                return out
            out.extend(batch)
            page += 1

    def uploads(self, game_id: str, key_id: str | None) -> list[dict]:
        # Free assets added by URL have no download key; their uploads are public.
        return self.get(f"{API}/games/{game_id}/uploads{_key_param(key_id)}").get("uploads", [])

    def game(self, game_id: str) -> dict:
        return self.get(f"{API}/games/{game_id}").get("game") or {}


    def download_url(self, upload_id: str, key_id: str | None) -> str:
        """The API answers with a redirect to a signed storage URL. Read it without following it, so the
        Authorization header (the API key) is never sent to the storage host."""
        req = urllib.request.Request(f"{API}/uploads/{upload_id}/download{_key_param(key_id)}",
                                     headers=self.headers(auth=True))
        self.throttle()
        try:
            _NO_REDIRECT.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
            if location:
                return location
            raise ItchError(f"HTTP {e.code} getting download link for upload {upload_id}") from None
        raise ItchError("itch.io did not return a download link")


def _key_param(key_id: str | None) -> str:
    return f"?download_key_id={key_id}" if key_id else ""


def client_from_secrets() -> Client:
    key = secrets.itch_key()
    if not key:
        raise ItchError("connect itch.io in Settings first")
    return Client(key, secrets.load())


_PUBLIC = Client("", {})  # for public pages: same pacing and retries, never sends a key


def fetch_public(url: str) -> str:
    return _PUBLIC.get(url, auth=False, raw=True).decode("utf-8", "replace")


def user_name(user: dict) -> str:
    return user.get("display_name") or user.get("username") or ""


# ------------------------------------------------------------------ page parsing

def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


TERMS_HEAD = re.compile(r"\b(licen[cs]e|terms|usage|rights|commercial use|permitted|allowed)\b", re.I)


def parse_page(body: str) -> dict:
    meta: dict = {"info": {}}
    m = re.search(r'class="game_info_panel_widget[^"]*"[^>]*>\s*<table>(.*?)</table>', body, re.S)
    if m:
        for label, value in re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td></tr>", m.group(1), re.S):
            label = _text(label)
            links = [_text(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", value, re.S)]
            text = ", ".join(links) if links else _text(value)
            low = label.lower()
            if low == "tags":
                meta["tags"] = links
            elif low in ("author", "authors"):
                meta["author"] = text
            elif "license" in low:
                meta["license"] = text
            elif low == "ai disclosure":
                meta["ai"] = text
            elif low not in ("links", "rating"):
                meta["info"][label] = text

    # Usage terms written into the description ("Terms of Use", "License", ...).
    d = re.search(r'class="formatted_description[^"]*">(.*?)<div class="more_information_toggle', body, re.S)
    if d:
        text = re.sub(r"<(br|/p|/h\d|/li|/div)[^>]*>", "\n", d.group(1))
        lines = [ln.strip() for ln in _text(text).splitlines() if ln.strip()]
        for i, ln in enumerate(lines):
            if len(ln) < 80 and TERMS_HEAD.search(ln):
                meta["terms"] = "\n".join(lines[i:i + 8])[:800]
                break
    return meta


# ------------------------------------------------------------------ sync

def _upsert_items(conn, keys: list[dict]) -> None:
    with conn:
        for k in keys:
            g = k.get("game") or {}
            user = g.get("user") or {}
            conn.execute(
                "INSERT INTO store_items(store, store_id, key_id, title, author, author_url, url, cover_url, "
                "classification, purchased_at, first_seen) VALUES ('itch',?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(store, store_id) DO UPDATE SET key_id=excluded.key_id, title=excluded.title, "
                "author=excluded.author, author_url=excluded.author_url, url=excluded.url, "
                "cover_url=excluded.cover_url, classification=excluded.classification, "
                "purchased_at=min(store_items.purchased_at, excluded.purchased_at)",
                (str(g.get("id") or k.get("game_id")), str(k["id"]), g.get("title") or "?",
                 user_name(user), user.get("url") or "",
                 g.get("url") or "", g.get("cover_url") or "", g.get("classification") or "",
                 k.get("created_at") or "", time.time()),
            )


def _refresh_item(cfg, client: Client, item, now: float, force: bool) -> str | None:
    conn = db.connect(cfg)
    try:
        if force or not item["uploads_fetched_at"] or now - item["uploads_fetched_at"] > UPLOADS_MAX_AGE:
            ups = client.uploads(item["store_id"], item["key_id"])
            with conn:
                keep = [str(u["id"]) for u in ups]
                conn.execute(f"DELETE FROM store_files WHERE item_id=? AND upload_id NOT IN ({db.placeholders(keep)})",
                             (item["id"], *keep))
                for u in ups:
                    conn.execute(
                        "INSERT INTO store_files(item_id, upload_id, filename, display_name, size, md5, type, "
                        "updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(item_id, upload_id) DO UPDATE SET "
                        "filename=excluded.filename, display_name=excluded.display_name, size=excluded.size, "
                        "md5=excluded.md5, type=excluded.type, updated_at=excluded.updated_at",
                        (item["id"], str(u["id"]), u.get("filename") or "", u.get("display_name") or "",
                         u.get("size"), u.get("md5_hash"), u.get("type") or "", u.get("updated_at") or ""),
                    )
                conn.execute("UPDATE store_items SET uploads_fetched_at=? WHERE id=?", (now, item["id"]))
        if item["url"] and (force or not item["meta_fetched_at"] or now - item["meta_fetched_at"] > META_MAX_AGE):
            try:
                meta = parse_page(fetch_public(item["url"]))
            except ItchError as e:
                if "HTTP 404" not in str(e):
                    raise
                meta = {"info": {"Page": "no longer available on itch.io"}}  # unlisted/removed; don't retry
            with conn:
                conn.execute(
                    "UPDATE store_items SET tags=?, license=?, terms=?, ai=?, info=?, meta_fetched_at=?, "
                    "author=CASE WHEN ?<>'' THEN ? ELSE author END WHERE id=?",
                    (",".join(meta.get("tags", [])), meta.get("license", ""), meta.get("terms", ""),
                     meta.get("ai", ""), json.dumps(meta.get("info", {})), now,
                     meta.get("author", ""), meta.get("author", ""), item["id"]),
                )
        return None
    except KeyRejected:
        raise
    except ItchError as e:
        return f"{item['title']}: {e}"
    finally:
        conn.close()


def sync(cfg, progress=None, force: bool = False) -> dict:
    db.init(cfg)
    client = client_from_secrets()
    conn = db.connect(cfg)
    if progress:
        progress(0, 0, "fetching purchases…")
    keys = client.owned_keys()
    first_import = db.get_kv(conn, "itch_first_sync") is None
    _upsert_items(conn, keys)
    if first_import:
        # Everything in the first import is "backlog"; auto-download only applies to purchases after it.
        with conn:
            conn.execute("UPDATE store_items SET first_seen=NULL WHERE store='itch'")
        db.set_kv(conn, "itch_first_sync", time.time())
    db.set_kv(conn, "itch_last_sync", time.time())
    items = conn.execute("SELECT * FROM store_items WHERE store='itch'").fetchall()
    now = time.time()
    errors, done = [], 0
    with ThreadPoolExecutor(4) as pool:
        for err in pool.map(lambda it: _refresh_item(cfg, client, it, now, force), items):
            done += 1
            if err:
                errors.append(err)
                log.warning(err)
            if progress:
                progress(done, len(items), "fetching files and page info")
    db.set_kv(conn, "itch_last_errors", json.dumps(sorted(errors)[:200]))
    stats = match(cfg, conn)
    conn.close()
    return {"purchases": len(items), "errors": len(errors), "error_samples": errors[:5], **stats}


# ------------------------------------------------------------------ free assets (added by URL or search)

# Free downloads don't create a download key, so they never show up in owned-keys. They are stored as itch
# items with key_id NULL; their uploads and download links work without one. If the asset is later claimed
# or bought, the sync fills in the key on the same row (store_id is the game id either way).

PAGE_RE = re.compile(r"^(?:https?://)?([a-z0-9][a-z0-9-]*)\.itch\.io/([a-z0-9][a-z0-9_-]*)/?(?:[?#].*)?$", re.I)
SEARCH_URL = "https://itch.io/search?type=games&classification=assets&q="
FREE_TAG_URL = "https://itch.io/game-assets/free/tag-"


def page_url(url: str) -> str:
    m = PAGE_RE.match((url or "").strip())
    if not m:
        raise ItchError("not an itch.io page link (it should look like https://author.itch.io/asset-name)")
    return f"https://{m.group(1).lower()}.itch.io/{m.group(2).lower()}"


def _resolve(client: Client, url: str) -> tuple[str, dict]:
    """Page URL -> (game id, API game object)."""
    url = page_url(url)
    try:
        data = client.get(url + "/data.json", auth=False)
    except ItchError as e:
        if "HTTP 404" in str(e):
            raise ItchError("itch.io has no public page at that link (private, removed or mistyped)") from None
        raise
    gid = str(data["id"])
    game = client.game(gid)
    game.setdefault("url", url)
    game.setdefault("title", data.get("title") or "?")
    game["price_label"] = data.get("price") or ""
    return gid, game


def is_free(game: dict) -> bool:
    return not (game.get("min_price") or 0)


def lookup(url: str) -> dict:
    """Preview an itch.io page before adding it: what it is, whether it's free, its files."""
    client = client_from_secrets()
    gid, game = _resolve(client, url)
    free = is_free(game)
    files = [{"filename": u.get("filename") or "", "size": u.get("size"), "type": u.get("type") or ""}
             for u in (client.uploads(gid, None) if free else [])]
    user = game.get("user") or {}
    return {
        "game_id": gid, "url": game.get("url"), "title": game.get("title"),
        "author": user_name(user), "author_url": user.get("url") or "", "cover_url": game.get("cover_url") or "", "classification": game.get("classification") or "",
        "short_text": game.get("short_text") or "", "price": game.get("price_label") or "", "free": free,
        "files": files,
    }


def add_free(cfg, url: str) -> dict:
    """Import a free itch.io asset into Purchases (files, tags, license) and match it to the library.
    Returns the item id and the ids of its files still to download."""
    db.init(cfg)
    client = client_from_secrets()
    gid, game = _resolve(client, url)
    conn = db.connect(cfg)
    try:
        row = conn.execute("SELECT * FROM store_items WHERE store='itch' AND store_id=?", (gid,)).fetchone()
        if row is None:
            if not is_free(game):
                raise ItchError("this asset isn't free; buy or claim it on itch.io and it will arrive with the next sync")
            user = game.get("user") or {}
            with conn:
                conn.execute(
                    "INSERT INTO store_items(store, store_id, key_id, title, author, author_url, url, cover_url, "
                    "classification, purchased_at, first_seen) VALUES ('itch',?,NULL,?,?,?,?,?,?,?,?)",
                    (gid, game.get("title") or "?", user_name(user), user.get("url") or "", game.get("url") or "",
                     game.get("cover_url") or "", game.get("classification") or "",
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), time.time()))
        else:
            with conn:
                conn.execute("UPDATE store_items SET hidden=0 WHERE id=?", (row["id"],))  # adding it again unhides it
        item = conn.execute("SELECT * FROM store_items WHERE store='itch' AND store_id=?", (gid,)).fetchone()
        err = _refresh_item(cfg, client, item, time.time(), force=True)
        if err:
            raise ItchError(err)
        match(cfg, conn)
        pending = [r["id"] for r in pending_files(conn, [item["id"]])]
        return {"item_id": item["id"], "title": item["title"], "pending_file_ids": pending}
    finally:
        conn.close()


def _cell_text(cell: str, cls: str) -> str:
    m = re.search(rf'class="[^"]*\b{cls}\b[^"]*"[^>]*>(.*?)</(?:a|div)>', cell, re.S)
    return _text(m.group(1)) if m else ""


def search(query: str) -> list[dict]:
    """Search itch.io game assets. itch.io has no search API, so this reads its public search page;
    if itch changes that page, search breaks but adding by URL keeps working."""
    query = (query or "").strip()
    if not query:
        return []
    return parse_cells(fetch_public(SEARCH_URL + urllib.parse.quote_plus(query)))


def tag_slug(text: str) -> str:
    """'Pixel Art' -> 'pixel-art', the form itch.io uses in tag page links."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def browse_free_tag(tag: str, page: int = 1) -> dict:
    """One page (36 assets) of itch.io's free game assets with this tag, most popular first. Unlike search,
    these pages go on: ask for the next page until `more` is false."""
    slug = tag_slug(tag)
    if not slug:
        return {"tag": "", "results": [], "more": False}
    data = json.loads(fetch_public(f"{FREE_TAG_URL}{slug}?page={max(1, page)}&format=json"))
    results = parse_cells(data.get("content") or "")
    return {"tag": slug, "results": results, "more": len(results) >= 36}


def parse_cells(body: str) -> list[dict]:
    """The asset cells of an itch.io search or browse page."""
    out = []
    for cell in re.split(r'(?=<div[^>]+class="game_cell\b)', body)[1:]:
        gid = re.search(r'data-game_id="(\d+)"', cell)
        link = re.search(r'class="[^"]*\btitle game_link\b[^"]*"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*class="[^"]*\btitle game_link\b', cell)
        if not gid or not link:
            continue
        author = re.search(r'class="game_author"><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', cell, re.S)
        cover = re.search(r'data-lazy_src="([^"]+)"', cell)
        price = _cell_text(cell, "price_value")
        out.append({
            "game_id": gid.group(1), "url": html.unescape(link.group(1) or link.group(2)),
            "title": _cell_text(cell, "title"), "short_text": _cell_text(cell, "game_text"),
            "author": _text(author.group(2)) if author else "", "author_url": author.group(1) if author else "",
            "cover_url": html.unescape(cover.group(1)) if cover else "",
            "price": price,
            "free": not price,  # no price tag: free or pay-what-you-want, both addable without paying
        })
    return out


def download(cfg, store_file_id: int, dest_root: str, progress=None, session: dict | None = None) -> str:
    """Download one purchased file into <dest_root>/<item title>/<filename>. Returns the path."""
    conn = db.connect(cfg)
    row = conn.execute("SELECT sf.*, si.title, si.key_id FROM store_files sf JOIN store_items si "
                       "ON si.id=sf.item_id WHERE sf.id=?", (store_file_id,)).fetchone()
    conn.close()
    if row is None:
        raise ItchError("unknown file")
    session = {} if session is None else session
    if "itch" not in session:  # one client per download batch
        session["itch"] = client_from_secrets()
    client = session["itch"]
    url = client.download_url(row["upload_id"], row["key_id"])
    folder = os.path.join(dest_root, safe_name(row["title"]))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, safe_name(row["filename"]))
    fetch_to(url, target, progress, md5=row["md5"], size_hint=row["size"])  # itch.io's MD5s are kept current
    return target
