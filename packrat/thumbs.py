import hashlib
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from .archives import read_file

Image.MAX_IMAGE_PIXELS = None  # trusted local files; some sprite sheets are huge
SIZE = 256


def thumb_key(row) -> str:
    raw = f"{row['path']}|{row['member'] or ''}|{row['size']}|{row['mtime']}"
    return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()


def thumb_path(cfg, row) -> str:
    key = thumb_key(row)
    return os.path.join(cfg.thumb_dir, key[:2], key + ".png")


def cover_rows(conn, pack_keys: list[str] | None = None) -> list:
    """The cover image file of every pack (or of the packs with these keys)."""
    sql = "SELECT f.* FROM packs p JOIN files f ON f.id = p.cover_file_id WHERE p.missing = 0"
    if pack_keys is None:
        return conn.execute(sql).fetchall()
    return conn.execute(f"{sql} AND p.key IN ({','.join('?' * len(pack_keys)) or 'NULL'})", pack_keys).fetchall()


def make_thumbs(cfg, rows, progress=None, label: str = "making thumbnails") -> int:
    """Generate the thumbnails these files don't have yet (several at once; reading them can be network-bound),
    so pages never wait for them. Returns how many were missing."""
    todo = [r for r in rows if not os.path.exists(thumb_path(cfg, r))]
    with ThreadPoolExecutor(8) as pool:
        for i, _ in enumerate(pool.map(lambda r: get_thumb(cfg, r), todo), 1):
            if progress:
                progress(i, len(todo), label)
    return len(todo)


def get_thumb(cfg, row) -> str | None:
    """Return the path of a cached PNG thumbnail, generating it if needed."""
    out = thumb_path(cfg, row)
    if os.path.exists(out):
        return out
    fail = out[:-4] + ".fail"
    if os.path.exists(fail):
        return None
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        data = read_file(row["path"], row["member"], cfg.sevenzip)
    except OSError:
        return None  # folder unreachable etc.; try again next time
    try:
        with Image.open(io.BytesIO(data)) as src:
            im = src.convert("RGBA") if src.mode not in ("RGB", "RGBA") else src.copy()
        if max(im.size) > SIZE:
            # Small pixel art stays crisp; large sheets get a smooth downscale.
            resample = Image.Resampling.NEAREST if max(im.size) <= SIZE * 2 else Image.Resampling.LANCZOS
            im.thumbnail((SIZE, SIZE), resample)
    except Exception as e:  # unreadable/unsupported: remember so we don't retry every request
        with open(fail, "w", encoding="utf-8") as f:
            f.write(repr(e))
        return None
    tmp = f"{out}.{threading.get_ident()}.tmp"
    im.save(tmp, "PNG")
    for attempt in range(5):
        try:
            os.replace(tmp, out)
            return out
        except PermissionError:  # Windows: antivirus/indexer briefly locking the new file
            if os.path.exists(out):
                os.remove(tmp)
                return out
            time.sleep(0.1 * (attempt + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None
