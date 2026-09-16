from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile

from . import __version__
from .db import Database
from .util import utcnow_iso


PROFILE_VERSION = 2
MAX_PROFILE_JSON_BYTES = 250 * 1024 * 1024
_TRANSIENT_OR_SECRET_SETTINGS = {
    "tmdb_token",
    "catalog_bootstrap_running",
    "chosen_for_today_v1",
}
_MOVIE_COLUMNS = {
    "imdb_id", "identity_key", "title", "original_title", "year", "title_type", "runtime_min",
    "genres_json", "directors_json", "countries_json", "overview", "keywords_json", "semantic_json",
    "imdb_rating", "num_votes", "release_date", "poster_url", "source", "created_at", "updated_at",
    "tmdb_id", "title_norm", "original_title_norm",
}


def _rows(con, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in con.execute(sql, tuple(params)).fetchall()]


def _referenced_movie_ids(con) -> list[int]:
    rows = con.execute(
        """SELECT movie_id FROM ratings
           UNION SELECT movie_id FROM feedback
           UNION SELECT movie_id FROM watchlist
           UNION SELECT movie_id FROM recommendation_history
           UNION SELECT movie_id FROM recommendation_trust_audit
           ORDER BY movie_id"""
    ).fetchall()
    return [int(row[0]) for row in rows]


def _movies_for_ids(con, movie_ids: list[int]) -> list[dict]:
    if not movie_ids:
        return []
    out: list[dict] = []
    for start in range(0, len(movie_ids), 700):
        chunk = movie_ids[start:start + 700]
        marks = ",".join("?" for _ in chunk)
        out.extend(_rows(con, f"SELECT * FROM movies WHERE id IN ({marks}) ORDER BY id", chunk))
    return out


def export_profile(db: Database, path: str | Path) -> Path:
    """Export portable user state, not the rebuildable IMDb catalog."""
    path = Path(path)
    exported_at = utcnow_iso()
    payload = {
        "format": "CineCalendarProfile",
        "version": PROFILE_VERSION,
        "app_version": __version__,
        "exported_at": exported_at,
        "tables": {},
    }
    with db.connect() as con:
        # One explicit read transaction pins a single WAL snapshot for every exported table.
        # Without it, a watcher/background worker could commit between SELECTs and create a
        # logically mixed backup assembled from two different moments in time.
        con.execute("BEGIN")
        try:
            movie_ids = _referenced_movie_ids(con)
            payload["tables"]["movies"] = _movies_for_ids(con, movie_ids)
            payload["tables"]["ratings"] = _rows(con, "SELECT * FROM ratings ORDER BY id")
            payload["tables"]["feedback"] = _rows(con, "SELECT * FROM feedback ORDER BY id")
            payload["tables"]["settings"] = _rows(
                con,
                "SELECT * FROM settings WHERE key NOT IN (?,?,?) ORDER BY key",
                tuple(sorted(_TRANSIENT_OR_SECRET_SETTINGS)),
            )
            payload["tables"]["recommendation_history"] = _rows(
                con, "SELECT * FROM recommendation_history ORDER BY id"
            )
            payload["tables"]["recommendation_runs"] = _rows(
                con, "SELECT * FROM recommendation_runs ORDER BY id"
            )
            payload["tables"]["recommendation_trust_audit"] = _rows(
                con, "SELECT * FROM recommendation_trust_audit ORDER BY id"
            )
            payload["tables"]["watchlist"] = _rows(con, "SELECT * FROM watchlist ORDER BY movie_id")
        finally:
            con.rollback()  # End the read snapshot; no user data was changed.

    manifest = {
        "format": payload["format"],
        "version": PROFILE_VERSION,
        "app_version": __version__,
        "exported_at": exported_at,
        "counts": {key: len(value) for key, value in payload["tables"].items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("profile.json", json.dumps(payload, ensure_ascii=False))
    return path


def _parse_time(value) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.fromisoformat(raw[:10])
        except ValueError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _backup_row_is_newer(existing, incoming: dict, *time_fields: str) -> bool:
    if existing is None:
        return True
    incoming_time = max((_parse_time(incoming.get(name)) for name in time_fields), default=0.0)
    existing_time = max((_parse_time(existing[name]) for name in time_fields if name in existing.keys()), default=0.0)
    # If neither side has useful timestamps, keep the current local value in merge mode.
    if incoming_time == 0.0 and existing_time == 0.0:
        return False
    return incoming_time >= existing_time


def _find_or_insert_movie(con, movie: dict) -> int:
    existing = None
    if movie.get("imdb_id"):
        existing = con.execute("SELECT * FROM movies WHERE imdb_id=?", (movie.get("imdb_id"),)).fetchone()
    if existing is None and movie.get("identity_key"):
        existing = con.execute(
            "SELECT * FROM movies WHERE identity_key=? ORDER BY id LIMIT 1",
            (movie.get("identity_key"),),
        ).fetchone()
    if existing is not None:
        # Merge only missing metadata. A profile backup must never downgrade a richer current catalog.
        updates = {}
        for key in _MOVIE_COLUMNS:
            if key not in movie or key not in existing.keys():
                continue
            current = existing[key]
            incoming = movie.get(key)
            if (current is None or current == "" or current == "[]" or current == "{}") and incoming not in (None, "", "[]", "{}"):
                updates[key] = incoming
        if updates:
            sets = ",".join(f"{key}=?" for key in updates)
            con.execute(f"UPDATE movies SET {sets} WHERE id=?", (*updates.values(), int(existing["id"])))
        return int(existing["id"])

    columns = [key for key in movie.keys() if key != "id" and key in _MOVIE_COLUMNS]
    if "identity_key" not in columns or "title" not in columns:
        raise ValueError("Backupul conține un film fără identity_key/title.")
    values = [movie[key] for key in columns]
    cur = con.execute(
        f"INSERT INTO movies({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        values,
    )
    return int(cur.lastrowid)


def _restore_runs(con, rows: list[dict]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for row in sorted(rows, key=lambda value: int(value.get("id") or 0)):
        existing = con.execute(
            """SELECT id FROM recommendation_runs
               WHERE context_date=? AND slot=? AND generated_at=? AND candidate_count=?
                 AND result_count=? AND engine_version=?
               ORDER BY id LIMIT 1""",
            (
                row.get("context_date"), row.get("slot"), row.get("generated_at"),
                int(row.get("candidate_count") or 0), int(row.get("result_count") or 0),
                row.get("engine_version"),
            ),
        ).fetchone()
        if existing is not None:
            new_id = int(existing[0])
        else:
            cur = con.execute(
                """INSERT INTO recommendation_runs(
                       context_date,slot,generated_at,candidate_count,result_count,engine_version
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    row.get("context_date"), row.get("slot"), row.get("generated_at"),
                    int(row.get("candidate_count") or 0), int(row.get("result_count") or 0),
                    row.get("engine_version"),
                ),
            )
            new_id = int(cur.lastrowid)
        if row.get("id") is not None:
            mapping[int(row["id"])] = new_id
    return mapping


def _restore_history(con, rows: list[dict], movie_map: dict[int, int]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    pending_parents: list[tuple[int, int]] = []
    for row in sorted(rows, key=lambda value: int(value.get("id") or 0)):
        old_movie = row.get("movie_id")
        if old_movie is None or int(old_movie) not in movie_map:
            continue
        movie_id = movie_map[int(old_movie)]
        old_parent = row.get("exposure_history_id")
        parent_id = mapping.get(int(old_parent)) if old_parent is not None else None

        existing = con.execute(
            """SELECT id FROM recommendation_history
               WHERE movie_id=? AND recommended_at=? AND context_date=? AND slot=?
                 AND COALESCE(final_score,-999999.0)=COALESCE(?,-999999.0)
                 AND ignored=? AND COALESCE(action,'')=COALESCE(?,'')
                 AND COALESCE(exposure_history_id,0)=COALESCE(?,0)
               ORDER BY id LIMIT 1""",
            (
                movie_id, row.get("recommended_at"), row.get("context_date"), row.get("slot", "today"),
                row.get("final_score"), int(row.get("ignored") or 0), row.get("action"), parent_id,
            ),
        ).fetchone()
        if existing is not None:
            new_id = int(existing[0])
        else:
            cur = con.execute(
                """INSERT INTO recommendation_history(
                       movie_id,recommended_at,context_date,slot,final_score,ignored,action,exposure_history_id
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    movie_id, row.get("recommended_at"), row.get("context_date"), row.get("slot", "today"),
                    row.get("final_score"), int(row.get("ignored") or 0), row.get("action"), parent_id,
                ),
            )
            new_id = int(cur.lastrowid)
        if row.get("id") is not None:
            old_id = int(row["id"])
            mapping[old_id] = new_id
            if old_parent is not None and parent_id is None:
                pending_parents.append((new_id, int(old_parent)))

    for new_id, old_parent in pending_parents:
        mapped_parent = mapping.get(old_parent)
        if mapped_parent is not None:
            con.execute(
                "UPDATE recommendation_history SET exposure_history_id=? WHERE id=? AND exposure_history_id IS NULL",
                (mapped_parent, new_id),
            )
    return mapping


def _restore_trust(con, rows: list[dict], movie_map: dict[int, int], history_map: dict[int, int], run_map: dict[int, int]) -> int:
    restored = 0
    for row in rows:
        old_history = row.get("history_id")
        old_movie = row.get("movie_id")
        if old_history is None or old_movie is None:
            continue
        history_id = history_map.get(int(old_history))
        movie_id = movie_map.get(int(old_movie))
        if history_id is None or movie_id is None:
            continue
        old_run = row.get("run_id")
        run_id = run_map.get(int(old_run)) if old_run is not None else None
        before = con.total_changes
        con.execute(
            """INSERT OR IGNORE INTO recommendation_trust_audit(
                   history_id,run_id,movie_id,context_date,slot,rank_position,engine_version,
                   trust_status,trust_score,gate_score,support_count,support_labels,red_flag,
                   red_reason,score_gap,als_score,public_bayes,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                history_id, run_id, movie_id, row.get("context_date"), row.get("slot", "today"),
                int(row.get("rank_position") or 1), row.get("engine_version", "unknown"),
                row.get("trust_status", "unclassified"), row.get("trust_score"), row.get("gate_score"),
                int(row.get("support_count") or 0), row.get("support_labels") or "[]",
                int(row.get("red_flag") or 0), row.get("red_reason"), row.get("score_gap"),
                row.get("als_score"), row.get("public_bayes"), row.get("created_at") or utcnow_iso(),
            ),
        )
        if con.total_changes > before:
            restored += 1
    return restored


def _load_payload(path: Path) -> dict:
    with zipfile.ZipFile(path, "r") as archive:
        try:
            info = archive.getinfo("profile.json")
        except KeyError as exc:
            raise ValueError("Backup incompatibil: profile.json lipsește.") from exc
        if info.file_size <= 0 or info.file_size > MAX_PROFILE_JSON_BYTES:
            raise ValueError("Backup incompatibil: profile.json are o dimensiune invalidă.")
        payload = json.loads(archive.read(info).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Backup incompatibil: structură invalidă.")
    return payload


def import_profile(db: Database, path: str | Path, mode: str = "merge") -> dict:
    """Import profile in safe merge mode by default.

    merge: newer local rating/settings/watchlist values win over an older backup.
    restore: backup values are authoritative for user state while the rebuildable movie catalog is kept.
    """
    mode = str(mode or "merge").strip().lower()
    if mode not in {"merge", "restore"}:
        raise ValueError("Mod backup necunoscut. Folosește 'merge' sau 'restore'.")

    path = Path(path)
    payload = _load_payload(path)
    version = int(payload.get("version") or 0)
    if payload.get("format") != "CineCalendarProfile" or version not in {1, PROFILE_VERSION}:
        raise ValueError("Backup incompatibil.")
    tables = payload.get("tables", {}) or {}
    if not isinstance(tables, dict):
        raise ValueError("Backup incompatibil: tables invalid.")

    stats = {
        "mode": mode,
        "movies": 0,
        "ratings": 0,
        "feedback": 0,
        "history": 0,
        "trust": 0,
        "conflicts_skipped": 0,
    }
    with db.tx() as con:
        if mode == "restore":
            con.execute("DELETE FROM recommendation_trust_audit")
            con.execute("DELETE FROM recommendation_history")
            con.execute("DELETE FROM recommendation_runs")
            con.execute("DELETE FROM feedback")
            con.execute("DELETE FROM watchlist")
            con.execute("DELETE FROM ratings")
            # Preserve local secrets/transient runtime state; replace all other settings from backup.
            marks = ",".join("?" for _ in _TRANSIENT_OR_SECRET_SETTINGS)
            con.execute(
                f"DELETE FROM settings WHERE key NOT IN ({marks})",
                tuple(sorted(_TRANSIENT_OR_SECRET_SETTINGS)),
            )

        movie_map: dict[int, int] = {}
        for movie in tables.get("movies", []):
            if not isinstance(movie, dict):
                continue
            new_id = _find_or_insert_movie(con, movie)
            if movie.get("id") is not None:
                movie_map[int(movie["id"])] = new_id
        stats["movies"] = len(movie_map)

        for rating in tables.get("ratings", []):
            if not isinstance(rating, dict):
                continue
            old_movie = rating.get("movie_id")
            if old_movie is None or int(old_movie) not in movie_map:
                continue
            movie_id = movie_map[int(old_movie)]
            existing = con.execute(
                "SELECT rating,date_rated,source,imported_at,updated_at FROM ratings WHERE movie_id=?",
                (movie_id,),
            ).fetchone()
            if mode == "merge" and existing is not None and not _backup_row_is_newer(existing, rating, "updated_at", "date_rated"):
                stats["conflicts_skipped"] += 1
                continue
            con.execute(
                """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(movie_id) DO UPDATE SET
                     rating=excluded.rating,date_rated=excluded.date_rated,source=excluded.source,
                     imported_at=excluded.imported_at,updated_at=excluded.updated_at""",
                (
                    movie_id, int(rating["rating"]), rating.get("date_rated"), rating.get("source", "backup"),
                    rating.get("imported_at") or utcnow_iso(), rating.get("updated_at") or utcnow_iso(),
                ),
            )
            stats["ratings"] += 1

        for feedback in tables.get("feedback", []):
            if not isinstance(feedback, dict):
                continue
            old_movie = feedback.get("movie_id")
            if old_movie is None or int(old_movie) not in movie_map:
                continue
            movie_id = movie_map[int(old_movie)]
            exists = con.execute(
                """SELECT 1 FROM feedback
                   WHERE movie_id=? AND kind=? AND weight=? AND created_at=? LIMIT 1""",
                (movie_id, feedback.get("kind"), feedback.get("weight"), feedback.get("created_at")),
            ).fetchone()
            if exists is None:
                con.execute(
                    "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
                    (movie_id, feedback.get("kind"), feedback.get("weight"), feedback.get("created_at") or utcnow_iso()),
                )
                stats["feedback"] += 1

        for setting in tables.get("settings", []):
            if not isinstance(setting, dict):
                continue
            key = str(setting.get("key") or "")
            if not key or key in _TRANSIENT_OR_SECRET_SETTINGS:
                continue
            existing = con.execute(
                "SELECT value_json,updated_at FROM settings WHERE key=?", (key,)
            ).fetchone()
            if mode == "merge" and existing is not None and not _backup_row_is_newer(existing, setting, "updated_at"):
                stats["conflicts_skipped"] += 1
                continue
            con.execute(
                """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (key, setting.get("value_json", "null"), setting.get("updated_at") or utcnow_iso()),
            )

        for watch in tables.get("watchlist", []):
            if not isinstance(watch, dict):
                continue
            old_movie = watch.get("movie_id")
            if old_movie is None or int(old_movie) not in movie_map:
                continue
            movie_id = movie_map[int(old_movie)]
            existing = con.execute(
                "SELECT status,added_at,updated_at FROM watchlist WHERE movie_id=?", (movie_id,)
            ).fetchone()
            if mode == "merge" and existing is not None and not _backup_row_is_newer(existing, watch, "updated_at", "added_at"):
                stats["conflicts_skipped"] += 1
                continue
            con.execute(
                """INSERT INTO watchlist(movie_id,status,added_at,updated_at) VALUES(?,?,?,?)
                   ON CONFLICT(movie_id) DO UPDATE SET
                     status=excluded.status,added_at=excluded.added_at,updated_at=excluded.updated_at""",
                (
                    movie_id, watch.get("status", "want_to_watch"),
                    watch.get("added_at") or utcnow_iso(), watch.get("updated_at") or utcnow_iso(),
                ),
            )

        run_map = _restore_runs(con, list(tables.get("recommendation_runs", [])))
        history_map = _restore_history(con, list(tables.get("recommendation_history", [])), movie_map)
        stats["history"] = len(history_map)
        stats["trust"] = _restore_trust(
            con,
            list(tables.get("recommendation_trust_audit", [])),
            movie_map,
            history_map,
            run_map,
        )

    from .profile import build_profile
    build_profile(db)
    return stats
