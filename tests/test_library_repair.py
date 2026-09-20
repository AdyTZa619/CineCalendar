from __future__ import annotations

from cinecalendar.db import Database
from cinecalendar.library_repair import rated_library_health, repair_rated_library
from cinecalendar.util import utcnow_iso


def _rated_movie(db: Database, *, complete: bool) -> int:
    now = utcnow_iso()
    genres = '["Drama"]' if complete else "[]"
    directors = '["Director Test"]' if complete else "[]"
    runtime = 100 if complete else None
    imdb_rating = 7.4 if complete else None
    poster = "https://example.test/poster.jpg" if complete else None
    countries = '["Romania"]' if complete else "[]"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,runtime_min,genres_json,directors_json,countries_json,
                imdb_rating,num_votes,poster_url,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt5000001" if complete else "tt5000002",
                "complete" if complete else "incomplete",
                "Film complet" if complete else "Film incomplet",
                "Film complet" if complete else "Film incomplet",
                "film complet" if complete else "film incomplet",
                "film complet" if complete else "film incomplet",
                2020,
                "movie",
                runtime,
                genres,
                directors,
                countries,
                imdb_rating,
                1000 if complete else None,
                poster,
                "test",
                now,
                now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,8,'2026-09-20','test',?,?)""",
            (int(cur.lastrowid), now, now),
        )
        return int(cur.lastrowid)


def test_rated_library_health_counts_real_missing_fields(tmp_path):
    db = Database(tmp_path / "health.db")
    _rated_movie(db, complete=True)
    _rated_movie(db, complete=False)

    health = rated_library_health(db)
    assert health.total == 2
    assert health.complete == 1
    assert health.incomplete == 1
    assert health.missing_genres == 1
    assert health.missing_directors == 1
    assert health.missing_runtime == 1
    assert health.missing_imdb_rating == 1
    assert health.missing_poster == 1
    assert health.missing_countries == 1
    assert health.completion_percent == 50.0


def test_repair_pipeline_recalculates_health_and_profile(tmp_path, monkeypatch):
    db = Database(tmp_path / "repair.db")
    mid = _rated_movie(db, complete=False)

    def fake_imdb_backfill(_db, limit=5000):
        with _db.tx() as con:
            con.execute(
                """UPDATE movies SET runtime_min=95,genres_json='["Drama"]',
                   directors_json='["Director Repair"]',countries_json='["Romania"]',
                   imdb_rating=7.8,num_votes=1234,poster_url='https://example.test/repaired.jpg'
                   WHERE id=?""",
                (mid,),
            )
        return 1

    monkeypatch.setattr(
        "cinecalendar.library_repair.backfill_public_rating_metadata",
        fake_imdb_backfill,
    )

    class FakeOpenProvider:
        def __init__(self, _db):
            self.db = _db

        def enrich_by_imdb(self, movie):
            return movie

    monkeypatch.setattr(
        "cinecalendar.library_repair.OpenMovieMetadataProvider",
        FakeOpenProvider,
    )

    result = repair_rated_library(db, limit=20)
    assert result.before.incomplete == 1
    assert result.imdb_enriched == 1
    assert result.after.complete == 1
    assert result.after.incomplete == 0

    with db.connect() as con:
        profile = con.execute(
            "SELECT value_json FROM user_profile WHERE profile_key='main'"
        ).fetchone()
    assert profile is not None
