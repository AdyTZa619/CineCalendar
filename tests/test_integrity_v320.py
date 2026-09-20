from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3
import zipfile

from cinecalendar import __version__
from cinecalendar.backup import export_profile, import_profile
from cinecalendar.db import Database, MIGRATIONS, SCHEMA_VERSION
from cinecalendar.decision_action_patch import record_decision_action
from cinecalendar.feedback import apply_feedback
from cinecalendar.trust_audit import build_trust_outcome_audit, record_trust_snapshot
from cinecalendar.util import utcnow_iso
from cinecalendar.watch_success_ui_patch import record_watch_event


def _movie(db: Database, imdb_id: str, title: str) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, imdb_id, title, title, 2024, "movie", 105,
                json.dumps(["Drama"]), json.dumps(["Director"]), json.dumps(["RO"]),
                "A sufficiently detailed premise for deterministic tests.", "[]", "{}",
                7.2, 20000, "test", now, now, title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def _trust_exposure(db: Database, movie_id: int, status: str = "trusted") -> int:
    now = utcnow_iso()
    today = date.today().isoformat()
    with db.tx() as con:
        run = con.execute(
            """INSERT INTO recommendation_runs(
                   context_date,slot,generated_at,candidate_count,result_count,engine_version
               ) VALUES(?,?,?,?,?,?)""",
            (today, "today", now, 24, 1, "16.0.0-top3-trust-gate"),
        )
        history = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score
               ) VALUES(?,?,?,?,?)""",
            (movie_id, now, today, "today", 0.82),
        )
        history_id = int(history.lastrowid)
        record_trust_snapshot(
            con,
            history_id=history_id,
            run_id=int(run.lastrowid),
            movie_id=movie_id,
            context_date=today,
            slot="today",
            rank_position=1,
            engine_version="16.0.0-top3-trust-gate",
            payload={
                "status": status,
                "trust": 0.78 if status == "trusted" else 0.55,
                "gate_score": 0.83,
                "supports": ["rating personal", "ALS", "calitate publică"],
                "red_flag": False,
                "gap": 0.0,
                "als": 0.9,
                "public_bayes": 7.1,
            },
            created_at=now,
        )
        return history_id


def test_v4_database_migrates_to_canonical_v6(tmp_path):
    path = tmp_path / "upgrade.db"
    con = sqlite3.connect(path)
    try:
        for version in range(1, 5):
            con.executescript(MIGRATIONS[version])
            con.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, utcnow_iso()),
            )
        con.commit()
    finally:
        con.close()

    db = Database(path)
    with db.connect() as con:
        current = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        columns = {row[1] for row in con.execute("PRAGMA table_info(recommendation_history)").fetchall()}
        trust = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_trust_audit'"
        ).fetchone()
        provenance = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_provenance'"
        ).fetchone()
    assert SCHEMA_VERSION == 6
    assert current == 6
    assert "exposure_history_id" in columns
    assert trust is not None
    assert provenance is not None


def test_watch_events_link_to_exact_exposure_and_exposure_stays_immutable(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db, "tt9400001", "Exact Exposure")
    exposure_id = _trust_exposure(db, movie_id, "trusted")
    with db.connect() as con:
        before = dict(con.execute("SELECT * FROM recommendation_history WHERE id=?", (exposure_id,)).fetchone())

    chosen_id = record_decision_action(db, movie_id, "chosen", exposure_id)
    stremio_id = record_watch_event(db, movie_id, "stremio_opened", exposure_id)
    playback_id = record_watch_event(db, movie_id, "playback_confirmed", exposure_id)
    watched_id = record_watch_event(db, movie_id, "watched", exposure_id)
    apply_feedback(db, movie_id, "seen")

    with db.connect() as con:
        root = con.execute(
            "SELECT action,recommended_at,final_score,exposure_history_id FROM recommendation_history WHERE id=?",
            (exposure_id,),
        ).fetchone()
        events = con.execute(
            """SELECT id,action,exposure_history_id FROM recommendation_history
               WHERE id IN (?,?,?,?) ORDER BY id""",
            (chosen_id, stremio_id, playback_id, watched_id),
        ).fetchall()
        seen_feedback = con.execute(
            "SELECT COUNT(*) FROM feedback WHERE movie_id=? AND kind='seen'", (movie_id,)
        ).fetchone()[0]

    assert root["action"] is None
    assert root["recommended_at"] == before["recommended_at"]
    assert root["final_score"] == before["final_score"]
    assert root["exposure_history_id"] is None
    assert [row["action"] for row in events] == ["chosen", "stremio_opened", "playback_confirmed", "watched"]
    assert all(int(row["exposure_history_id"]) == exposure_id for row in events)
    assert seen_feedback == 1

    audit = build_trust_outcome_audit(db, days=30)
    assert audit["by_status"]["trusted"]["watched"] == 1
    assert audit["by_status"]["trusted"]["confirmed_starts"] == 1
    assert audit["exact_linked_actions"] == 4
    assert audit["ambiguous_unlinked_actions"] == 0


def test_ambiguous_legacy_action_is_reported_not_guessed(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db, "tt9400002", "Ambiguous")
    _trust_exposure(db, movie_id, "trusted")
    _trust_exposure(db, movie_id, "backfill")
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action,exposure_history_id
               ) VALUES(?,?,?,?,?,?,?,NULL)""",
            (movie_id, now, date.today().isoformat(), "legacy", 0.8, 0, "stremio_opened"),
        )

    audit = build_trust_outcome_audit(db, days=30)
    assert audit["ambiguous_unlinked_actions"] == 1
    assert audit["by_status"]["trusted"]["stremio_attempts"] == 0
    assert audit["by_status"]["backfill"]["stremio_attempts"] == 0


def test_profile_backup_v2_is_compact_complete_and_idempotent(tmp_path):
    source = Database(tmp_path / "source.db")
    user_movie = _movie(source, "tt9500001", "User Movie")
    unused_catalog_movie = _movie(source, "tt9500002", "Unused Catalog Movie")
    now = utcnow_iso()
    with source.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (user_movie, 9, date.today().isoformat(), "test", now, now),
        )
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
            (user_movie, "more_like_this", 0.1, now),
        )
        con.execute(
            "INSERT INTO watchlist(movie_id,status,added_at,updated_at) VALUES(?,?,?,?)",
            (user_movie, "want_to_watch", now, now),
        )
    exposure_id = _trust_exposure(source, user_movie, "trusted")
    record_watch_event(source, user_movie, "watched", exposure_id)
    source.set_setting("theme", "dark")
    source.set_setting("tmdb_token", "must-not-leave-device")
    source.set_setting(
        "chosen_for_today_v1",
        {"date": date.today().isoformat(), "movie_id": user_movie},
    )

    archive = export_profile(source, tmp_path / "profile.zip")
    with zipfile.ZipFile(archive, "r") as fh:
        payload = json.loads(fh.read("profile.json").decode("utf-8"))
    assert payload["version"] == 2
    assert payload["app_version"] == __version__
    exported_movies = payload["tables"]["movies"]
    assert {row["id"] for row in exported_movies} == {user_movie}
    assert unused_catalog_movie not in {row["id"] for row in exported_movies}
    setting_keys = {row["key"] for row in payload["tables"]["settings"]}
    assert "theme" in setting_keys
    assert "tmdb_token" not in setting_keys
    assert "chosen_for_today_v1" not in setting_keys
    assert len(payload["tables"]["recommendation_trust_audit"]) == 1

    restored = Database(tmp_path / "restored.db")
    import_profile(restored, archive)
    with restored.connect() as con:
        counts_before = {
            "movies": con.execute("SELECT COUNT(*) FROM movies").fetchone()[0],
            "ratings": con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
            "feedback": con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
            "history": con.execute("SELECT COUNT(*) FROM recommendation_history").fetchone()[0],
            "runs": con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0],
            "trust": con.execute("SELECT COUNT(*) FROM recommendation_trust_audit").fetchone()[0],
        }
        linked = con.execute(
            """SELECT child.exposure_history_id,parent.id
               FROM recommendation_history child
               JOIN recommendation_history parent ON parent.id=child.exposure_history_id
               WHERE child.action='watched' LIMIT 1"""
        ).fetchone()
        trust_history = con.execute(
            "SELECT history_id FROM recommendation_trust_audit LIMIT 1"
        ).fetchone()[0]
    assert counts_before == {"movies": 1, "ratings": 1, "feedback": 1, "history": 2, "runs": 1, "trust": 1}
    assert linked is not None and int(linked[0]) == int(linked[1]) == int(trust_history)

    import_profile(restored, archive)
    with restored.connect() as con:
        counts_after = {
            "movies": con.execute("SELECT COUNT(*) FROM movies").fetchone()[0],
            "ratings": con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
            "feedback": con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
            "history": con.execute("SELECT COUNT(*) FROM recommendation_history").fetchone()[0],
            "runs": con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0],
            "trust": con.execute("SELECT COUNT(*) FROM recommendation_trust_audit").fetchone()[0],
        }
    assert counts_after == counts_before


def test_release_sources_do_not_reintroduce_stale_packaging_or_docs():
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts" / "build_windows.bat").read_text(encoding="utf-8").lower()
    portable = (root / "README_PORTABLE.txt").read_text(encoding="utf-8").lower()
    implemented = (root / "docs" / "IMPLEMENTED.md").read_text(encoding="utf-8").lower()
    schema = (root / "docs" / "SCHEMA.md").read_text(encoding="utf-8").lower()

    assert "--onedir" in build
    assert "--onefile" not in build
    assert "0.2.0" not in portable
    assert "updater declarat explicit ca dezactivat" not in implemented
    assert "versiunea curentă a schemei: **5**" in schema
    assert not (root / "releases" / "CineCalendar_LATEST.exe").exists()
