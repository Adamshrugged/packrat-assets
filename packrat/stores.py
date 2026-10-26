"""What all stores share: matching purchases to the library, deciding what still needs downloading, downloading
into each store's folder, and syncing every connected store. The stores themselves are itch.py and humble.py."""
import hashlib
import json
import logging
import os
import re
import sys
import urllib.request
from collections import defaultdict
from contextlib import closing

from . import db, secrets
from .net import UA
from .scanner import PACK_ARCHIVES, norm_name

log = logging.getLogger("packrat.stores")

NAMES = {"itch": "itch.io", "humble": "Humble"}


class StoreError(Exception):
    """A store couldn't do what was asked (network, sign-in, a missing file...). The message is for people."""


class DownloadCancelled(Exception):
    """Raised from a progress callback to stop a download batch."""


def module(store: str):
    from . import humble, itch
    return {"itch": itch, "humble": humble}[store]


def connected() -> dict[str, bool]:
    values = secrets.load()
    return {"itch": bool(values.get("itch_api_key")), "humble": bool(values.get("humble_session_cookie"))}


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
        # Anything found in the library is a game asset, even a Humble product that didn't look like one.
        conn.execute("UPDATE store_items SET asset=1 WHERE asset=0 AND id IN "
                     "(SELECT item_id FROM store_files WHERE match_pack_id IS NOT NULL)")
        db.refresh_pack_fts(conn)
    if own:
        conn.close()
    return {"files_" + k: v for k, v in counts.items()}


# ------------------------------------------------------------------ what still needs downloading

# Extras shipped next to the actual assets (engine packages, installers, docs). They are listed but
# don't count toward "downloaded" and aren't fetched by bulk downloads.
SIDE_FILES = re.compile(r"\.(txt|pdf|md|url|yymps|yyz|exe|dmg|apk|html?)$|license|readme|\bhtml\b|demo", re.I)

# Humble offers some products as one build per platform; only this computer's is wanted.
PLATFORM_BUILDS = {"windows", "mac", "linux", "android", "asmjs"}
THIS_PLATFORM = "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"


def is_pending(matched: bool, filename: str) -> bool:
    """The one rule for "this purchased file still needs fetching", used by downloads, the Purchases list
    and its buttons: not yet found in the asset library, and not an extra (readme, installer…)."""
    return not matched and not SIDE_FILES.search(filename)


def unwanted_builds(files: list) -> set[int]:
    """Ids of platform builds not worth downloading: other platforms' builds, when this platform's is offered
    (otherwise every build is kept). files: rows with "id" and "type" (the platform)."""
    builds = [f for f in files if f["type"] in PLATFORM_BUILDS]
    if not any(f["type"] == THIS_PLATFORM for f in builds):
        return set()
    return {f["id"] for f in builds if f["type"] != THIS_PLATFORM}


def pending_files(conn, item_ids: list[int] | None = None, new_only: bool = False) -> list:
    """Files of the given (or all listed) purchases that still need downloading (see is_pending)."""
    where, args = ["si.hidden = 0", "si.asset = 1"], []
    if item_ids is not None:
        where.append(f"si.id IN ({db.placeholders(item_ids)})")
        args.extend(item_ids)
    if new_only:
        where.append("si.first_seen IS NOT NULL")
    rows = conn.execute(f"SELECT sf.*, si.title, si.store FROM store_files sf JOIN store_items si ON si.id = sf.item_id "
                        f"WHERE {' AND '.join(where)} ORDER BY si.purchased_at, sf.filename", args).fetchall()
    by_item = defaultdict(list)
    for r in rows:
        by_item[r["item_id"]].append(r)
    skip = set().union(*(unwanted_builds(files) for files in by_item.values()))
    return [r for r in rows if r["id"] not in skip and is_pending(r["match_pack_id"] is not None, r["filename"])]


# ------------------------------------------------------------------ downloading

def download_many(cfg, file_ids: list[int], progress=None) -> dict:
    """Download purchased files into their store's download folder (one folder per purchase), then index them.
    A progress callback may raise DownloadCancelled: finished files are kept and indexed."""
    with closing(db.connect(cfg)) as conn:
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT sf.*, si.title, si.key_id, si.store FROM store_files sf JOIN store_items si "
            f"ON si.id = sf.item_id WHERE sf.id IN ({db.placeholders(file_ids)})", file_ids)}
    # a queued download may find some files already fetched by the one before it
    file_ids = [f for f in file_ids if f in rows and rows[f]["match_pack_id"] is None]
    done, failed, cancelled = [], [], False
    session: dict = {}  # per batch: a store's client, order details...
    try:
        for i, fid in enumerate(file_ids):
            name, store = rows[fid]["filename"], rows[fid]["store"]

            def step(got, size, i=i, name=name):
                if progress:
                    pct = f" {got * 100 // size}%" if size else ""
                    progress(i, len(file_ids), f"Downloading {name}{pct}")
            step(0, 0)
            try:
                try:
                    folder = cfg.ensure_download_dir(store)
                except FileNotFoundError as e:
                    raise StoreError(str(e)) from None
                done.append(_download(cfg, rows[fid], folder, step, session))
            except DownloadCancelled:
                raise
            except Exception as e:
                failed.append(f"{name}: {e}")
                log.warning("download failed: %s: %s", name, e)
    except DownloadCancelled:
        cancelled = True  # the file in progress is discarded; finished ones are kept and indexed below
    if done:
        if progress and not cancelled:
            progress(len(file_ids), len(file_ids), "Indexing new downloads…")
        from .scanner import scan_paths
        scan_paths(cfg, done)  # index just the new packs; also re-matches purchases
    return {"downloaded": len(done), "failed": len(failed), "errors": failed[:10], "cancelled": cancelled}


def _download(cfg, row, dest_root: str, progress, session: dict) -> str:
    """Download one purchased file into <dest_root>/<purchase title>/<filename>. Returns the path."""
    url, md5 = module(row["store"]).download_link(row, session)
    folder = os.path.join(dest_root, safe_name(row["title"]))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, safe_name(row["filename"]))
    got = fetch_to(url, target, progress, md5=md5, size_hint=row["size"])
    if got != row["size"]:  # the store's listed size was out of date; matching needs the real one
        with closing(db.connect(cfg)) as c, c:
            c.execute("UPDATE store_files SET size=? WHERE id=?", (got, row["id"]))
    return target


def fetch_to(url: str, target: str, progress=None, md5: str | None = None, size_hint: int | None = None) -> int:
    """Stream a signed download link (no credentials sent) to target via a .part file. It's kept only if
    complete (the server's Content-Length) and, when an MD5 is given, matching it. Returns the size."""
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
            raise StoreError(f"incomplete download ({got} of {expected} bytes)")
        if digest and digest.hexdigest() != md5:
            raise StoreError("checksum mismatch; download discarded")
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)  # never leave half-written .part files behind
        raise
    os.replace(tmp, target)
    return got


WINDOWS_RESERVED = re.compile(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", re.I)


def safe_name(name: str) -> str:
    """A file or folder name that's valid on Windows and Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:120].rstrip(" .")
    if WINDOWS_RESERVED.fullmatch(name):
        name = "_" + name
    return name or "untitled"


# ------------------------------------------------------------------ syncing

def sync_all(cfg, progress=None) -> dict:
    """Sync every connected store, then (if enabled) download what's new. One store failing (an expired
    Humble cookie, say) doesn't stop the others; its error is reported with the rest."""
    stores = [s for s, on in connected().items() if on]
    if not stores:
        raise StoreError("connect itch.io or Humble in Settings first")
    results, errors = {}, []
    for store in stores:
        try:
            results[store] = module(store).sync(cfg, progress)
            errors += [f"{NAMES[store]}: {e}" for e in results[store].get("error_samples", [])]
        except StoreError as e:
            with closing(db.connect(cfg)) as conn:  # listed on the Purchases page with the other sync errors
                db.set_kv(conn, f"{store}_last_errors", json.dumps([f"{NAMES[store]}: {e}"]))
            if len(stores) == 1:
                raise
            errors.append(f"{NAMES[store]}: {e}")
    out = {"stores": results, "errors": sum(r.get("errors", 0) for r in results.values()) + len(stores) - len(results),
           "error_samples": errors[:5]}
    if cfg.auto_download:
        with closing(db.connect(cfg)) as conn:
            new = [r["id"] for r in pending_files(conn, new_only=True)]
        if new:
            out["auto_download"] = download_many(cfg, new, progress)
    return out
