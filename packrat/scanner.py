"""Walk the asset roots (read-only) and record packs and files in the database."""
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import db
from .archives import list_members
from .config import inside
from .kinds import KINDS, LISTABLE, ext_of, is_junk, kind_of

log = logging.getLogger("packrat.scan")

PACK_ARCHIVES = LISTABLE | {".unitypackage"}

AUTO_TAGS = {
    "tileset": {"tileset", "tilesets", "tiles", "tile", "interiors", "exteriors", "town", "village", "dungeon"},
    "characters": {"character", "characters", "avatar", "avatars", "npc", "npcs", "hero", "heroes", "sprites", "bosses"},
    "ui": {"ui", "gui", "interface", "hud", "button", "buttons"},
    "icons": {"icon", "icons"},
    "sfx": {"sfx", "sound", "sounds", "foley", "ambiences", "ambience"},
    "music": {"music", "track", "tracks", "ost", "soundtrack"},
    "vfx": {"vfx", "particles", "effects"},
    "font": {"font", "fonts"},
    "pixel": {"pixel", "8bit", "16bit", "16x16", "32x32", "48x48", "retro", "lpc"},
    "rpg": {"rpg"},
    "modern": {"modern", "office", "phone", "smartphones"},
    "fantasy": {"fantasy", "medieval", "castle", "dungeon", "dreamland"},
}


@dataclass
class PackSpec:
    kind: str  # folder | archive | loose
    path: str
    name: str
    root: str
    grp: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{os.path.normcase(self.path)}"


def norm_name(name: str) -> str:
    n = os.path.splitext(name)[0] if ext_of(name) in PACK_ARCHIVES else name
    n = n.lower()
    n = re.sub(r"\(\d+\)|_windows|_v\d+(\.\d+)*|v\d+(\.\d+)+|-win\b|\bfree\b", "", n)
    return re.sub(r"[^a-z0-9]+", "", n)


# ---------------------------------------------------------------- discovery

def discover(cfg):
    for root in cfg.roots:
        if not os.path.isdir(root.path):
            if not root.auto:  # the default download folder only exists after the first download
                log.warning("root not reachable, skipping: %s", root.path)
            continue
        base = os.path.normcase(root.path)
        transparent = set()
        containers = list(root.containers)
        if cfg.itch_download_dir and inside(cfg.itch_download_dir, root.path):
            # The download folder holds one folder per purchase: each is a pack, not the folder as a whole.
            containers.append(os.path.relpath(cfg.itch_download_dir, root.path))
        for c in containers:
            p = os.path.normcase(os.path.normpath(os.path.join(root.path, c)))
            while len(p) > len(base):
                transparent.add(p)
                p = os.path.dirname(p)
        yield from _visit(root, root.path, "", transparent)


def _auto_container(path: str) -> bool:
    try:
        entries = [e for e in os.scandir(path) if not is_junk(e.name)]
    except OSError:
        return False
    if any(e.is_dir() for e in entries):
        return False
    archives = sum(1 for e in entries if ext_of(e.name) in PACK_ARCHIVES)
    return archives >= 3 and archives >= 0.8 * len(entries)


def _visit(root, dirpath: str, grp: str, transparent: set[str]):
    loose = False
    for e in sorted(os.scandir(dirpath), key=lambda e: e.name.lower()):
        if is_junk(e.name):
            continue
        if e.is_dir():
            sub = f"{grp}/{e.name}" if grp else e.name
            if os.path.normcase(e.path) in transparent or _auto_container(e.path):
                yield from _visit(root, e.path, sub, transparent)
            else:
                yield PackSpec("folder", e.path, e.name, root.label, grp)
        elif ext_of(e.name) in PACK_ARCHIVES:
            yield PackSpec("archive", e.path, os.path.splitext(e.name)[0], root.label, grp)
        else:
            loose = True
    if loose:
        label = grp or os.path.basename(dirpath)
        yield PackSpec("loose", dirpath, f"{label} (loose files)", root.label, grp)


# ---------------------------------------------------------------- collection

def _archive_listing(conn, cfg, path: str, size: int, mtime: float):
    row = conn.execute("SELECT size, mtime, listing FROM archive_cache WHERE path=?", (path,)).fetchone()
    if row and row["size"] == size and row["mtime"] == mtime:
        return json.loads(row["listing"])
    try:
        listing = list_members(path, cfg.sevenzip)
    except Exception as e:
        log.warning("cannot list %s: %s", path, e)
        return None  # don't cache failures; may be transient (network drive hiccup, 7z missing)
    with conn:
        conn.execute("INSERT OR REPLACE INTO archive_cache(path, size, mtime, listing) VALUES (?,?,?,?)",
                     (path, size, mtime, json.dumps(listing)))
    return listing


def _member_rows(conn, cfg, path, size, mtime, base_rel):
    listing = _archive_listing(conn, cfg, path, size, mtime) or []
    for member, msize, mmtime in listing:
        rel = f"{base_rel}/{member}" if base_rel else member
        yield (path, member, rel, os.path.basename(member), ext_of(member), kind_of(member), msize, mmtime)


def collect(conn, cfg, spec: PackSpec) -> list[tuple]:
    """Rows of (path, member, rel, name, ext, kind, size, mtime)."""
    rows = []
    if spec.kind == "archive":
        st = os.stat(spec.path)
        if ext_of(spec.path) in LISTABLE:
            rows.extend(_member_rows(conn, cfg, spec.path, st.st_size, st.st_mtime, ""))
        if not rows:  # unlistable (e.g. .unitypackage) or failed: keep the archive itself
            name = os.path.basename(spec.path)
            rows.append((spec.path, None, name, name, ext_of(name), "archive", st.st_size, st.st_mtime))
        return rows

    stack = [(spec.path, "")]
    while stack:
        dirpath, rel_dir = stack.pop()
        try:
            entries = list(os.scandir(dirpath))
        except OSError as e:
            log.warning("cannot read %s: %s", dirpath, e)
            continue
        for e in entries:
            if is_junk(e.name):
                continue
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if e.is_dir():
                if spec.kind == "folder":
                    stack.append((e.path, rel))
                continue
            if spec.kind == "loose" and ext_of(e.name) in PACK_ARCHIVES:
                continue  # those are packs of their own
            st = e.stat()
            ext = ext_of(e.name)
            rows.append((e.path, None, rel, e.name, ext, kind_of(e.name), st.st_size, st.st_mtime))
            if ext in LISTABLE:
                rows.extend(_member_rows(conn, cfg, e.path, st.st_size, st.st_mtime, rel))
    return rows


# ---------------------------------------------------------------- summary

COVER_WORDS = re.compile(r"preview|cover|promo|screenshot|showcase|thumbnail|banner|title|example|sample|demo|mockup")
COVER_EXTS = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp"}


def pick_cover(images) -> int | None:
    best, best_score = None, -math.inf
    for r in images:
        if r["ext"] not in COVER_EXTS or not r["size"]:
            continue
        rel = r["rel"].lower()
        score = math.log2(r["size"]) - 1.5 * rel.count("/")
        if COVER_WORDS.search(os.path.basename(rel)):
            score += 40
        if score > best_score:
            best, best_score = r["id"], score
    return best


def auto_tags(spec: PackSpec, counts: dict[str, int]) -> str:
    words = set(re.findall(r"[a-z0-9]+", f"{spec.name} {spec.grp}".lower()))
    tags = {tag for tag, keys in AUTO_TAGS.items() if words & keys}
    if counts.get("audio", 0) > counts.get("image", 0):
        tags.add("audio")
    elif counts.get("image", 0):
        tags.add("art")
    if counts.get("font", 0) and not counts.get("image", 0) and not counts.get("audio", 0):
        tags.add("font")
    return ",".join(sorted(tags))


def gather(conn, cfg, spec: PackSpec):
    """The slow, read-only part of indexing a pack: stat it and list its files (network-bound on a network drive)."""
    return os.stat(spec.path), collect(conn, cfg, spec)


def scan_pack(conn, cfg, spec: PackSpec, now: float) -> None:
    store_pack(conn, spec, *gather(conn, cfg, spec), now)


def store_pack(conn, spec: PackSpec, st, rows: list[tuple], now: float) -> None:
    """Write one pack's gathered files to the index (fast; done on a single connection)."""
    with conn:
        conn.execute(
            "INSERT INTO packs(key, path, kind, name, norm_name, root, grp, mtime, first_seen, last_scanned, missing) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET path=excluded.path, "
            "name=excluded.name, norm_name=excluded.norm_name, root=excluded.root, grp=excluded.grp, "
            "mtime=excluded.mtime, last_scanned=excluded.last_scanned, missing=0",
            (spec.key, spec.path, spec.kind, spec.name, norm_name(spec.name), spec.root, spec.grp,
             st.st_mtime, now, now),
        )
        pid = conn.execute("SELECT id FROM packs WHERE key=?", (spec.key,)).fetchone()[0]
        conn.execute("DELETE FROM files_fts WHERE rowid IN (SELECT id FROM files WHERE pack_id=?)", (pid,))
        conn.execute("DELETE FROM files WHERE pack_id=?", (pid,))
        conn.executemany(
            "INSERT INTO files(pack_id, path, member, rel, dir, name, ext, kind, size, mtime) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(pid, p, m, rel, rel.rpartition("/")[0], n, e, k, s, t) for p, m, rel, n, e, k, s, t in rows],
        )
        conn.execute("INSERT INTO files_fts(rowid, rel) SELECT id, rel FROM files WHERE pack_id=?", (pid,))

        counts = {k: 0 for k in KINDS}
        for r in rows:
            counts[r[5]] = counts.get(r[5], 0) + 1
        disk_size = st.st_size if spec.kind == "archive" else sum(r[6] or 0 for r in rows if r[1] is None)
        # Audio packs usually only contain a promo/"thank you" image; an audio icon reads better.
        images = [] if counts["audio"] > counts["image"] else conn.execute(
            "SELECT id, rel, ext, size FROM files WHERE pack_id=? AND kind='image'", (pid,)).fetchall()
        conn.execute(
            "UPDATE packs SET size=?, file_count=?, image_count=?, audio_count=?, font_count=?, source_count=?, "
            "archive_count=?, doc_count=?, cover_file_id=?, auto_tags=? WHERE id=?",
            (disk_size, len(rows), counts["image"], counts["audio"], counts["font"], counts["source"],
             counts["archive"], counts["doc"], pick_cover(images), auto_tags(spec, counts), pid),
        )


def scan_paths(cfg, paths: list[str]) -> dict:
    """Index just the packs containing `paths` (e.g. files a download just wrote), then re-link purchases.
    Much faster than scan(): it doesn't walk or rewrite the rest of the library, and never flags packs
    as missing, so the full Rescan is still what picks up changes made to the folders directly."""
    db.init(cfg)
    conn = db.connect(cfg)
    now = time.time()
    wanted = [os.path.normcase(os.path.normpath(p)) for p in paths]

    def contains(spec) -> bool:
        base = os.path.normcase(os.path.normpath(spec.path))
        return any(p == base or p.startswith(base + os.sep) for p in wanted)

    specs = [s for s in discover(cfg) if contains(s)]
    errors = 0
    for spec in specs:
        try:
            scan_pack(conn, cfg, spec, now)
        except Exception as e:
            errors += 1
            log.exception("failed to scan %s: %s", spec.path, e)
    if len(specs) < len(paths):
        log.info("indexed %d packs for %d new files; files outside the library roots wait for a Rescan",
                 len(specs), len(paths))
    from .itch import match
    match(cfg, conn)
    from .thumbs import cover_rows, make_thumbs
    make_thumbs(cfg, cover_rows(conn, [s.key for s in specs]))
    conn.close()
    return {"packs": len(specs), "errors": errors, "seconds": round(time.time() - now, 1)}


SCAN_THREADS = 8  # walking packs over SMB is mostly waiting on round trips; several at once hide them


def scan(cfg, progress=None) -> dict:
    db.init(cfg)
    conn = db.connect(cfg)
    now = time.time()
    specs = list(discover(cfg))
    reachable = {r.label for r in cfg.roots if os.path.isdir(r.path)}
    errors = 0

    # Packs are walked in parallel (each worker has its own connection for the archive-listing cache);
    # the index itself is written here, one pack at a time, in discovery order.
    local, worker_conns = threading.local(), []

    def work(spec):
        if not hasattr(local, "conn"):
            local.conn = db.connect(cfg)
            worker_conns.append(local.conn)
        try:
            return gather(local.conn, cfg, spec), None
        except Exception as e:
            return None, e

    with ThreadPoolExecutor(SCAN_THREADS) as pool:
        for i, (spec, (gathered, err)) in enumerate(zip(specs, pool.map(work, specs))):
            if progress:
                progress(i, len(specs), spec.name)
            try:
                if err:
                    raise err
                store_pack(conn, spec, *gathered, now)
            except Exception as e:
                errors += 1
                log.exception("failed to scan %s: %s", spec.path, e)
    for c in worker_conns:
        c.close()
    with conn:
        # Packs that vanished are flagged, never deleted, so their notes/tags survive a move or a drive going
        # offline. That includes packs of library folders removed in Settings; an unreachable folder's are kept.
        for label in reachable:
            conn.execute("UPDATE packs SET missing=1 WHERE root=? AND last_scanned < ?", (label, now))
        labels = [r.label for r in cfg.roots]
        conn.execute(f"UPDATE packs SET missing=1 WHERE root NOT IN ({db.placeholders(labels)})", labels)
    # Re-link store purchases to the (possibly changed) library; also refreshes pack search.
    from .itch import match
    match(cfg, conn)
    from .thumbs import cover_rows, make_thumbs
    covers = make_thumbs(cfg, cover_rows(conn), progress, "making pack covers")  # the library grid never waits
    if progress:
        progress(len(specs), len(specs), "")
    totals = conn.execute("SELECT count(*), coalesce(sum(file_count),0) FROM packs WHERE missing=0").fetchone()
    conn.close()
    return {"packs": totals[0], "files": totals[1], "errors": errors, "new_covers": covers,
            "seconds": round(time.time() - now, 1)}
