"""Library: packs, their files, search, previews, scanning, and the activity feed for background work."""
import mimetypes
import os
import re
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .. import db, desktop
from ..archives import read_member
from ..kinds import KINDS, LISTABLE, THUMBABLE, ext_of
from ..thumbs import get_thumb
from . import Context, get_file, is_local, split_tags
from .store import load_items

for _type, _ext in [("audio/ogg", ".ogg"), ("audio/wav", ".wav"), ("font/ttf", ".ttf"), ("font/otf", ".otf"),
                    ("font/woff", ".woff"), ("font/woff2", ".woff2"), ("image/svg+xml", ".svg"),
                    ("text/plain", ".txt"), ("text/plain", ".url"), ("text/plain", ".md")]:
    mimetypes.add_type(_type, _ext)

PACK_SORTS = {
    "name": "p.name COLLATE NOCASE",
    "size": "p.size DESC",
    "recent": "p.first_seen DESC, p.mtime DESC",
    "rating": "p.rating DESC, p.name COLLATE NOCASE",
}
TYPE_FILTERS = {"images": "p.image_count > 0", "audio": "p.audio_count > 0", "fonts": "p.font_count > 0"}
FILE_KINDS = set(KINDS) | {"other"}


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


def new_since(c: sqlite3.Connection) -> float:
    """Packs first seen after this are "new": the last 7 days, but not the first scan of a library."""
    first = c.execute("SELECT min(first_seen) FROM packs").fetchone()[0] or 0
    return max(time.time() - 7 * 86400, first + 3600)


def router(ctx: Context) -> APIRouter:
    r = APIRouter(prefix="/api")
    cfg = ctx.cfg
    conn = Depends(ctx.conn)

    def pack_out(row, since: float | None = None) -> dict:
        d = dict(row)
        d["new"] = since is not None and (d.get("first_seen") or 0) > since
        d["display_path"] = cfg.display_path(d["path"])
        d["tags"] = split_tags(d["tags"])
        d["auto_tags"] = split_tags(d["auto_tags"])
        d["cover"] = f"/api/files/{d['cover_file_id']}/thumb" if d["cover_file_id"] else None
        d["favorite"] = bool(d["favorite"])
        return d

    def file_out(row) -> dict:
        d = {k: row[k] for k in ("id", "pack_id", "name", "rel", "dir", "ext", "kind", "size")}
        d["in_archive"] = row["member"] is not None
        d["display_path"] = cfg.display_path(row["path"], row["member"])
        d["raw"] = f"/api/files/{row['id']}/raw"
        d["thumb"] = (f"/api/files/{row['id']}/thumb" if row["ext"] in THUMBABLE
                      else d["raw"] if row["ext"] == ".svg" else None)
        if "pack_name" in row.keys():
            d["pack_name"] = row["pack_name"]
        return d

    # ------------------------------------------------------------ packs

    @r.get("/facets")
    def facets(c: sqlite3.Connection = conn):
        roots = [dict(x) for x in c.execute(
            "SELECT root AS name, count(*) AS count FROM packs WHERE missing=0 GROUP BY root ORDER BY root")]
        tag_counts: dict[str, int] = {}
        for x in c.execute("SELECT tags, auto_tags FROM packs WHERE missing=0"):
            for t in set(split_tags(x["tags"]) + split_tags(x["auto_tags"])):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        totals = dict(c.execute(
            "SELECT count(*) AS packs, coalesce(sum(file_count),0) AS files, coalesce(sum(size),0) AS size, "
            "coalesce(sum(image_count),0) AS images, coalesce(sum(audio_count),0) AS audio, "
            "sum(favorite) AS favorites, sum(CASE WHEN image_count>0 THEN 1 ELSE 0 END) AS with_images, "
            "sum(CASE WHEN audio_count>0 THEN 1 ELSE 0 END) AS with_audio, "
            "sum(CASE WHEN font_count>0 THEN 1 ELSE 0 END) AS with_fonts "
            "FROM packs WHERE missing=0").fetchone())
        totals["new"] = c.execute("SELECT count(*) FROM packs WHERE missing=0 AND first_seen > ?",
                                  (new_since(c),)).fetchone()[0]
        return {
            "roots": roots,
            "tags": sorted(({"name": k, "count": v} for k, v in tag_counts.items()),
                           key=lambda t: (-t["count"], t["name"])),
            "totals": totals,
            "missing": c.execute("SELECT count(*) FROM packs WHERE missing=1").fetchone()[0],
        }

    @r.get("/packs")
    def list_packs(root: str = "", type: str = "", tag: str = "", fav: bool = False, sort: str = "name",
                   missing: bool = False, new: bool = False, c: sqlite3.Connection = conn):
        since = new_since(c)
        where, args = ["p.missing = ?"], [1 if missing else 0]
        if new:
            where.append("p.first_seen > ?")
            args.append(since)
            sort = "recent"
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
        return [pack_out(x, since) for x in c.execute(sql, args)]

    @r.get("/packs/{pack_id}")
    def get_pack(pack_id: int, c: sqlite3.Connection = conn):
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
        out["store"] = load_items(ctx, c, "id IN (SELECT item_id FROM store_files WHERE match_pack_id=?)", (pack_id,))
        return out

    @r.patch("/packs/{pack_id}")
    def update_pack(pack_id: int, meta: PackMeta, c: sqlite3.Connection = conn):
        sets, args = [], []
        for field in ("vendor", "license", "notes"):
            value = getattr(meta, field)
            if value is not None:
                sets.append(f"{field}=?")
                args.append(value.strip())
        if meta.tags is not None:
            tags = meta.tags if isinstance(meta.tags, list) else meta.tags.split(",")
            sets.append("tags=?")
            args.append(",".join(sorted({t.strip().lower() for t in tags if t.strip()})))
        if meta.rating is not None:
            sets.append("rating=?")
            args.append(max(0, min(5, meta.rating)))
        if meta.favorite is not None:
            sets.append("favorite=?")
            args.append(int(meta.favorite))
        if sets:
            with c:
                c.execute(f"UPDATE packs SET {', '.join(sets)} WHERE id=?", (*args, pack_id))
                db.refresh_pack_fts(c, pack_id)
        return get_pack(pack_id, c)

    @r.get("/packs/{pack_id}/files")
    def pack_files(pack_id: int, dir: str = "", kind: str = "", flat: bool = False, offset: int = 0,
                   limit: int = 300, sort: str = "name", c: sqlite3.Connection = conn):
        """One folder of a pack: its subfolders (with file counts) and a page of its files. flat=True lists
        every file below the folder instead. Archives inside a pack show as folders of their contents."""
        limit = max(1, min(limit, 2000))
        kind_sql, kind_args = ("AND kind=?", [kind]) if kind in FILE_KINDS else ("", [])
        prefix = dir + "/" if dir else ""
        in_dir = "(dir = ? OR substr(dir, 1, ?) = ?)" if dir else "1"
        in_dir_args = [dir, len(prefix), prefix] if dir else []

        dirs: dict[str, int] = {}
        if not flat:
            for x in c.execute(f"SELECT dir, count(*) AS n FROM files WHERE pack_id=? AND {in_dir} "
                               f"{kind_sql} GROUP BY dir", (pack_id, *in_dir_args, *kind_args)):
                if x["dir"] == dir:
                    continue
                child = x["dir"][len(prefix):].split("/", 1)[0]
                dirs[child] = dirs.get(child, 0) + x["n"]
            file_where, file_args = "dir = ?", [dir]
        else:
            file_where, file_args = in_dir, in_dir_args

        base = f"FROM files WHERE pack_id=? AND {file_where} {kind_sql}"
        params = (pack_id, *file_args, *kind_args)
        total = c.execute(f"SELECT count(*) {base}", params).fetchone()[0]
        order = "size DESC" if sort == "size" else "rel COLLATE NOCASE"
        rows = c.execute(f"SELECT * {base} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {
            "dirs": [{"name": k, "path": prefix + k, "count": v, "archive": ext_of(k) in LISTABLE}
                     for k, v in sorted(dirs.items(), key=lambda kv: kv[0].lower())],
            "files": [file_out(x) for x in rows],
            "total": total,
        }

    # ------------------------------------------------------------ search

    @r.get("/search")
    def search(q: str, kind: str = "", pack_id: int | None = None, offset: int = 0, limit: int = 200,
               c: sqlite3.Connection = conn):
        result = _search(c, q, kind, pack_id, offset, limit, any_word=False)
        if not result["total"] and not result["packs"] and len(re.findall(r"[^\W_]+", q)) > 1:
            # Nothing has every word; fall back to any word, best matches first.
            result = _search(c, q, kind, pack_id, offset, limit, any_word=True)
            result["matched"] = "any word"
        return result

    def _search(c, q: str, kind: str, pack_id: int | None, offset: int, limit: int, any_word: bool):
        match = fts_query(q, any_word)
        if not match:
            return {"packs": [], "files": [], "total": 0}
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

        # Packs: name/tag/author matches first, then packs ranked by how many of their files match.
        packs = []
        if pack_id is None and offset == 0:
            seen = set()
            for x in c.execute("SELECT p.*, 0 AS dupes FROM packs_fts JOIN packs p ON p.id = packs_fts.rowid "
                               "WHERE packs_fts MATCH ? AND p.missing = 0 ORDER BY rank LIMIT 60", (match,)):
                packs.append({**pack_out(x), "hits": None})
                seen.add(x["id"])
            for x in c.execute(f"SELECT p.*, 0 AS dupes, count(*) AS hits {base} GROUP BY p.id "
                               f"ORDER BY hits DESC LIMIT 60", args):
                if x["id"] not in seen:
                    packs.append({**pack_out(x), "hits": x["hits"]})
        return {"packs": packs, "files": [file_out(x) for x in files], "total": total}

    # ------------------------------------------------------------ file content

    @r.get("/files/{file_id}/info")
    def file_info(file_id: int, c: sqlite3.Connection = conn):
        return file_out(get_file(c, file_id))

    @r.get("/files/{file_id}/raw")
    def raw(file_id: int, download: bool = False, c: sqlite3.Connection = conn):
        row = get_file(c, file_id)
        media = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
        disposition = "attachment" if download else "inline"
        if row["member"] is None:
            if not os.path.exists(row["path"]):
                raise HTTPException(404, "not found on disk")
            return FileResponse(row["path"], media_type=media, filename=row["name"],
                                content_disposition_type=disposition)
        try:
            data = read_member(row["path"], row["member"], cfg.sevenzip)
        except Exception as e:
            raise HTTPException(500, f"cannot read from archive: {e}") from None
        quoted = row["name"].encode("ascii", "replace").decode().replace('"', "")
        return Response(data, media_type=media, headers={
            "Content-Disposition": f'{disposition}; filename="{quoted}"',
            "Cache-Control": "private, max-age=86400",
        })

    @r.get("/files/{file_id}/thumb")
    def thumb(file_id: int, c: sqlite3.Connection = conn):
        path = get_thumb(cfg, get_file(c, file_id))
        if path is None:
            raise HTTPException(404, "no preview")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=604800"})

    @r.post("/reveal")
    def reveal(request: Request, file_id: int | None = None, pack_id: int | None = None,
               c: sqlite3.Connection = conn):
        """Open the file manager at the item. Only for a browser on the computer running Packrat."""
        if not is_local(request):
            raise HTTPException(403, "only works on the computer running Packrat")
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
            raise HTTPException(404, "not found on disk")
        try:
            desktop.reveal(target, select)
        except desktop.NoFileManager as e:
            raise HTTPException(501, str(e)) from None
        return {"ok": True}

    # ------------------------------------------------------------ background work

    @r.post("/scan")
    def start_scan():
        return ctx.start_scan()

    @r.get("/activity")
    def activity():
        """Scans, syncs and downloads in progress, for the header indicator and per-purchase progress."""
        return {"scan": ctx.jobs.status("scan"), "itch": ctx.jobs.status("itch"),
                "download": ctx.jobs.status("download"), "items": ctx.downloads.items}

    return r
