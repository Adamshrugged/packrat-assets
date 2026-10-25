import json
import mimetypes
import os
import re
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .guard import HostGuard, is_loopback
from .archives import read_member
from .kinds import THUMBABLE
from .thumbs import get_thumb

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

for _type, _ext in [("audio/ogg", ".ogg"), ("audio/wav", ".wav"), ("font/ttf", ".ttf"), ("font/otf", ".otf"),
                    ("font/woff", ".woff"), ("font/woff2", ".woff2"), ("image/svg+xml", ".svg"),
                    ("text/plain", ".txt"), ("text/plain", ".url"), ("text/plain", ".md")]:
    mimetypes.add_type(_type, _ext)

PACK_SORTS = {
    "name": "p.name COLLATE NOCASE",
    "size": "p.size DESC",
    "recent": "p.mtime DESC",
    "rating": "p.rating DESC, p.name COLLATE NOCASE",
}
TYPE_FILTERS = {"images": "p.image_count > 0", "audio": "p.audio_count > 0", "fonts": "p.font_count > 0"}
FILE_KINDS = {"image", "audio", "font", "source", "doc", "archive", "other"}


class PackMeta(BaseModel):
    vendor: str | None = None
    license: str | None = None
    tags: list[str] | str | None = None
    notes: str | None = None
    rating: int | None = None
    favorite: bool | None = None


def fts_query(q: str, any_word: bool = False) -> str | None:
    tokens = re.findall(r"[^\W_]+", q.lower())
    return (" OR " if any_word else " ").join(f'"{t}"*' for t in tokens) or None


def split_tags(s: str) -> list[str]:
    return [t for t in (s or "").split(",") if t]


def create_app(cfg) -> FastAPI:
    db.init(cfg)
    app = FastAPI(title="Packrat")
    def conn():
        return db.connect(cfg)

    def pack_out(r) -> dict:
        d = dict(r)
        d["display_path"] = cfg.display_path(d["path"])
        d["tags"] = split_tags(d["tags"])
        d["auto_tags"] = split_tags(d["auto_tags"])
        d["cover"] = f"/api/files/{d['cover_file_id']}/thumb" if d["cover_file_id"] else None
        d["favorite"] = bool(d["favorite"])
        return d

    def file_out(r) -> dict:
        d = {k: r[k] for k in ("id", "pack_id", "name", "rel", "dir", "ext", "kind", "size")}
        d["in_archive"] = r["member"] is not None
        d["display_path"] = cfg.display_path(r["path"], r["member"])
        d["raw"] = f"/api/files/{r['id']}/raw"
        d["thumb"] = (f"/api/files/{r['id']}/thumb" if r["ext"] in THUMBABLE
                      else d["raw"] if r["ext"] == ".svg" else None)
        if "pack_name" in r.keys():
            d["pack_name"] = r["pack_name"]
        return d

    def get_file(c, file_id: int):
        row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "file not found (try reloading)")
        return row

    def is_local(request: Request) -> bool:
        return os.name == "nt" and request.client is not None and request.client.host in ("127.0.0.1", "::1")

    # ------------------------------------------------------------ library

    @app.get("/api/facets")
    def facets(request: Request):
        c = conn()
        roots = [dict(r) for r in c.execute(
            "SELECT root AS name, count(*) AS count FROM packs WHERE missing=0 GROUP BY root ORDER BY root")]
        tag_counts: dict[str, int] = {}
        for r in c.execute("SELECT tags, auto_tags FROM packs WHERE missing=0"):
            for t in set(split_tags(r["tags"]) + split_tags(r["auto_tags"])):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        totals = dict(c.execute(
            "SELECT count(*) AS packs, coalesce(sum(file_count),0) AS files, coalesce(sum(size),0) AS size, "
            "coalesce(sum(image_count),0) AS images, coalesce(sum(audio_count),0) AS audio, "
            "sum(favorite) AS favorites, sum(CASE WHEN image_count>0 THEN 1 ELSE 0 END) AS with_images, "
            "sum(CASE WHEN audio_count>0 THEN 1 ELSE 0 END) AS with_audio, "
            "sum(CASE WHEN font_count>0 THEN 1 ELSE 0 END) AS with_fonts "
            "FROM packs WHERE missing=0").fetchone())
        missing = c.execute("SELECT count(*) FROM packs WHERE missing=1").fetchone()[0]
        return {
            "roots": roots,
            "tags": sorted(({"name": k, "count": v} for k, v in tag_counts.items()),
                           key=lambda t: (-t["count"], t["name"])),
            "totals": totals,
            "missing": missing,
            "can_reveal": is_local(request),
        }

    @app.get("/api/packs")
    def list_packs(root: str = "", type: str = "", tag: str = "", fav: bool = False, sort: str = "name",
                   missing: bool = False):
        where, args = ["p.missing = ?"], [1 if missing else 0]
        if root:
            where.append("p.root = ?")
            args.append(root)
        if type in TYPE_FILTERS:
            where.append(TYPE_FILTERS[type])
        if tag:
            where.append("(',' || p.tags || ',' || p.auto_tags || ',') LIKE ?")
            args.append(f"%,{tag.lower()},%")
        if fav:
            where.append("p.favorite = 1")
        sql = (f"SELECT p.*, (SELECT count(*) FROM packs d WHERE d.norm_name = p.norm_name AND d.id <> p.id "
               f"AND d.missing = 0) AS dupes FROM packs p WHERE {' AND '.join(where)} "
               f"ORDER BY {PACK_SORTS.get(sort, PACK_SORTS['name'])}")
        return [pack_out(r) for r in conn().execute(sql, args)]

    @app.get("/api/packs/{pack_id}")
    def get_pack(pack_id: int):
        c = conn()
        row = c.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "pack not found")
        out = pack_out(row)
        out["duplicates"] = [
            {"id": d["id"], "name": d["name"], "grp": d["grp"], "root": d["root"], "kind": d["kind"],
             "display_path": cfg.display_path(d["path"])}
            for d in c.execute("SELECT * FROM packs WHERE norm_name=? AND id<>? AND missing=0",
                               (row["norm_name"], pack_id))
        ]
        out["other_count"] = c.execute("SELECT count(*) FROM files WHERE pack_id=? AND kind='other'",
                                       (pack_id,)).fetchone()[0]
        out["store"] = load_items(c, "id IN (SELECT item_id FROM store_files WHERE match_pack_id=?)", (pack_id,))
        return out

    @app.patch("/api/packs/{pack_id}")
    def update_pack(pack_id: int, meta: PackMeta):
        sets, args = [], []
        for field in ("vendor", "license", "notes"):
            value = getattr(meta, field)
            if value is not None:
                sets.append(f"{field}=?")
                args.append(value.strip())
        if meta.tags is not None:
            tags = meta.tags if isinstance(meta.tags, list) else meta.tags.split(",")
            clean = sorted({t.strip().lower() for t in tags if t.strip()})
            sets.append("tags=?")
            args.append(",".join(clean))
        if meta.rating is not None:
            sets.append("rating=?")
            args.append(max(0, min(5, meta.rating)))
        if meta.favorite is not None:
            sets.append("favorite=?")
            args.append(int(meta.favorite))
        c = conn()
        if sets:
            with c:
                c.execute(f"UPDATE packs SET {', '.join(sets)} WHERE id=?", (*args, pack_id))
                db.refresh_pack_fts(c, pack_id)
        return get_pack(pack_id)

    @app.get("/api/packs/{pack_id}/files")
    def pack_files(pack_id: int, dir: str = "", kind: str = "", flat: bool = False, offset: int = 0,
                   limit: int = 300, sort: str = "name"):
        c = conn()
        limit = max(1, min(limit, 2000))
        kind_sql, kind_args = ("AND kind=?", [kind]) if kind in FILE_KINDS else ("", [])
        prefix = dir + "/" if dir else ""
        in_dir = "(dir = ? OR substr(dir, 1, ?) = ?)" if dir else "1"
        in_dir_args = [dir, len(prefix), prefix] if dir else []

        dirs: dict[str, int] = {}
        if not flat:
            for r in c.execute(f"SELECT dir, count(*) AS n FROM files WHERE pack_id=? AND {in_dir} "
                               f"{kind_sql} GROUP BY dir", (pack_id, *in_dir_args, *kind_args)):
                if r["dir"] == dir:
                    continue
                child = r["dir"][len(prefix):].split("/", 1)[0]
                dirs[child] = dirs.get(child, 0) + r["n"]
            file_where, file_args = "dir = ?", [dir]
        else:
            file_where, file_args = in_dir, in_dir_args

        base = f"FROM files WHERE pack_id=? AND {file_where} {kind_sql}"
        params = (pack_id, *file_args, *kind_args)
        total = c.execute(f"SELECT count(*) {base}", params).fetchone()[0]
        order = "size DESC" if sort == "size" else "rel COLLATE NOCASE"
        rows = c.execute(f"SELECT * {base} ORDER BY {order} LIMIT ? OFFSET ?",
                         (*params, limit, offset)).fetchall()
        return {
            "dirs": [{"name": k, "path": prefix + k, "count": v}
                     for k, v in sorted(dirs.items(), key=lambda kv: kv[0].lower())],
            "files": [file_out(r) for r in rows],
            "total": total,
        }

    # ------------------------------------------------------------ search

    @app.get("/api/search")
    def search(q: str, kind: str = "", pack_id: int | None = None, offset: int = 0, limit: int = 200):
        result = _search(q, kind, pack_id, offset, limit, any_word=False)
        if not result["total"] and not result["packs"] and len(re.findall(r"[^\W_]+", q)) > 1:
            # Nothing has every word; fall back to any word, best matches first.
            result = _search(q, kind, pack_id, offset, limit, any_word=True)
            result["matched"] = "any word"
        return result

    def _search(q: str, kind: str, pack_id: int | None, offset: int, limit: int, any_word: bool):
        match = fts_query(q, any_word)
        if not match:
            return {"packs": [], "files": [], "total": 0}
        c = conn()
        where, args = ["files_fts MATCH ?", "p.missing = 0"], [match]
        if kind in FILE_KINDS:
            where.append("f.kind = ?")
            args.append(kind)
        if pack_id is not None:
            where.append("f.pack_id = ?")
            args.append(pack_id)
        base = (f"FROM files_fts JOIN files f ON f.id = files_fts.rowid JOIN packs p ON p.id = f.pack_id "
                f"WHERE {' AND '.join(where)}")
        total = c.execute(f"SELECT count(*) {base}", args).fetchone()[0]
        files = c.execute(f"SELECT f.*, p.name AS pack_name {base} ORDER BY rank LIMIT ? OFFSET ?",
                          (*args, max(1, min(limit, 1000)), offset)).fetchall()

        packs = []
        if pack_id is None and offset == 0:
            seen = set()
            for r in c.execute("SELECT p.*, 0 AS dupes FROM packs_fts JOIN packs p ON p.id = packs_fts.rowid "
                               "WHERE packs_fts MATCH ? AND p.missing = 0 ORDER BY rank LIMIT 60", (match,)):
                packs.append({**pack_out(r), "hits": None})
                seen.add(r["id"])
            for r in c.execute(f"SELECT p.*, 0 AS dupes, count(*) AS hits {base} GROUP BY p.id "
                               f"ORDER BY hits DESC LIMIT 60", args):
                if r["id"] not in seen:
                    packs.append({**pack_out(r), "hits": r["hits"]})
        return {"packs": packs, "files": [file_out(r) for r in files], "total": total}

    # ------------------------------------------------------------ file content

    @app.get("/api/files/{file_id}/info")
    def file_info(file_id: int):
        row = conn().execute("SELECT f.*, p.name AS pack_name FROM files f JOIN packs p ON p.id = f.pack_id "
                             "WHERE f.id=?", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "file not found (try reloading)")
        return file_out(row)

    @app.get("/api/files/{file_id}/raw")
    def raw(file_id: int, download: bool = False):
        row = get_file(conn(), file_id)
        media = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
        disposition = "attachment" if download else "inline"
        if row["member"] is None:
            if not os.path.exists(row["path"]):
                raise HTTPException(404, "file is not reachable")
            return FileResponse(row["path"], media_type=media, filename=row["name"],
                                content_disposition_type=disposition)
        try:
            data = read_member(row["path"], row["member"], cfg.sevenzip)
        except Exception as e:
            raise HTTPException(500, f"cannot read from archive: {e}")
        quoted = row["name"].encode("ascii", "replace").decode().replace('"', "")
        return Response(data, media_type=media, headers={
            "Content-Disposition": f'{disposition}; filename="{quoted}"',
            "Cache-Control": "private, max-age=86400",
        })

    @app.get("/api/files/{file_id}/thumb")
    def thumb(file_id: int):
        row = get_file(conn(), file_id)
        path = get_thumb(cfg, row)
        if path is None:
            raise HTTPException(404, "no preview")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=604800"})

    @app.post("/api/reveal")
    def reveal(request: Request, file_id: int | None = None, pack_id: int | None = None):
        """Open Explorer at the item. Only for a browser on the machine running the server."""
        if not is_local(request):
            raise HTTPException(403, "only works on the computer running Packrat")
        c = conn()
        if file_id is not None:
            target, select = get_file(c, file_id)["path"], True
        elif pack_id is not None:
            row = c.execute("SELECT path, kind FROM packs WHERE id=?", (pack_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "pack not found")
            target, select = row["path"], row["kind"] == "archive"
        else:
            raise HTTPException(400, "file_id or pack_id required")
        if not os.path.exists(target):
            raise HTTPException(404, "not reachable")
        subprocess.Popen(f'explorer /select,"{target}"' if select else f'explorer "{target}"')
        return {"ok": True}

    # ------------------------------------------------------------ store purchases

    from .itch import SIDE_FILES, is_pending, pending_files

    def item_status(files, category: str = "asset") -> str:
        if not files:
            return "no-files"
        files = [f for f in files if f.get("wanted", True)] or files  # skip unwanted platform builds
        main = [f for f in files if not SIDE_FILES.search(f["filename"])] or files
        if any(f["pack_id"] for f in files) and all(f["pack_id"] for f in main):
            return "downloaded"
        matched = sum(1 for f in main if f["pack_id"])
        return "missing" if matched == 0 else "partial"

    def load_items(c, where: str = "1", args=()) -> list[dict]:
        items = {}
        for r in c.execute(f"SELECT * FROM store_items WHERE {where} ORDER BY purchased_at DESC", args):
            d = {k: r[k] for k in ("id", "store", "title", "author", "author_url", "url", "cover_url",
                                   "classification", "purchased_at", "license", "terms", "ai")}
            d["tags"] = split_tags(r["tags"])
            d["store_id"] = r["store_id"]
            d["free"] = r["store"] == "itch" and not r["key_id"]  # added from the catalog, not bought/claimed
            d["hidden"] = bool(r["hidden"])
            d["category"] = r["category"] or "asset"
            d["files"] = []
            try:
                info = json.loads(r["info"] or "{}")
            except ValueError:
                info = {}
            d["bundle"] = info.get("Bundle", "")
            d["downloads_page"] = info.get("downloads_page", "")
            items[r["id"]] = d
        if not items:
            return []
        ids = list(items)
        for f in c.execute(
                f"SELECT sf.*, p.name AS pack_name, mf.path AS file_path FROM store_files sf "
                f"LEFT JOIN packs p ON p.id = sf.match_pack_id LEFT JOIN files mf ON mf.id = sf.match_file_id "
                f"WHERE sf.item_id IN ({','.join('?' * len(ids))}) ORDER BY sf.filename", ids):
            items[f["item_id"]]["files"].append({
                "id": f["id"], "filename": f["filename"], "display_name": f["display_name"], "size": f["size"],
                "match": f["match_kind"], "pack_id": f["match_pack_id"], "pack_name": f["pack_name"],
                "path": cfg.display_path(f["file_path"]) if f["file_path"] else None,
                "extra": bool(SIDE_FILES.search(f["filename"])),
                "type": f["type"],
                "saved": bool(f["saved_path"]),  # checked against the disk by Rescan, not per request
                "saved_path": cfg.display_path(f["saved_path"]) if f["saved_path"] else None,
            })
        for d in items.values():
            for f in d["files"]:  # what Download / Save would fetch; the UI's buttons follow this
                f["pending"] = is_pending(d["category"], bool(f["pack_id"]), f["saved"], f["filename"],
                                          f.get("wanted", True))
            d["status"] = item_status(d["files"], d["category"])
        return list(items.values())

    @app.get("/api/store/items")
    def store_items(status: str = "", hidden: bool = False, store: str = "", category: str = "asset"):
        where, args = "hidden = ? AND coalesce(category, 'asset') = ?", [int(hidden), category]
        if store == "itch":
            where += " AND store = ?"
            args.append(store)
        items = load_items(conn(), where, args)
        if status:
            items = [i for i in items if i["status"] in status.split(",")]
        return items

    class HideRequest(BaseModel):
        item_ids: list[int]
        hidden: bool = True

    @app.post("/api/store/hide-many")
    def hide_many(req: HideRequest):
        c = conn()
        with c:
            c.executemany("UPDATE store_items SET hidden=? WHERE id=?", [(int(req.hidden), i) for i in req.item_ids])
        return {"ok": True, "count": len(req.item_ids)}

    @app.get("/api/store/summary")
    def store_summary(store: str = "", category: str = "asset"):
        from . import secrets as secrets_mod
        c = conn()
        counts: dict[str, int] = {}
        stores: dict[str, int] = {}
        categories: dict[str, int] = {}
        items = load_items(c)
        for i in items:
            categories[i["category"]] = categories.get(i["category"], 0) + 1
            if i["category"] != category:
                continue
            stores[i["store"]] = stores.get(i["store"], 0) + 1
            if store and i["store"] != store:
                continue
            key = "hidden" if i["hidden"] else i["status"]
            counts[key] = counts.get(key, 0) + 1
        pending = pending_files(c)
        configured = secrets_mod.load()
        return {
            "total": sum(counts.values()), "counts": counts, "stores": stores, "categories": categories,
            "has_itch": bool(configured.get("itch_api_key")),
            "pending_files": len(pending), "pending_bytes": sum(r["size"] or 0 for r in pending),
            "pending_items": len({r["item_id"] for r in pending}),
            "download_dir": cfg.display_path(cfg.itch_download_dir) if cfg.itch_download_dir else None,
            "last_sync": float(db.get_kv(c, "itch_last_sync", 0) or 0),
            "auto_download": cfg.itch_auto_download, "sync_every_hours": cfg.itch_sync_hours,
        }

    class DownloadRequest(BaseModel):
        item_ids: list[int] | None = None
        file_ids: list[int] | None = None
        all_missing: bool = False

    @app.post("/api/store/download")
    def store_download(req: DownloadRequest):
        """Download missing itch.io files into the download folder."""
        from .itch import download_many
        c = conn()
        if req.file_ids:
            rows = c.execute(f"SELECT sf.id, si.store FROM store_files sf JOIN store_items si ON si.id = sf.item_id "
                             f"WHERE sf.match_pack_id IS NULL AND sf.id IN ({','.join('?' * len(req.file_ids))})",
                             req.file_ids).fetchall()
            itch_ids = [r["id"] for r in rows if r["store"] == "itch"]
        elif req.item_ids or req.all_missing:
            itch_ids = [r["id"] for r in pending_files(c, None if req.all_missing else req.item_ids)]
        else:
            raise HTTPException(400, "nothing selected")
        if not itch_ids:
            raise HTTPException(400, "nothing left to download")
        return start_job("download", lambda p: {"itch": download_many(cfg, itch_ids, p)},
                         f"Queued {len(itch_ids)} files…", queue=True)

    # ------------------------------------------------------------ free itch.io assets (search / add by URL)

    def itch_call(fn, *args):
        from .itch import ItchError
        try:
            return fn(*args)
        except ItchError as e:
            raise HTTPException(400, str(e)) from None

    def itch_states(c, game_ids: list[str]) -> dict:
        """game id -> {item_id, status, owned} for the itch.io items already in Purchases."""
        if not game_ids:
            return {}
        items = load_items(c, f"store='itch' AND store_id IN ({','.join('?' * len(game_ids))})", game_ids)
        return {i["store_id"]: {"item_id": i["id"], "status": i["status"], "owned": not i["free"],
                                "hidden": i["hidden"], "pending": any(f["pending"] for f in i["files"])}
                for i in items}

    @app.get("/api/itch/search")
    def itch_search(q: str):
        from .itch import search
        results = itch_call(search, q)
        states = itch_states(conn(), [r["game_id"] for r in results])
        for r in results:
            r["library"] = states.get(r["game_id"])
        return results

    @app.get("/api/itch/lookup")
    def itch_lookup(url: str):
        from .itch import lookup
        p = itch_call(lookup, url)
        p["library"] = itch_states(conn(), [p["game_id"]]).get(p["game_id"])
        return p

    class AddFreeRequest(BaseModel):
        url: str
        download: bool = True

    @app.post("/api/itch/add")
    def itch_add(req: AddFreeRequest):
        """Add a free itch.io asset to Purchases and, unless asked not to, download what isn't in the library."""
        from .itch import add_free, download_many
        added = itch_call(add_free, cfg, req.url)
        ids = added["pending_file_ids"]
        added["download"] = "none"
        if req.download and ids:
            job = start_job("download", lambda p: {"itch": download_many(cfg, ids, p)}, f"Queued {len(ids)} files…",
                            queue=True)
            added["download"] = "queued" if job["queued_behind"] else "started"
        return added

    @app.post("/api/store/items/{item_id}/hide")
    def hide_item(item_id: int, hidden: bool = True):
        c = conn()
        with c:
            c.execute("UPDATE store_items SET hidden=? WHERE id=?", (int(hidden), item_id))
        return {"ok": True}

    # ------------------------------------------------------------ background jobs

    jobs: dict[str, dict] = {}

    def start_job(name: str, fn, first_message: str, queue: bool = False):
        """Run fn in the background as job `name`, one at a time. If that job is already running, return its
        state; with queue=True, run fn after it instead (e.g. downloads requested while one is in progress)."""
        state = jobs.setdefault(name, {"running": False})
        lock = state.setdefault("_lock", threading.Lock())
        if not lock.acquire(blocking=False):
            if not queue:
                return public(state)
            state["queued"] = state.get("queued", 0) + 1
            waiting = True
        else:
            waiting = False

        def progress(i, n, msg):
            state.update(done=i, total=n, current=msg)

        def begin():
            state.update(running=True, done=0, total=0, current=first_message, result=None, error=None,
                         started=time.time())

        def run():
            if waiting:
                lock.acquire()
                state["queued"] -= 1
                begin()
            try:
                state["result"] = fn(progress)
            except Exception as e:
                state["error"] = str(e)
            finally:
                state["running"] = bool(state.get("queued"))  # stays "running" while queued work follows
                lock.release()

        if not waiting:
            begin()
        threading.Thread(target=run, daemon=True).start()
        return {**public(state), "queued_behind": waiting}

    def public(state: dict) -> dict:
        return {k: v for k, v in state.items() if not k.startswith("_")}

    @app.get("/api/jobs/{name}")
    def job_status(name: str):
        return public(jobs.get(name, {"running": False}))

    @app.post("/api/scan")
    def start_scan():
        from .scanner import scan
        return start_job("scan", lambda p: scan(cfg, p), "discovering packs…")

    @app.get("/api/scan")
    def scan_status():
        return job_status("scan")

    @app.post("/api/store/sync")
    def start_store_sync():
        from .itch import sync_and_auto_download
        return start_job("itch", lambda p: sync_and_auto_download(cfg, p), "fetching purchases…")

    # ------------------------------------------------------------ settings

    from . import settings as settings_mod

    def settings_out() -> dict:
        from . import secrets as secrets_mod
        return {
            "first_run": not os.path.exists(cfg.source),
            "roots": [{"label": r.label, "path": r.path, "display": r.display if r.display != r.path else None,
                       "containers": r.containers} for r in cfg.roots if not r.auto],
            "itch": {"download_dir": cfg.itch_download_dir, "sync_every_hours": cfg.itch_sync_hours,
                     "auto_download": cfg.itch_auto_download,
                     "has_key": bool(secrets_mod.load().get("itch_api_key")),
                     "user": db.get_kv(conn(), "itch_user")},
            "config_path": cfg.source, "data_dir": cfg.data_dir, "sevenzip": cfg.sevenzip,
            "default_download_dir": os.path.join(os.path.expanduser("~"), "Packrat", "itch.io"),
            "on_network": not is_loopback(cfg.host),
        }

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

    @app.get("/api/settings")
    def get_settings():
        return settings_out()

    @app.put("/api/settings")
    def put_settings(req: SettingsIn):
        roots, seen = [], set()
        for r in req.roots:
            path = os.path.normpath(os.path.expanduser(r.path.strip()))
            if not os.path.isdir(path):
                raise HTTPException(400, f"library folder not found: {path}")
            if os.path.normcase(path) not in seen:
                seen.add(os.path.normcase(path))
                roots.append({**r.model_dump(), "path": path})
        download_dir = req.itch.download_dir and os.path.normpath(os.path.expanduser(req.itch.download_dir.strip()))
        if download_dir:
            try:
                os.makedirs(download_dir, exist_ok=True)
            except OSError as e:
                raise HTTPException(400, f"can't create the download folder: {e.strerror}") from None
        if req.itch.sync_every_hours < 0:
            raise HTTPException(400, "sync interval can't be negative")
        old_roots = [r.path for r in cfg.roots]
        settings_mod.save(cfg, roots, {**req.itch.model_dump(), "download_dir": download_dir})
        rescan = [r.path for r in cfg.roots] != old_roots
        if rescan:
            from .scanner import scan
            start_job("scan", lambda p: scan(cfg, p), "scanning library folders…")
        return {**settings_out(), "rescan": rescan}

    class KeyIn(BaseModel):
        key: str

    @app.post("/api/settings/itch-key")
    def set_itch_key(req: KeyIn):
        """Check the key with itch.io, then store it. The key is never sent back to the browser."""
        from . import secrets as secrets_mod
        from .itch import Client, ItchError
        key = req.key.strip()
        if not key:
            raise HTTPException(400, "paste an API key first")
        try:
            user = Client(key, {"itch_api_key": key}).profile()
        except ItchError:
            raise HTTPException(400, "itch.io rejected this key. Copy it again from itch.io.") from None
        settings_mod.save_secrets({**secrets_mod.load(), "itch_api_key": key})
        db.set_kv(conn(), "itch_user", user.get("display_name") or user.get("username") or "")
        return settings_out()

    @app.delete("/api/settings/itch-key")
    def delete_itch_key():
        from . import secrets as secrets_mod
        values = secrets_mod.load()
        values.pop("itch_api_key", None)
        settings_mod.save_secrets(values)
        with conn() as c:
            c.execute("DELETE FROM kv WHERE key='itch_user'")
        return settings_out()

    @app.get("/api/browse")
    def browse(path: str = ""):
        """Folder listing for the folder picker in Settings."""
        try:
            return settings_mod.browse(path or None)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from None

    def scheduler():
        """Periodic itch.io sync while the server runs (config: [itch] sync_every_hours)."""
        from .itch import sync_and_auto_download
        from . import secrets as secrets_mod
        while True:
            time.sleep(60)
            if cfg.itch_sync_hours <= 0 or not secrets_mod.load().get("itch_api_key"):
                continue
            last = float(db.get_kv(conn(), "itch_last_sync", 0) or 0)
            if time.time() - last >= cfg.itch_sync_hours * 3600:
                start_job("itch", lambda p: sync_and_auto_download(cfg, p), "scheduled sync…")

    threading.Thread(target=scheduler, daemon=True).start()

    # One-time data fixes (cheap; safe to run on every start).
    from .itch import migrate as itch_migrate
    itch_migrate(conn())

    # A brand-new install (empty database) indexes the library on its own.
    if conn().execute("SELECT count(*) FROM packs").fetchone()[0] == 0:
        from .scanner import scan as _scan
        start_job("scan", lambda p: _scan(cfg, p), "first-time scan…")

    @app.get("/download/mcp_server.py")
    def download_mcp_server():
        """The standalone MCP client, so other computers can install it straight from Packrat."""
        return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py"),
                            media_type="text/x-python", filename="mcp_server.py")

    @app.get("/install.ps1")
    def install_ps1(request: Request):
        from .installers import POWERSHELL, render
        return Response(render(POWERSHELL, str(request.base_url)), media_type="text/plain; charset=utf-8")

    @app.get("/install.sh")
    def install_sh(request: Request):
        from .installers import SHELL, render
        return Response(render(SHELL, str(request.base_url)), media_type="text/plain; charset=utf-8")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    app.add_middleware(TextGZip)
    app.add_middleware(HostGuard, bind_host=cfg.host, allowed_hosts=cfg.allowed_hosts)
    return app


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
