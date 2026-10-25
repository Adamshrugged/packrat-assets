"""itch.io integration: import purchases, their files and page metadata; match them to the library.

Uses the official API (https://api.itch.io) with the personal API key (Settings, stored in secrets.toml),
sent as a header so it never appears in URLs or error messages. Public asset pages are
fetched without credentials for tags/license.
"""
import hashlib
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import db, net, secrets
from .net import UA
from .scanner import PACK_ARCHIVES, norm_name

log = logging.getLogger("packrat.itch")

API = "https://api.itch.io"
UPLOADS_MAX_AGE = 7 * 86400
META_MAX_AGE = 30 * 86400


class ItchError(Exception):
    pass


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

    def page_meta(self, url: str) -> dict:
        return parse_page(self.get(url, auth=False, raw=True).decode("utf-8", "replace"))

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
    values = secrets.load()
    key = values.get("itch_api_key")
    if not key:
        raise ItchError("connect itch.io in Settings first")
    return Client(key, values)


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
                 user.get("display_name") or user.get("username") or "", user.get("url") or "",
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
                conn.execute(f"DELETE FROM store_files WHERE item_id=? AND upload_id NOT IN "
                             f"({','.join('?' * len(keep)) or 'NULL'})", (item["id"], *keep))
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
                meta = client.page_meta(item["url"])
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
    stats = match(cfg, conn)
    conn.close()
    return {"purchases": len(items), "errors": len(errors), "error_samples": errors[:5], **stats}


def migrate(conn) -> None:
    """One-time fixes for data imported by earlier versions."""
    if db.get_kv(conn, "itch_meta_v2") is None:
        # parse_page missed the info table after itch.io changed its markup, so tags and licenses were
        # never stored; mark page info stale so the next sync fetches it again
        with conn:
            conn.execute("UPDATE store_items SET meta_fetched_at=NULL WHERE store='itch'")
        db.set_kv(conn, "itch_meta_v2", time.time())


# ------------------------------------------------------------------ free assets (added by URL or search)

# Free downloads don't create a download key, so they never show up in owned-keys. They are stored as itch
# items with key_id NULL; their uploads and download links work without one. If the asset is later claimed
# or bought, the sync fills in the key on the same row (store_id is the game id either way).

PAGE_RE = re.compile(r"^(?:https?://)?([a-z0-9][a-z0-9-]*)\.itch\.io/([a-z0-9][a-z0-9_-]*)/?(?:[?#].*)?$", re.I)
SEARCH_URL = "https://itch.io/search?type=games&classification=assets&q="


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
        "author": user.get("display_name") or user.get("username") or "", "author_url": user.get("url") or "",
        "cover_url": game.get("cover_url") or "", "classification": game.get("classification") or "",
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
                    "classification, purchased_at, first_seen, info) VALUES ('itch',?,NULL,?,?,?,?,?,?,?,?,?)",
                    (gid, game.get("title") or "?", user.get("display_name") or user.get("username") or "",
                     user.get("url") or "", game.get("url") or "", game.get("cover_url") or "",
                     game.get("classification") or "", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     time.time(), json.dumps({"Source": "Free, added from the catalog"})))
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
    req = urllib.request.Request(SEARCH_URL + urllib.parse.quote_plus(query), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ItchError(f"itch.io search failed: {e}") from None
    out = []
    for cell in re.split(r'(?=<div[^>]+class="game_cell\b)', body)[1:]:
        gid = re.search(r'data-game_id="(\d+)"', cell)
        link = re.search(r'class="[^"]*\btitle game_link\b[^"]*"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*class="[^"]*\btitle game_link\b', cell)
        if not gid or not link:
            continue
        author = re.search(r'class="game_author"><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', cell, re.S)
        cover = re.search(r'data-lazy_src="([^"]+)"', cell)
        out.append({
            "game_id": gid.group(1), "url": html.unescape(link.group(1) or link.group(2)),
            "title": _cell_text(cell, "title"), "short_text": _cell_text(cell, "game_text"),
            "author": _text(author.group(2)) if author else "", "author_url": author.group(1) if author else "",
            "cover_url": html.unescape(cover.group(1)) if cover else "",
            "price": _cell_text(cell, "price_value"),  # empty for free (and pay-what-you-want) assets
        })
    return out


# ------------------------------------------------------------------ matching

def match(cfg, conn=None) -> dict:
    """Link each purchased file to the library: exact name+size, then unique size, then pack name."""
    own = conn is None
    conn = conn or db.connect(cfg)
    by_name_size, by_size = {}, defaultdict(list)
    for r in conn.execute("SELECT f.id, f.pack_id, f.name, f.size, f.ext FROM files f JOIN packs p ON p.id=f.pack_id "
                          "WHERE f.member IS NULL AND p.missing=0"):
        by_name_size[(r["name"].lower(), r["size"])] = (r["id"], r["pack_id"])
        if r["ext"] in PACK_ARCHIVES and (r["size"] or 0) >= 256 * 1024:
            by_size[r["size"]].append((r["id"], r["pack_id"]))
    packs_by_norm = defaultdict(list)
    for r in conn.execute("SELECT id, norm_name, kind, path, size FROM packs WHERE missing=0"):
        packs_by_norm[r["norm_name"]].append(r["id"])
        if r["kind"] == "archive":  # an archive pack *is* the downloaded file
            by_name_size[(os.path.basename(r["path"]).lower(), r["size"])] = (None, r["id"])
            if (r["size"] or 0) >= 256 * 1024:
                by_size[r["size"]].append((None, r["id"]))

    counts = defaultdict(int)
    rows = conn.execute("SELECT sf.id, sf.filename, sf.size, si.title, "
                        "(SELECT count(*) FROM store_files x WHERE x.item_id=sf.item_id) AS n "
                        "FROM store_files sf JOIN store_items si ON si.id=sf.item_id").fetchall()
    with conn:
        for r in rows:
            fid = pid = kind = None
            stem_norm = norm_name(r["filename"])
            if (r["filename"].lower(), r["size"]) in by_name_size:
                fid, pid = by_name_size[(r["filename"].lower(), r["size"])]
                kind = "exact"
            elif r["size"] and len(by_size.get(r["size"], [])) == 1:
                fid, pid = by_size[r["size"]][0]
                kind = "size"
            elif stem_norm and packs_by_norm.get(stem_norm):
                pid, kind = packs_by_norm[stem_norm][0], "name"
            elif r["n"] == 1 and packs_by_norm.get(norm_name(r["title"])):
                pid, kind = packs_by_norm[norm_name(r["title"])][0], "title"
            counts[kind or "missing"] += 1
            conn.execute("UPDATE store_files SET match_file_id=?, match_pack_id=?, match_kind=? WHERE id=?",
                         (fid, pid, kind, r["id"]))
        db.refresh_pack_fts(conn)
    if own:
        conn.close()
    return {"files_" + k: v for k, v in counts.items()}


# ------------------------------------------------------------------ downloading

# Extras shipped next to the actual assets (engine packages, installers, docs). They are listed but
# don't count toward "downloaded" and aren't fetched by bulk downloads.
SIDE_FILES = re.compile(r"\.(txt|pdf|md|url|yymps|yyz|exe|dmg|apk|html?)$|license|readme|\bhtml\b|demo", re.I)


def is_pending(category: str, matched: bool, saved: bool, filename: str, wanted: bool = True) -> bool:
    """The one rule for "this purchased file still needs fetching", used by downloads, the Purchases list
    and its buttons: not yet found in the asset library, and not an extra (readme, installer…)."""
    if not wanted:
        return False
    return not matched and not SIDE_FILES.search(filename)


def pending_files(conn, item_ids: list[int] | None = None, new_only: bool = False) -> list:
    """itch.io files of the given purchases that still need downloading (see is_pending)."""
    where, args = ["si.hidden = 0", "si.store = 'itch'"], []  # only itch downloads
    if item_ids is not None:
        where.append(f"si.id IN ({','.join('?' * len(item_ids)) or 'NULL'})")
        args.extend(item_ids)
    if new_only:
        where.append("si.first_seen IS NOT NULL")
    rows = conn.execute(f"SELECT sf.*, si.title FROM store_files sf JOIN store_items si ON si.id = sf.item_id "
                        f"WHERE {' AND '.join(where)} ORDER BY si.purchased_at, sf.filename", args).fetchall()
    return [r for r in rows if is_pending("asset", r["match_pack_id"] is not None, False, r["filename"])]


def download_many(cfg, file_ids: list[int], progress=None) -> dict:
    if not cfg.itch_download_dir:
        raise ItchError("choose a download folder in Settings first")
    require_dir(cfg.itch_download_dir)
    conn = db.connect(cfg)
    rows = {r["id"]: r for r in conn.execute(f"SELECT id, filename, match_pack_id FROM store_files WHERE id IN "
                                             f"({','.join('?' * len(file_ids)) or 'NULL'})", file_ids)}
    conn.close()
    # a queued download may find some files already fetched by the one before it
    file_ids = [f for f in file_ids if f in rows and rows[f]["match_pack_id"] is None]
    done, failed = [], []
    for i, fid in enumerate(file_ids):
        name = rows[fid]["filename"]

        def step(got, size, i=i, name=name):
            if progress:
                pct = f" {got * 100 // size}%" if size else ""
                progress(i, len(file_ids), f"Downloading {name}{pct}")
        step(0, 0)
        try:
            done.append(download(cfg, fid, cfg.itch_download_dir, step))
        except Exception as e:
            failed.append(f"{name}: {e}")
            log.warning("download failed: %s: %s", name, e)
    if done:
        if progress:
            progress(len(file_ids), len(file_ids), "Indexing new downloads…")
        from .scanner import scan_paths
        scan_paths(cfg, done)  # index just the new packs; also re-matches purchases
    return {"downloaded": len(done), "failed": len(failed), "errors": failed[:10]}


def sync_and_auto_download(cfg, progress=None) -> dict:
    result = sync(cfg, progress)
    if cfg.itch_auto_download:
        conn = db.connect(cfg)
        new = [r["id"] for r in pending_files(conn, new_only=True)]
        conn.close()
        if new:
            result["auto_download"] = download_many(cfg, new, progress)
    return result


def require_dir(root: str, error=None) -> None:
    error = error or ItchError
    if not os.path.isdir(root):
        raise error(f"download folder not reachable: {root}")


def fetch_to(url: str, target: str, progress=None, md5: str | None = None, size_hint: int | None = None,
             error=None) -> int:
    """Stream a signed download link (no credentials sent) to target via a .part file. It's kept only if
    complete (the server's Content-Length) and, when an MD5 is given, matching it. Returns the size."""
    error = error or ItchError
    tmp = target + ".part"
    got = 0
    digest = hashlib.md5() if md5 else None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
            expected = int(r.headers.get("Content-Length") or 0) or None
            while chunk := r.read(1 << 20):
                fh.write(chunk)
                if digest:
                    digest.update(chunk)
                got += len(chunk)
                if progress:
                    progress(got, expected or size_hint or 0)
        if expected and got != expected:
            raise error(f"incomplete download ({got} of {expected} bytes)")
        if digest and digest.hexdigest() != md5:
            raise error("checksum mismatch; download discarded")
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)  # never leave half-written .part files behind
        raise
    os.replace(tmp, target)
    return got


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:120] or "untitled"


def download(cfg, store_file_id: int, dest_root: str, progress=None) -> str:
    """Download one purchased file into <dest_root>/<item title>/<filename>. Returns the path."""
    conn = db.connect(cfg)
    row = conn.execute("SELECT sf.*, si.title, si.key_id FROM store_files sf JOIN store_items si "
                       "ON si.id=sf.item_id WHERE sf.id=?", (store_file_id,)).fetchone()
    conn.close()
    if row is None:
        raise ItchError("unknown file")
    client = client_from_secrets()
    url = client.download_url(row["upload_id"], row["key_id"])
    folder = os.path.join(dest_root, safe_name(row["title"]))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, safe_name(row["filename"]))
    fetch_to(url, target, progress, md5=row["md5"], size_hint=row["size"])  # itch.io's MD5s are kept current
    return target
