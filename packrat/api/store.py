"""Purchases: the itch.io library, downloads, sync, and finding free assets on itch.io."""
import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db, itch, stores
from . import Context, split_tags

GONE = "no longer available on itch.io"  # itch._refresh_item stores this when a page was removed


def item_status(files: list[dict]) -> str:
    """downloaded: every main file is in the library; partial: some are; missing: none; no-files: nothing to get.
    Extras (readmes, installers…) don't count."""
    if not files:
        return "no-files"
    main = [f for f in files if not f["extra"]] or files
    matched = sum(1 for f in main if f["pack_id"])
    return "downloaded" if matched == len(main) else "partial" if matched else "missing"


def load_items(ctx: Context, c: sqlite3.Connection, where: str = "1", args=()) -> list[dict]:
    """Purchases matching an SQL condition, with their files, library matches and download state."""
    items = {}
    for r in c.execute(f"SELECT * FROM store_items WHERE asset = 1 AND ({where}) ORDER BY purchased_at DESC", args):
        d = {k: r[k] for k in ("id", "store", "store_id", "title", "author", "author_url", "url", "cover_url",
                               "classification", "purchased_at", "license", "terms", "ai")}
        d["tags"] = split_tags(r["tags"])
        d["free"] = not r["key_id"]  # added as a free asset, not bought or claimed
        d["hidden"] = bool(r["hidden"])
        info = json.loads(r["info"] or "{}")
        d["page_gone"] = info.get("Page") == GONE
        d["bundle"] = info.get("Bundle", "")  # Humble: the bundle it came in
        d["files"] = []
        items[r["id"]] = d
    if not items:
        return []
    for f in c.execute(
            f"SELECT sf.*, p.name AS pack_name FROM store_files sf LEFT JOIN packs p ON p.id = sf.match_pack_id "
            f"WHERE sf.item_id IN ({db.placeholders(items)}) ORDER BY sf.filename", list(items)):
        items[f["item_id"]]["files"].append({
            "id": f["id"], "filename": f["filename"], "size": f["size"], "match": f["match_kind"],
            "pack_id": f["match_pack_id"], "pack_name": f["pack_name"],
            "extra": bool(stores.SIDE_FILES.search(f["filename"])),
            "pending": stores.is_pending(f["match_pack_id"] is not None, f["filename"]),  # what Download would fetch
        })
    for d in items.values():
        d["status"] = item_status(d["files"])
        d["downloading"] = ctx.downloads.items.get(d["id"])
        d["pack_id"] = next((f["pack_id"] for f in d["files"] if f["pack_id"] and not f["extra"]), None)
    return list(items.values())


class HideRequest(BaseModel):
    item_ids: list[int]
    hidden: bool = True


class DownloadRequest(BaseModel):
    item_ids: list[int] | None = None
    file_ids: list[int] | None = None
    all_missing: bool = False


class AddFreeRequest(BaseModel):
    url: str
    download: bool = True


def itch_call(fn, *args):
    try:
        return fn(*args)
    except itch.ItchError as e:
        raise HTTPException(400, str(e)) from None


def router(ctx: Context) -> APIRouter:
    r = APIRouter(prefix="/api")
    cfg = ctx.cfg
    conn = Depends(ctx.conn)

    # ------------------------------------------------------------ purchases

    @r.get("/store/items")
    def store_items(status: str = "", hidden: bool = False, c: sqlite3.Connection = conn):
        items = load_items(ctx, c, "hidden = ?", [int(hidden)])
        return [i for i in items if i["status"] in status.split(",")] if status else items

    @r.post("/store/hide-many")
    def hide_many(req: HideRequest, c: sqlite3.Connection = conn):
        with c:
            c.executemany("UPDATE store_items SET hidden=? WHERE id=?", [(int(req.hidden), i) for i in req.item_ids])
        return {"ok": True, "count": len(req.item_ids)}

    @r.get("/store/summary")
    def store_summary(c: sqlite3.Connection = conn):
        counts: dict[str, int] = {}
        for i in load_items(ctx, c):
            key = "hidden" if i["hidden"] else i["status"]
            counts[key] = counts.get(key, 0) + 1
        pending = stores.pending_files(c)
        connected = stores.connected()
        return {
            "total": sum(counts.values()), "counts": counts,
            "has_itch": connected["itch"], "has_humble": connected["humble"],
            "pending_files": len(pending), "pending_bytes": sum(f["size"] or 0 for f in pending),
            "pending_items": len({f["item_id"] for f in pending}),
            # where the pending files would go
            "download_dirs": [cfg.display_path(cfg.download_dirs[s]) for s in sorted({f["store"] for f in pending})],
            "last_sync": max(float(db.get_kv(c, f"{s}_last_sync", 0) or 0) for s in stores.NAMES),
            "sync_errors": sum((json.loads(db.get_kv(c, f"{s}_last_errors", "[]") or "[]") for s in stores.NAMES), []),
            "auto_download": cfg.itch_auto_download, "sync_every_hours": cfg.itch_sync_hours,
        }

    @r.post("/store/sync")
    def start_sync():
        return ctx.start_sync()

    # ------------------------------------------------------------ downloads

    @r.post("/store/download")
    def store_download(req: DownloadRequest, c: sqlite3.Connection = conn):
        """Download missing files: chosen files, everything missing from chosen purchases, or all of it."""
        if req.file_ids:
            ids = [f["id"] for f in c.execute(f"SELECT id FROM store_files WHERE match_pack_id IS NULL AND id IN "
                                              f"({db.placeholders(req.file_ids)})", req.file_ids)]
        elif req.item_ids or req.all_missing:
            ids = [f["id"] for f in stores.pending_files(c, None if req.all_missing else req.item_ids)]
        else:
            raise HTTPException(400, "nothing selected")
        if not ids:
            raise HTTPException(400, "nothing left to download")
        return ctx.downloads.start(ids)

    @r.post("/downloads/cancel")
    def cancel_downloads():
        """Stop the running download batch and drop the queued ones. Finished files are kept."""
        ctx.downloads.cancel()
        return {"ok": True}

    @r.post("/store/items/{item_id}/hide")
    def hide_item(item_id: int, hidden: bool = True, c: sqlite3.Connection = conn):
        with c:
            c.execute("UPDATE store_items SET hidden=? WHERE id=?", (int(hidden), item_id))
        return {"ok": True}

    # ------------------------------------------------------------ free assets on itch.io

    def with_library(c: sqlite3.Connection, results: list[dict]) -> list[dict]:
        """Mark itch.io results that are already in Purchases with their state there (`library`)."""
        ids = [x["game_id"] for x in results]
        items = load_items(ctx, c, f"store='itch' AND store_id IN ({db.placeholders(ids)})", ids)
        state = {i["store_id"]: {"item_id": i["id"], "status": i["status"], "owned": not i["free"],
                                 "hidden": i["hidden"], "pending": any(f["pending"] for f in i["files"]),
                                 "downloading": i["downloading"], "pack_id": i["pack_id"]} for i in items}
        for x in results:
            x["library"] = state.get(x["game_id"])
        return results

    @r.get("/itch/search")
    def itch_search(q: str, c: sqlite3.Connection = conn):
        return with_library(c, itch_call(itch.search, q))

    @r.get("/itch/tag")
    def itch_tag(tag: str, page: int = 1, c: sqlite3.Connection = conn):
        """Free itch.io assets with a tag, a page at a time."""
        data = itch_call(itch.browse_free_tag, tag, page)
        with_library(c, data["results"])
        return data

    @r.get("/itch/lookup")
    def itch_lookup(url: str, c: sqlite3.Connection = conn):
        return with_library(c, [itch_call(itch.lookup, url)])[0]

    @r.post("/itch/add")
    def itch_add(req: AddFreeRequest):
        """Add a free itch.io asset to Purchases and, unless asked not to, download what isn't in the library."""
        added = itch_call(itch.add_free, cfg, req.url)
        added["download"] = "none"
        if req.download and added["pending_file_ids"]:
            job = ctx.downloads.start(added["pending_file_ids"])
            added["download"] = "queued" if job["queued_behind"] else "started"
        return added

    return r
