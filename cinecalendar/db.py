from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator


SCHEMA_VERSION = 8

# Kept as explicit SQL for documentation/tests. Database.migrate() applies v2-v8 through
# idempotent Python helpers so an interrupted ALTER TABLE can be resumed safely.
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
''',
5: r'''
ALTER TABLE recommendation_history
  ADD COLUMN exposure_history_id INTEGER REFERENCES recommendation_history(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_rec_hist_exposure ON recommendation_history(exposure_history_id);
CREATE INDEX IF NOT EXISTS ix_rec_hist_context_action ON recommendation_history(context_date,action);
CREATE TABLE IF NOT EXISTS recommendation_trust_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  history_id INTEGER NOT NULL UNIQUE REFERENCES recommendation_history(id) ON DELETE CASCADE,
  run_id INTEGER REFERENCES recommendation_runs(id) ON DELETE SET NULL,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL,
  rank_position INTEGER NOT NULL,
  engine_version TEXT NOT NULL,
  trust_status TEXT NOT NULL,
  trust_score REAL,
  gate_score REAL,
  support_count INTEGER NOT NULL DEFAULT 0,
  support_labels TEXT NOT NULL DEFAULT '[]',
  red_flag INTEGER NOT NULL DEFAULT 0,
  red_reason TEXT,
  score_gap REAL,
  als_score REAL,
  public_bayes REAL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rec_trust_date_status ON recommendation_trust_audit(context_date,trust_status);
CREATE INDEX IF NOT EXISTS ix_rec_trust_movie_date ON recommendation_trust_audit(movie_id,context_date);
''',
6: r'''
CREATE TABLE IF NOT EXISTS metadata_provenance(
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  provider TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(movie_id,field)
);
CREATE INDEX IF NOT EXISTS ix_metadata_provenance_provider ON metadata_provenance(provider);
''',
7: r'''
ALTER TABLE recommendation_history ADD COLUMN predicted_rating REAL;
ALTER TABLE recommendation_history ADD COLUMN confidence REAL;
CREATE TABLE IF NOT EXISTS recommendation_outcomes(
  exposure_history_id INTEGER PRIMARY KEY REFERENCES recommendation_history(id) ON DELETE CASCADE,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  rank_position INTEGER,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL,
  chosen_at TEXT,
  playback_at TEXT,
  watched_at TEXT,
  rating_id INTEGER REFERENCES ratings(id) ON DELETE SET NULL,
  actual_rating INTEGER,
  rating_date TEXT,
  predicted_rating REAL,
  confidence REAL,
  final_score REAL,
  engine_version TEXT,
  absolute_error REAL,
  resolved_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rec_outcome_movie ON recommendation_outcomes(movie_id);
CREATE INDEX IF NOT EXISTS ix_rec_outcome_rating_date ON recommendation_outcomes(rating_date);
''',
8: r'''
CREATE TABLE IF NOT EXISTS recommendation_explanations(
  history_id INTEGER PRIMARY KEY REFERENCES recommendation_history(id) ON DELETE CASCADE,
  personal_reason TEXT NOT NULL DEFAULT '',
  why_not TEXT NOT NULL DEFAULT '',
  score_factors_json TEXT NOT NULL DEFAULT '{}',
  contributions_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
'''
}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._preflight_existing_database()
        self.migrate()

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _table_exists(con: sqlite3.Connection, table: str) -> bool:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None

    @property
    def recovery_snapshot_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".last_good.bak")

    @staticmethod
    def _quick_check_path(path: Path) -> tuple[bool, str]:
        if not path.is_file() or path.stat().st_size <= 0:
            return True, "empty/new"
        con = None
        try:
            con = sqlite3.connect(path, timeout=10)
            con.execute("PRAGMA busy_timeout=3000")
            row = con.execute("PRAGMA quick_check").fetchone()
            text = str(row[0]) if row else "fără rezultat"
            return text.lower() == "ok", text
        except sqlite3.DatabaseError as exc:
            return False, str(exc)
        finally:
            if con is not None:
                con.close()

    def _preserve_corrupt_copy(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        preserved = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        try:
            shutil.copy2(self.path, preserved)
        except Exception:
            # Recovery must not be blocked only because the forensic copy could not be made.
            preserved = self.path
        return preserved

    def _restore_recovery_snapshot(self) -> bool:
        snapshot = self.recovery_snapshot_path
        healthy, _reason = self._quick_check_path(snapshot)
        if not healthy or not snapshot.is_file():
            return False
        self._preserve_corrupt_copy()
        self._restore_database_file(snapshot)
        healthy, _reason = self._quick_check_path(self.path)
        return healthy

    def _preflight_existing_database(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size <= 0:
            return
        healthy, reason = self._quick_check_path(self.path)
        if healthy:
            return
        if self._restore_recovery_snapshot():
            return
        raise RuntimeError(
            "Baza CineCalendar nu trece SQLite quick_check și nu există un snapshot last-good "
            f"valid pentru recuperare automată. Detaliu SQLite: {reason}"
        )

    def _refresh_recovery_snapshot(self, con: sqlite3.Connection) -> Path:
        snapshot = self.recovery_snapshot_path
        tmp = snapshot.with_suffix(snapshot.suffix + ".tmp")
        tmp.unlink(missing_ok=True)
        dst = sqlite3.connect(tmp)
        try:
            con.backup(dst)
            dst.commit()
            row = dst.execute("PRAGMA quick_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise RuntimeError("Snapshotul last-good nu trece SQLite quick_check.")
        finally:
            dst.close()
        os.replace(tmp, snapshot)
        return snapshot

    def _schema_needs_work(self, con: sqlite3.Connection) -> bool:
        current = con.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
        ).fetchone()[0]
        if int(current or 0) < SCHEMA_VERSION:
            return True
        if "tmdb_id" not in self._columns(con, "movies"):
            return True
        movie_cols = self._columns(con, "movies")
        if "title_norm" not in movie_cols or "original_title_norm" not in movie_cols:
            return True
        hist_cols = self._columns(con, "recommendation_history")
        if "exposure_history_id" not in hist_cols:
            return True
        if "predicted_rating" not in hist_cols or "confidence" not in hist_cols:
            return True
        if not self._table_exists(con, "recommendation_runs"):
            return True
        if not self._table_exists(con, "recommendation_trust_audit"):
            return True
        if not self._table_exists(con, "metadata_provenance"):
            return True
        if not self._table_exists(con, "recommendation_outcomes"):
            return True
        if not self._table_exists(con, "recommendation_explanations"):
            return True
        return False

    def _backup_database(self, con: sqlite3.Connection) -> Path:
        backup = self.path.with_name(self.path.stem + ".pre_migration.bak")
        tmp = backup.with_suffix(backup.suffix + ".tmp")
        tmp.unlink(missing_ok=True)
        dst = sqlite3.connect(tmp)
        try:
            con.backup(dst)
            dst.commit()
        finally:
            dst.close()
        os.replace(tmp, backup)
        return backup

    def _restore_database_file(self, backup: Path) -> None:
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".restore")
        shutil.copy2(backup, tmp)
        os.replace(tmp, self.path)

    @staticmethod
    def _mark(con: sqlite3.Connection, version: int) -> None:
        from .util import utcnow_iso
        con.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
            (int(version), utcnow_iso()),
        )

    def _apply_v2(self, con: sqlite3.Connection) -> None:
        if "tmdb_id" not in self._columns(con, "movies"):
            con.execute("ALTER TABLE movies ADD COLUMN tmdb_id INTEGER")
        con.execute("CREATE INDEX IF NOT EXISTS ix_movies_tmdb ON movies(tmdb_id)")

    def _apply_v3(self, con: sqlite3.Connection) -> None:
        con.execute(
            """CREATE TABLE IF NOT EXISTS recommendation_runs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              context_date TEXT NOT NULL,
              slot TEXT NOT NULL,
              generated_at TEXT NOT NULL,
              candidate_count INTEGER NOT NULL,
              result_count INTEGER NOT NULL,
              engine_version TEXT NOT NULL
            )"""
        )

    def _apply_v4(self, con: sqlite3.Connection) -> None:
        cols = self._columns(con, "movies")
        if "title_norm" not in cols:
            con.execute("ALTER TABLE movies ADD COLUMN title_norm TEXT")
        cols = self._columns(con, "movies")
        if "original_title_norm" not in cols:
            con.execute("ALTER TABLE movies ADD COLUMN original_title_norm TEXT")
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_movies_title_norm_year_type ON movies(title_norm,year,title_type)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_movies_original_norm_year_type ON movies(original_title_norm,year,title_type)"
        )
        from .util import normalize_text
        rows = con.execute(
            """SELECT id,title,original_title FROM movies
               WHERE title_norm IS NULL OR original_title_norm IS NULL"""
        ).fetchall()
        if rows:
            con.executemany(
                "UPDATE movies SET title_norm=?,original_title_norm=? WHERE id=?",
                [
                    (
                        normalize_text(row[1] or ""),
                        normalize_text(row[2] or row[1] or ""),
                        row[0],
                    )
                    for row in rows
                ],
            )

    def _apply_v5(self, con: sqlite3.Connection) -> None:
        if "exposure_history_id" not in self._columns(con, "recommendation_history"):
            con.execute(
                """ALTER TABLE recommendation_history
                   ADD COLUMN exposure_history_id INTEGER
                   REFERENCES recommendation_history(id) ON DELETE SET NULL"""
            )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_rec_hist_exposure ON recommendation_history(exposure_history_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_rec_hist_context_action ON recommendation_history(context_date,action)"
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS recommendation_trust_audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              history_id INTEGER NOT NULL UNIQUE REFERENCES recommendation_history(id) ON DELETE CASCADE,
              run_id INTEGER REFERENCES recommendation_runs(id) ON DELETE SET NULL,
              movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
              context_date TEXT NOT NULL,
              slot TEXT NOT NULL,
              rank_position INTEGER NOT NULL,
              engine_version TEXT NOT NULL,
              trust_status TEXT NOT NULL,
              trust_score REAL,
              gate_score REAL,
              support_count INTEGER NOT NULL DEFAULT 0,
              support_labels TEXT NOT NULL DEFAULT '[]',
              red_flag INTEGER NOT NULL DEFAULT 0,
              red_reason TEXT,
              score_gap REAL,
              als_score REAL,
              public_bayes REAL,
              created_at TEXT NOT NULL
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_rec_trust_date_status ON recommendation_trust_audit(context_date,trust_status)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_rec_trust_movie_date ON recommendation_trust_audit(movie_id,context_date)"
        )

    def _apply_v6(self, con: sqlite3.Connection) -> None:
        con.execute(
            """CREATE TABLE IF NOT EXISTS metadata_provenance(
              movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
              field TEXT NOT NULL,
              provider TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(movie_id,field)
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_metadata_provenance_provider ON metadata_provenance(provider)"
        )

    def _apply_v7(self, con: sqlite3.Connection) -> None:
        hist_cols = self._columns(con, "recommendation_history")
        if "predicted_rating" not in hist_cols:
            con.execute("ALTER TABLE recommendation_history ADD COLUMN predicted_rating REAL")
        hist_cols = self._columns(con, "recommendation_history")
        if "confidence" not in hist_cols:
            con.execute("ALTER TABLE recommendation_history ADD COLUMN confidence REAL")
        con.execute(
            """CREATE TABLE IF NOT EXISTS recommendation_outcomes(
              exposure_history_id INTEGER PRIMARY KEY
                REFERENCES recommendation_history(id) ON DELETE CASCADE,
              movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
              rank_position INTEGER,
              context_date TEXT NOT NULL,
              slot TEXT NOT NULL,
              chosen_at TEXT,
              playback_at TEXT,
              watched_at TEXT,
              rating_id INTEGER REFERENCES ratings(id) ON DELETE SET NULL,
              actual_rating INTEGER,
              rating_date TEXT,
              predicted_rating REAL,
              confidence REAL,
              final_score REAL,
              engine_version TEXT,
              absolute_error REAL,
              resolved_at TEXT,
              updated_at TEXT NOT NULL
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_rec_outcome_movie ON recommendation_outcomes(movie_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_rec_outcome_rating_date ON recommendation_outcomes(rating_date)"
        )

    def _apply_v8(self, con: sqlite3.Connection) -> None:
        con.execute(
            """CREATE TABLE IF NOT EXISTS recommendation_explanations(
              history_id INTEGER PRIMARY KEY
                REFERENCES recommendation_history(id) ON DELETE CASCADE,
              personal_reason TEXT NOT NULL DEFAULT '',
              why_not TEXT NOT NULL DEFAULT '',
              score_factors_json TEXT NOT NULL DEFAULT '{}',
              contributions_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL
            )"""
        )

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-32768")
        try:
            con.execute("PRAGMA mmap_size=268435456")
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
        existed = self.path.exists() and self.path.stat().st_size > 0
        backup_path: Path | None = None
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        closed = False
        try:
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )

            # v1 consists entirely of CREATE IF NOT EXISTS statements and is safe to reconcile.
            con.executescript(MIGRATIONS[1])
            self._mark(con, 1)
            con.commit()

            needs_schema_work = bool(existed and self._schema_needs_work(con))
            if needs_schema_work:
                backup_path = self._backup_database(con)

            # DDL and version marker live in the same explicit transaction. Helpers are idempotent,
            # so a database left half-migrated by an older release is safely reconciled.
            for version, fn in (
                (2, self._apply_v2),
                (3, self._apply_v3),
                (4, self._apply_v4),
                (5, self._apply_v5),
                (6, self._apply_v6),
                (7, self._apply_v7),
                (8, self._apply_v8),
            ):
                con.execute("BEGIN IMMEDIATE")
                try:
                    fn(con)
                    self._mark(con, version)
                    con.commit()
                except Exception:
                    con.rollback()
                    raise

            fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                raise RuntimeError(
                    f"Integritatea SQLite a eșuat după migrare: {len(fk_errors)} încălcări foreign-key."
                )
            quick = con.execute("PRAGMA quick_check").fetchone()
            if quick is None or str(quick[0]).lower() != "ok":
                raise RuntimeError(
                    f"SQLite quick_check a eșuat după migrare: {quick[0] if quick else 'fără rezultat'}"
                )
            con.execute("PRAGMA synchronous=NORMAL")
            con.commit()

            # Keep one known-good recovery image. It is refreshed after a schema repair/migration
            # and created once for existing installations. We intentionally do not copy the large
            # catalog on every startup.
            if needs_schema_work or not self.recovery_snapshot_path.is_file():
                self._refresh_recovery_snapshot(con)
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            con.close()
            closed = True
            if backup_path is not None and backup_path.exists():
                self._restore_database_file(backup_path)
            raise
        finally:
            if not closed:
                con.close()

    def get_setting(self, key: str, default=None):
        from .util import json_loads
        with self.connect() as con:
            row = con.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            return json_loads(row[0], default) if row else default

    def set_setting(self, key: str, value) -> None:
        from .util import json_dumps, utcnow_iso
        with self.tx() as con:
            con.execute(
                """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (key, json_dumps(value), utcnow_iso()),
            )
