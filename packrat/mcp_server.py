"""MCP server exposing the Packrat library to AI agents such as Claude Code.

It talks to the running Packrat server over HTTP (PACKRAT_URL, default http://localhost:8765).
Register it with Claude Code once:

    claude mcp add packrat --scope user -- packrat mcp
"""
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image as PILImage, ImageDraw, ImageFont
from mcp.server.mcpserver import Image, MCPServer

from .kinds import LISTABLE

BASE = os.environ.get("PACKRAT_URL", "http://localhost:8765").rstrip("/")

INSTRUCTIONS = """\
Packrat: the user's personal library of purchased and free game assets (pixel art, tilesets,
characters, UI, icons, VFX, sound effects, music, fonts). list_tags gives the size of the library.
Purchases synced from itch.io carry author, tags and usage terms.

Typical workflow when a project needs assets:
1. search_assets with a few keywords (e.g. "chest", "door open", "forest tileset") or browse with
   list_packs / list_tags. Packs are folders or archives; files inside archives work like normal files.
2. Look before choosing: preview_pack or preview_files return contact sheets of thumbnails (labelled
   with file ids). Check tile size / art style consistency with what the project already uses.
3. get_pack for license / usage terms and the store page before using an asset in a shipped project;
   read_text_file reads license/readme files inside a pack without copying them.
4. copy_to_project to copy the chosen files (or a whole folder of a pack) into the project.
Never modify or delete anything in the library itself; it is the user's master copy.
"""

mcp = MCPServer("packrat", instructions=INSTRUCTIONS)


class PackratError(Exception):
    pass


def _url(path: str, params: dict | None = None) -> str:
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", False)}
    return f"{BASE}{path}" + (f"?{urllib.parse.urlencode(clean)}" if clean else "")


def _get(path: str, params: dict | None = None, raw: bool = False):
    try:
        with urllib.request.urlopen(_url(path, params), timeout=120) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise PackratError(f"{e.code}: {detail}") from None
    except urllib.error.URLError:
        raise PackratError(f"Packrat isn't reachable at {BASE}. Ask the user to start it "
                           f"(run: packrat serve).") from None
    return body if raw else json.loads(body)


def _pack_brief(p: dict) -> dict:
    out = {
        "pack_id": p["id"], "name": p["name"], "location": p.get("grp") or p.get("root"),
        "kind": p["kind"], "files": p["file_count"], "size_mb": round((p["size"] or 0) / 1048576, 1),
        "tags": sorted(set(p.get("tags", []) + p.get("auto_tags", []))),
    }
    for key, label in (("image_count", "images"), ("audio_count", "sounds"), ("font_count", "fonts")):
        if p.get(key):
            out[label] = p[key]
    if p.get("rating"):
        out["rating"] = p["rating"]
    if p.get("favorite"):
        out["favorite"] = True
    if p.get("hits"):
        out["matching_files"] = p["hits"]
    return out


def _file_brief(f: dict) -> dict:
    out = {"file_id": f["id"], "name": f["name"], "path_in_pack": f["rel"], "kind": f["kind"],
           "size_kb": round((f["size"] or 0) / 1024, 1), "pack_id": f["pack_id"]}
    if f.get("pack_name"):
        out["pack"] = f["pack_name"]
    if f.get("in_archive"):
        out["in_archive"] = True
    return out


# ------------------------------------------------------------------ discovery

@mcp.tool()
def search_assets(query: str, kind: str = "", limit: int = 40) -> dict:
    """Search pack names/tags/notes/authors and file paths. Words are prefix-matched; results must contain
    all words, falling back to any word when nothing contains all. Short keywords work best ("chest",
    "slime", "footstep") since file names are terse.

    kind: optional file filter: image | audio | font | source | doc | archive.
    Returns matching packs (name/tag/author hits first, then packs ranked by file hits) and files.
    """
    res = _get("/api/search", {"q": query, "kind": kind, "limit": max(1, min(limit, 200))})
    out = {
        "packs": [_pack_brief(p) for p in res["packs"][:25]],
        "files_total": res["total"],
        "files": [_file_brief(f) for f in res["files"]],
    }
    if res.get("matched") == "any word":
        out["note"] = "No results contained every word; these match any of the words, best first."
    return out


@mcp.tool()
def list_tags() -> dict:
    """Library overview: library roots, tags with pack counts, and totals. Good starting point for browsing."""
    f = _get("/api/facets")
    return {"roots": f["roots"], "tags": f["tags"], "totals": f["totals"]}


@mcp.tool()
def list_packs(tag: str = "", contains: str = "", root: str = "", favorites_only: bool = False,
               sort: str = "name", limit: int = 150) -> dict:
    """List packs, optionally filtered.

    tag: a tag from list_tags (e.g. tileset, characters, sfx, music, ui, icons, pixel, fantasy, modern).
    contains: images | audio | fonts.  root: a library root name from list_tags.
    sort: name | recent | size | rating.
    """
    packs = _get("/api/packs", {"tag": tag, "type": contains, "root": root, "fav": favorites_only, "sort": sort})
    return {"total": len(packs), "packs": [_pack_brief(p) for p in packs[:max(1, min(limit, 700))]]}


@mcp.tool()
def get_pack(pack_id: int) -> dict:
    """Full details of a pack: contents summary, top-level folders, the user's notes/license/rating, and the
    store listing it came from (title, author, page URL, tags, license and usage terms quoted from the page)."""
    p = _get(f"/api/packs/{pack_id}")
    top = _get(f"/api/packs/{pack_id}/files", {"limit": 60})
    wrapper = ""
    while len(top["dirs"]) == 1 and not top["files"]:  # archives often wrap everything in one folder
        wrapper = top["dirs"][0]["path"]
        top = _get(f"/api/packs/{pack_id}/files", {"dir": wrapper, "limit": 60})
    out = _pack_brief(p)
    out.update({
        "path": p["display_path"],
        "contents_root": wrapper or "(top level)",
        "top_level_folders": [{"path": d["path"], "files": d["count"]} for d in top["dirs"]],
        "top_level_files": [f["name"] for f in top["files"]][:60],
        "user_notes": {k: p[k] for k in ("vendor", "license", "notes") if p.get(k)},
        "store": [{k: it[k] for k in ("store", "title", "author", "url", "license", "terms", "ai", "tags") if it.get(k)}
                  for it in p.get("store", [])],
        "duplicates_elsewhere": [d["display_path"] for d in p.get("duplicates", [])],
    })
    return out


@mcp.tool()
def list_pack_files(pack_id: int, folder: str = "", kind: str = "", recursive: bool = False,
                    offset: int = 0, limit: int = 200) -> dict:
    """Browse a pack. folder is a path inside the pack ('' = top level, '/'-separated, as returned in
    'subfolders'). recursive=True lists everything below the folder. kind: image | audio | font | source | doc."""
    res = _get(f"/api/packs/{pack_id}/files",
               {"dir": folder, "kind": kind, "flat": recursive, "offset": offset, "limit": max(1, min(limit, 1000))})
    return {
        "folder": folder,
        "subfolders": [{"path": d["path"], "files": d["count"]} for d in res["dirs"]],
        "files_total": res["total"],
        "files": [_file_brief(f) for f in res["files"]],
    }


# ------------------------------------------------------------------ previews

CELL = 192
LABEL_H = 30


def _contact_sheet(entries: list[tuple[str, bytes | None]], cols: int = 4) -> bytes:
    """Grid of thumbnails on a checkerboard, each labelled. Pixel art is scaled up with nearest-neighbour."""
    rows = (len(entries) + cols - 1) // cols
    sheet = PILImage.new("RGB", (cols * CELL, rows * (CELL + LABEL_H)), (32, 33, 36))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    checker = PILImage.new("RGB", (CELL, CELL), (60, 62, 66))
    cd = ImageDraw.Draw(checker)
    for y in range(0, CELL, 12):
        for x in range(0, CELL, 12):
            if (x // 12 + y // 12) % 2:
                cd.rectangle([x, y, x + 11, y + 11], fill=(74, 76, 80))
    for i, (label, data) in enumerate(entries):
        x, y = (i % cols) * CELL, (i // cols) * (CELL + LABEL_H)
        sheet.paste(checker, (x, y))
        if data:
            try:
                im = PILImage.open(io.BytesIO(data)).convert("RGBA")
                scale = min((CELL - 8) / im.width, (CELL - 8) / im.height)
                if scale >= 1:
                    scale = max(1, int(scale))
                    im = im.resize((im.width * scale, im.height * scale), PILImage.Resampling.NEAREST)
                else:
                    im.thumbnail((CELL - 8, CELL - 8), PILImage.Resampling.LANCZOS)
                sheet.paste(im, (x + (CELL - im.width) // 2, y + (CELL - im.height) // 2), im)
            except Exception:
                draw.text((x + 8, y + CELL // 2), "(no preview)", fill=(200, 200, 200), font=font)
        else:
            draw.text((x + 8, y + CELL // 2), "(no preview)", fill=(200, 200, 200), font=font)
        for line_no, text in enumerate(_wrap(label, 30)[:2]):
            draw.text((x + 4, y + CELL + 3 + line_no * 13), text, fill=(230, 230, 230), font=font)
    buf = io.BytesIO()
    sheet.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _short(rel: str) -> str:
    """'a/b/chest1/1.png' -> 'chest1/1.png': enough context to tell same-named frames apart."""
    return "/".join(rel.split("/")[-2:])


def _wrap(text: str, width: int) -> list[str]:
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def _thumb(file_id: int) -> bytes | None:
    try:
        return _get(f"/api/files/{file_id}/thumb", raw=True)
    except PackratError:
        return None


@mcp.tool()
def preview_files(file_ids: list[int]) -> list:
    """Contact sheet of up to 24 image files (thumbnails, labelled '#file_id name'). Use it to judge style,
    resolution and fit before copying. Sound/font files can't be previewed visually."""
    ids = file_ids[:24]
    entries = []
    for i in ids:
        try:
            label = _short(_get(f"/api/files/{i}/info")["rel"])
        except PackratError:
            label = "(unknown file)"
        entries.append((f"#{i} {label}", _thumb(i)))
    return [Image(data=_contact_sheet(entries), format="png"),
            f"{len(ids)} files shown" + (f"; {len(file_ids) - 24} more not shown" if len(file_ids) > 24 else "")]


@mcp.tool()
def preview_pack(pack_id: int, folder: str = "", recursive: bool = True, limit: int = 16) -> list:
    """Contact sheet of images in a pack (or one folder of it), up to 24, largest first so sprite sheets
    and tilesets show up rather than single frames. Returns the sheet plus a legend of file ids."""
    res = _get(f"/api/packs/{pack_id}/files", {"dir": folder, "kind": "image", "flat": recursive,
                                               "sort": "size", "limit": max(1, min(limit, 24))})
    files = res["files"]
    if not files:
        return [f"No images in pack {pack_id}{' / ' + folder if folder else ''}."]
    sheet = _contact_sheet([(f"#{f['id']} {_short(f['rel'])}", _thumb(f["id"])) for f in files])
    legend = [{"file_id": f["id"], "path_in_pack": f["rel"], "size_kb": round(f["size"] / 1024, 1)} for f in files]
    return [Image(data=sheet, format="png"),
            {"images_in_scope": res["total"], "shown": legend}]


@mcp.tool()
def view_image(file_id: int) -> list:
    """The full-resolution image (up to 2048px; larger ones are downscaled), for inspecting a single
    sprite sheet or tileset in detail, e.g. to work out its tile/frame size."""
    data = _get(f"/api/files/{file_id}/raw", raw=True)
    try:
        im = PILImage.open(io.BytesIO(data))
        w, h = im.size
        im = im.convert("RGBA")
    except Exception:
        return [f"File {file_id} is not a viewable image."]
    note = f"{w}x{h} px"
    if max(w, h) > 2048:
        im.thumbnail((2048, 2048), PILImage.Resampling.LANCZOS)
        note += f" (shown downscaled to {im.width}x{im.height})"
    elif max(w, h) < 512:
        scale = 512 // max(w, h)
        if scale > 1:
            im = im.resize((w * scale, h * scale), PILImage.Resampling.NEAREST)
            note += f" (shown {scale}x enlarged)"
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return [Image(data=buf.getvalue(), format="png"), note]


@mcp.tool()
def read_text_file(file_id: int) -> str:
    """Read a text file from the library (license, readme, credits, .txt/.md/.json/.url) without copying it.
    Use it to check license terms that ship inside a pack (list_pack_files with kind='doc' finds them)."""
    info = _get(f"/api/files/{file_id}/info")
    if info["ext"] not in (".txt", ".md", ".json", ".url", ".ini", ".csv", ".xml", ".tsx", ".tmx", ".html", ".htm"):
        return f"{info['name']} is not a plain-text file ({info['ext']}); it can't be read here."
    data = _get(f"/api/files/{file_id}/raw", raw=True)[:40_000]
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return f"--- {info['rel']} (pack {info['pack_id']})\n" + data.decode(enc)
        except UnicodeDecodeError:
            continue
    return "(could not decode)"


# ------------------------------------------------------------------ export

MAX_COPY_FILES = 3000


@mcp.tool()
def copy_to_project(destination: str, file_ids: list[int] | None = None, pack_id: int | None = None,
                    folder: str = "", kind: str = "", overwrite: bool = False) -> dict:
    """Copy assets into a project. destination: absolute directory in the project (created if needed).

    Either give file_ids (copied flat into destination), or pack_id (+ optional folder inside the pack and
    kind filter) to copy that folder recursively, keeping its sub-structure. Existing files are skipped
    unless overwrite=True. The library itself is never modified. Returns written paths and the pack's
    license/usage terms so attribution requirements can be recorded in the project.
    """
    if not os.path.isabs(destination):
        raise PackratError("destination must be an absolute path")
    jobs: list[tuple[dict, str]] = []
    if file_ids:
        used: set[str] = set()
        for fid in file_ids:
            info = _get(f"/api/files/{fid}/info")
            name = info["name"]
            if name.lower() in used:  # e.g. frames named 1.png in several folders: prefix the folder name
                parent = info["rel"].split("/")[-2] if "/" in info["rel"] else f"file{fid}"
                name = f"{parent}_{name}"
            used.add(name.lower())
            jobs.append((info, name))
    elif pack_id is not None:
        offset, prefix = 0, (folder.strip("/") + "/") if folder.strip("/") else ""
        while True:
            res = _get(f"/api/packs/{pack_id}/files",
                       {"dir": folder.strip("/"), "kind": kind, "flat": True, "offset": offset, "limit": 1000})
            for f in res["files"]:
                rel = f["rel"][len(prefix):] if f["rel"].startswith(prefix) else f["name"]
                jobs.append((f, rel))
            offset += len(res["files"])
            if not res["files"] or offset >= res["total"]:
                break
            if len(jobs) > MAX_COPY_FILES:
                raise PackratError(f"More than {MAX_COPY_FILES} files; narrow it down with folder/kind.")
    else:
        raise PackratError("give file_ids or pack_id")

    dest_root = os.path.abspath(destination)
    written, skipped = [], []
    packs = set()
    for f, rel in jobs:
        if pack_id is not None and f["ext"] in LISTABLE and not f.get("in_archive"):
            continue  # an archive inside a folder pack: its contents are listed (and copied) separately
        target = os.path.abspath(os.path.join(dest_root, *rel.split("/")))
        if not target.startswith(dest_root):
            continue  # never write outside the destination
        packs.add(f["pack_id"])
        if os.path.exists(target) and not overwrite:
            skipped.append(target)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as out:
            out.write(_get(f"/api/files/{f['id']}/raw", raw=True))
        written.append(target)

    licenses = []
    for pid in sorted(packs):
        p = _get(f"/api/packs/{pid}")
        entry = {"pack": p["name"], "pack_id": pid}
        if p.get("license"):
            entry["user_license_note"] = p["license"]
        for it in p.get("store", []):
            entry.update({k: it[k] for k in ("title", "author", "url", "license", "terms") if it.get(k)})
        licenses.append(entry)
    return {
        "written": len(written), "skipped_existing": len(skipped),
        "files": written[:200] + ([f"... {len(written) - 200} more"] if len(written) > 200 else []),
        "license_info": licenses,
    }


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
