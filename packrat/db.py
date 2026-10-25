import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS packs(
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,          -- kind:normalized path
  path TEXT NOT NULL,
  kind TEXT NOT NULL,                -- folder | archive | loose
  name TEXT NOT NULL,
  norm_name TEXT NOT NULL,           -- for spotting the same pack in two places
  root TEXT NOT NULL,
  grp TEXT NOT NULL DEFAULT '',      -- container/bundle folder the pack sits in
  size INTEGER DEFAULT 0,
  file_count INTEGER DEFAULT 0,
  image_count INTEGER DEFAULT 0,
  audio_count INTEGER DEFAULT 0,
  font_count INTEGER DEFAULT 0,
  source_count INTEGER DEFAULT 0,
  archive_count INTEGER DEFAULT 0,
  doc_count INTEGER DEFAULT 0,
  cover_file_id INTEGER,
  auto_tags TEXT DEFAULT '',
  mtime REAL,
  first_seen REAL,
  last_scanned REAL,
  missing INTEGER DEFAULT 0,
  -- user-maintained
  vendor TEXT DEFAULT '',
  license TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  notes TEXT DEFAULT '',
  rating INTEGER DEFAULT 0,
  favorite INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pack_id INTEGER NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
  path TEXT NOT NULL,                -- file on disk (the archive, for archive members)
  member TEXT,                       -- path inside the archive, or NULL
  rel TEXT NOT NULL,                 -- path relative to the pack, '/'-separated
  dir TEXT NOT NULL,
  name TEXT NOT NULL,
  ext TEXT NOT NULL,
  kind TEXT NOT NULL,
  size INTEGER,
  mtime REAL
);
CREATE INDEX IF NOT EXISTS packs_norm ON packs(norm_name);  -- duplicate-pack lookups
CREATE INDEX IF NOT EXISTS files_pack_dir ON files(pack_id, dir);
CREATE INDEX IF NOT EXISTS files_pack_kind ON files(pack_id, kind);
CREATE TABLE IF NOT EXISTS archive_cache(
  path TEXT PRIMARY KEY, size INTEGER, mtime REAL, listing TEXT
);
CREATE TABLE IF NOT EXISTS store_items(
  id INTEGER PRIMARY KEY,
  store TEXT NOT NULL,               -- itch
  store_id TEXT NOT NULL,
  key_id TEXT,                       -- download key / order key
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  author_url TEXT DEFAULT '',
  url TEXT DEFAULT '',
  cover_url TEXT DEFAULT '',
  classification TEXT DEFAULT '',
  purchased_at TEXT DEFAULT '',
  tags TEXT DEFAULT '',              -- comma separated
  license TEXT DEFAULT '',
  terms TEXT DEFAULT '',             -- usage terms quoted from the description
  ai TEXT DEFAULT '',                -- AI disclosure
  info TEXT DEFAULT '{}',            -- other "More information" rows (json)
  hidden INTEGER DEFAULT 0,
  first_seen REAL,                   -- when a sync first saw it; NULL for the first import (backlog)
  uploads_fetched_at REAL,
  meta_fetched_at REAL,
  UNIQUE(store, store_id)
);
CREATE TABLE IF NOT EXISTS store_files(
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES store_items(id) ON DELETE CASCADE,
  upload_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  display_name TEXT DEFAULT '',
  size INTEGER,
  md5 TEXT,
  type TEXT DEFAULT '',
  updated_at TEXT DEFAULT '',
  match_file_id INTEGER,
  match_pack_id INTEGER,
  match_kind TEXT,                   -- exact | size | name | title
  UNIQUE(item_id, upload_id)
);
CREATE INDEX IF NOT EXISTS store_files_pack ON store_files(match_pack_id);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(rel, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS packs_fts USING fts5(name, grp, tags, vendor, notes, tokenize='unicode61');
"""


def connect(cfg) -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# Columns added after a release: (table, column, ALTER TABLE statement). New installs get them from SCHEMA.
MIGRATIONS: list[tuple[str, str, str]] = []


def init(cfg) -> None:
    conn = connect(cfg)
    conn.executescript(SCHEMA)
    for table, column, sql in MIGRATIONS:
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(sql)
    conn.commit()
    conn.close()


def placeholders(values) -> str:
    """'?,?,?' for an SQL IN (...) list; 'NULL' for an empty one, which matches nothing."""
    return ",".join("?" * len(values)) or "NULL"


def get_kv(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(conn, key: str, value) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, str(value)))


# Store metadata (titles, tags, authors) of purchases matched to a pack is searchable as part of the pack.
_PACK_FTS_SELECT = """
SELECT p.id, p.name, p.grp,
       p.tags || ' ' || p.auto_tags || ' ' || coalesce(s.tags, ''),
       p.vendor || ' ' || coalesce(s.authors, ''),
       p.notes || ' ' || coalesce(s.titles, '')
FROM packs p LEFT JOIN (
  SELECT sf.match_pack_id AS pack_id,
         group_concat(DISTINCT si.tags) AS tags,
         group_concat(DISTINCT si.author) AS authors,
         group_concat(DISTINCT si.title) AS titles
  FROM store_files sf JOIN store_items si ON si.id = sf.item_id
  WHERE sf.match_pack_id IS NOT NULL GROUP BY sf.match_pack_id
) s ON s.pack_id = p.id
"""


def refresh_pack_fts(conn: sqlite3.Connection, pack_id: int | None = None) -> None:
    cols = "INSERT INTO packs_fts(rowid, name, grp, tags, vendor, notes) "
    if pack_id is None:
        conn.execute("DELETE FROM packs_fts")
        conn.execute(cols + _PACK_FTS_SELECT)
    else:
        conn.execute("DELETE FROM packs_fts WHERE rowid=?", (pack_id,))
        conn.execute(cols + _PACK_FTS_SELECT + " WHERE p.id=?", (pack_id,))
