from __future__ import annotations

from cinecalendar.db import Database
from cinecalendar.imdb_rating_followup import (
    audit_imdb_rating_followups,
    begin_imdb_followup_check,
    cancel_imdb_rating_followup,
    due_imdb_rating_followups,
    pending_imdb_rating_followups,
    queue_imdb_rating_followup,
    recent_imdb_rating_followups,
    record_imdb_followup_failure,
    resolve_imdb_rating_followups,
)
from cinecalendar.models import Movie
from cinecalendar.util import json_dumps, utcnow_iso


def _insert_movie(db: Database, imdb_id: str, title: str) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,genres_json,directors_json,countries_json,keywords_json,
                   source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, f"movie|{title.lower()}|2020|movie", title, title,
                title.lower(), title.lower(), 2020, "movie", "[]", "[]", "[]", "[]",
                "test", now, now,
            ),
        )
        return int(cur.lastrowid)


def test_followup_waits_for_real_imdb_rating_then_resolves(tmp_path):
    db = Database(tmp_path / "followup.db")
    movie_id = _insert_movie(db, "tt9000001", "Film test")
    movie = Movie(id=movie_id, imdb_id="tt9000001", title="Film test")

    assert queue_imdb_rating_followup(db, movie) is True
    assert pending_imdb_rating_followups(db)[0]["title"] == "Film test"
    resolved, pending = resolve_imdb_rating_followups(db)
    assert resolved == []
    assert len(pending) == 1

    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, 9, "2026-09-23", "imdb_public_sync", now, now),
        )

    resolved, pending = resolve_imdb_rating_followups(db)
    assert [(item.title, item.rating) for item in resolved] == [("Film test", 9)]
    assert pending == []
    assert pending_imdb_rating_followups(db) == []


def test_followup_is_deduplicated_and_requires_imdb_identity(tmp_path):
    db = Database(tmp_path / "dedupe.db")
    movie_id = _insert_movie(db, "tt9000002", "Primul titlu")
    assert queue_imdb_rating_followup(
        db, Movie(id=movie_id, imdb_id="tt9000002", title="Primul titlu"),
    )
    assert queue_imdb_rating_followup(
        db, Movie(id=movie_id, imdb_id="tt9000002", title="Titlu actualizat"),
    )
    assert not queue_imdb_rating_followup(db, Movie(id=999, title="Fără IMDb"))

    pending = pending_imdb_rating_followups(db)
    assert len(pending) == 1
    assert pending[0]["title"] == "Titlu actualizat"
    assert cancel_imdb_rating_followup(db, movie_id) is True
    assert pending_imdb_rating_followups(db) == []


def test_ui_schedules_two_fast_checks_and_keeps_regular_sync_as_fallback():
    ui = open("cinecalendar/qt_ui.py", encoding="utf-8").read()
    watch = open("cinecalendar/watch_success_ui_patch.py", encoding="utf-8").read()

    assert "2 * 60 * 1000" in ui
    assert "10 * 60 * 1000" in ui
    assert "self.imdb_sync_timer.start(30 * 60 * 1000)" in ui
    assert "queue_imdb_rating_followup_values(" in ui
    assert "L-am văzut • nota din IMDb" in watch
    assert '"Verifică IMDb acum"' in ui
    assert "begin_imdb_followup_check" in ui
    assert "record_imdb_followup_failure" in ui


def test_followup_records_attempt_backoff_and_provider_failure(tmp_path):
    db = Database(tmp_path / "backoff.db")
    movie_id = _insert_movie(db, "tt9000003", "Backoff")
    assert queue_imdb_rating_followup(
        db, Movie(id=movie_id, imdb_id="tt9000003", title="Backoff"),
    )

    assert due_imdb_rating_followups(db) == []
    checked = begin_imdb_followup_check(db, force=True)
    assert checked == {movie_id}
    assert record_imdb_followup_failure(db, checked, "IMDb timeout") == 1

    item = pending_imdb_rating_followups(db)[0]
    assert item["status"] == "profile_unavailable"
    assert item["attempt_count"] == 1
    assert item["last_checked_at"]
    assert item["next_check_at"] > item["last_checked_at"]
    assert item["last_error"] == "IMDb timeout"


def test_followup_requires_exact_remote_imdb_identity(tmp_path):
    db = Database(tmp_path / "identity.db")
    movie_id = _insert_movie(db, "tt9000004", "Identity")
    assert queue_imdb_rating_followup(
        db, Movie(id=movie_id, imdb_id="tt9000004", title="Identity"),
    )
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, 8, "2026-09-23", "imdb_public_sync", now, now),
        )

    resolved, pending = resolve_imdb_rating_followups(
        db,
        profile_ratings={"tt9999999": 8},
        checked_movie_ids={movie_id},
    )
    assert resolved == []
    assert pending[0]["status"] == "rating_not_found"

    resolved, pending = resolve_imdb_rating_followups(
        db,
        profile_ratings={"tt9000004": 8},
        checked_movie_ids={movie_id},
    )
    assert [(item.imdb_id, item.rating) for item in resolved] == [("tt9000004", 8)]
    assert pending == []
    history = recent_imdb_rating_followups(db)
    assert history[0]["status"] == "found"
    assert history[0]["matched_imdb_id"] == "tt9000004"


def test_identity_conflict_requires_manual_retry(tmp_path):
    db = Database(tmp_path / "identity-conflict.db")
    movie_id = _insert_movie(db, "tt9000007", "Conflict")
    assert queue_imdb_rating_followup(
        db, Movie(id=movie_id, imdb_id="tt9000007", title="Conflict"),
    )
    with db.tx() as con:
        con.execute("UPDATE movies SET imdb_id='tt9000008' WHERE id=?", (movie_id,))

    resolve_imdb_rating_followups(
        db,
        profile_ratings={"tt9000007": 8},
        checked_movie_ids={movie_id},
    )

    assert pending_imdb_rating_followups(db)[0]["status"] == "identity_mismatch"
    assert due_imdb_rating_followups(db) == []
    assert begin_imdb_followup_check(db, force=False) == set()
    assert begin_imdb_followup_check(db, force=True) == {movie_id}


def test_audit_recovers_watched_movie_missing_from_queue(tmp_path):
    db = Database(tmp_path / "audit.db")
    movie_id = _insert_movie(db, "tt9000005", "Recovered")
    with db.tx() as con:
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
            (movie_id, "seen", 1.0, utcnow_iso()),
        )

    report = audit_imdb_rating_followups(db, recover=True)

    assert report.recovered == 1
    assert report.active == 1
    assert report.watched_without_rating == 1
    assert pending_imdb_rating_followups(db)[0]["movie_id"] == movie_id


def test_schema_v10_migrates_the_legacy_json_queue(tmp_path):
    path = tmp_path / "legacy-followup.db"
    db = Database(path)
    movie_id = _insert_movie(db, "tt9000006", "Legacy queue")
    queued_at = utcnow_iso()
    with db.tx() as con:
        con.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
            (
                "imdb_rating_followups_v1",
                json_dumps([{
                    "movie_id": movie_id,
                    "imdb_id": "tt9000006",
                    "title": "Legacy queue",
                    "queued_at": queued_at,
                }]),
                queued_at,
            ),
        )
        con.execute("DROP TABLE imdb_rating_followups")
        con.execute("DELETE FROM schema_migrations WHERE version=10")

    migrated = Database(path)
    pending = pending_imdb_rating_followups(migrated)
    assert len(pending) == 1
    assert pending[0]["movie_id"] == movie_id
    assert pending[0]["imdb_id"] == "tt9000006"
    assert pending[0]["attempt_count"] == 0
