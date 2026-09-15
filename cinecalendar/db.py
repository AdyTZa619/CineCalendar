from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 4

MIGRATIONS: dict[int, str] = {
1: r'''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS movies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  imdb_id TEXT UNIQUE,
  identity_key TEXT NOT NULL,
  title TEXT NOT NULL,
  original_title TEXT,
  year INTEGER,
  title_type TEXT,
  runtime_min INTEGER,
  genres_json TEXT NOT NULL DEFAULT '[]',
  directors_json TEXT NOT NULL DEFAULT '[]',
  countries_json TEXT NOT NULL DEFAULT '[]',
  overview TEXT NOT NULL DEFAULT '',
  keywords_json TEXT NOT NULL DEFAULT '[]',
  semantic_json TEXT NOT NULL DEFAULT '{}',
  imdb_rating REAL,
  num_votes INTEGER,
  release_date TEXT,
  poster_url TEXT,
  source TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_movies_identity ON movies(identity_key);
CREATE INDEX IF NOT EXISTS ix_movies_year ON movies(year);
CREATE INDEX IF NOT EXISTS ix_movies_votes ON movies(num_votes);
CREATE TABLE IF NOT EXISTS ratings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  movie_id INTEGER NOT NULL UNIQUE REFERENCES movies(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
  date_rated TEXT,
  source TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path_name TEXT,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER,
  sha256 TEXT NOT NULL UNIQUE,
  row_count INTEGER NOT NULL DEFAULT 0,
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_profile(
  profile_key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendation_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  recommended_at TEXT NOT NULL,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL DEFAULT 'today',
  final_score REAL,
  ignored INTEGER NOT NULL DEFAULT 0,
  action TEXT
);
CREATE INDEX IF NOT EXISTS ix_rec_hist_movie_date ON recommendation_history(movie_id,recommended_at);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  weight REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feedback_movie ON feedback(movie_id);
CREATE TABLE IF NOT EXISTS watchlist(
  movie_id INTEGER PRIMARY KEY REFERENCES movies(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'want_to_watch',
  added_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata_cache(
  provider TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  expires_at TEXT,
  PRIMARY KEY(provider, cache_key)
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL,
  event_date TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(event_key,event_date)
);
''',
2: r'''
ALTER TABLE movies ADD COLUMN tmdb_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_movies_tmdb ON movies(tmdb_id);
''',
3: r'''
CREATE TABLE IF NOT EXISTS recommendation_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  candidate_count INTEGER NOT NULL,
  result_count INTEGER NOT NULL,
  engine_version TEXT NOT NULL
);
''',
4: r'''
ALTER TABLE movies ADD COLUMN title_norm TEXT;
ALTER TABLE movies ADD COLUMN original_title_norm TEXT;
CREATE INDEX IF NOT EXISTS ix_movies_title_norm_year_type ON movies(title_norm,year,title_type);
CREATE INDEX IF NOT EXISTS ix_movies_original_norm_year_type ON movies(original_title_norm,year,title_type);
'''
}

class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        # Keep hot B-tree/table pages in RAM and memory-map the read-mostly catalog. This is
        # especially important when CineCalendarData lives on a mechanical HDD: repeated
        # recommendation queries should not turn thousands of small reads into 100% active time.
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-32768")  # ~32 MiB per active connection, upper bound.
        try:
            con.execute("PRAGMA mmap_size=268435456")  # up to 256 MiB, silently capped by SQLite/OS.
        except sqlite3.DatabaseError:
            pass
        return con

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def migrate(self) -> None:
        con = sqlite3.connect(self.path)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            current = con.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
            from .util import utcnow_iso
            for version in range(current + 1, SCHEMA_VERSION + 1):
                con.executescript(MIGRATIONS[version])
                if version == 4:
                    from .util import normalize_text
                    rows=con.execute("SELECT id,title,original_title FROM movies").fetchall()
                    con.executemany("UPDATE movies SET title_norm=?,original_title_norm=? WHERE id=?", [
                        (normalize_text(r[1] or ""), normalize_text(r[2] or r[1] or ""), r[0]) for r in rows
                    ])
                con.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, utcnow_iso()))
                con.commit()
        finally:
            con.close()

    def get_setting(self, key: str, default=None):
        from .util import json_loads
        with self.connect() as con:
            row = con.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            return json_loads(row[0], default) if row else default

    def set_setting(self, key: str, value) -> None:
        from .util import json_dumps, utcnow_iso
        with self.tx() as con:
            con.execute("""INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                        (key, json_dumps(value), utcnow_iso()))
