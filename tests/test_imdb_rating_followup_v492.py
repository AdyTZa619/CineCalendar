from __future__ import annotations

from cinecalendar.db import Database
from cinecalendar.imdb_rating_followup import (
    cancel_imdb_rating_followup,
    pending_imdb_rating_followups,
    queue_imdb_rating_followup,
    resolve_imdb_rating_followups,
)
from cinecalendar.models import Movie
from cinecalendar.util import utcnow_iso


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
