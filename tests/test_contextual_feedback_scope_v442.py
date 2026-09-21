from __future__ import annotations

import json

from cinecalendar.db import Database
from cinecalendar.feedback import apply_feedback
from cinecalendar.models import Movie
from cinecalendar.util import utcnow_iso
from cinecalendar.watch_success import WatchSuccessIntentLearner, feedback_feature_vector


def _movie() -> Movie:
    return Movie(
        id=1,
        title="Scoped feedback",
        year=2024,
        runtime_min=165,
        genres=["Drama", "History"],
        directors=["Director Test"],
        countries=["Romania"],
        semantic={"history": 1.0, "faith": 0.8},
        num_votes=25_000,
    )


def _insert_movie(db: Database) -> int:
    movie = _movie()
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9442001", "scoped-feedback", movie.title, movie.title,
                "scoped feedback", "scoped feedback", movie.year, "movie", movie.runtime_min,
                json.dumps(movie.genres), json.dumps(movie.directors), json.dumps(movie.countries),
                "Historical drama", "[]", json.dumps(movie.semantic), 7.4, movie.num_votes,
                "test", now, now,
            ),
        )
        return int(cur.lastrowid)


def test_not_now_is_exact_session_feedback_not_similarity_training(tmp_path):
    movie = _movie()
    assert feedback_feature_vector("not_now", movie) == {}

    db = Database(tmp_path / "not-now.db")
    movie_id = _insert_movie(db)
    apply_feedback(db, movie_id, "not_now")

    status = WatchSuccessIntentLearner(db).status()
    assert status["explicit_events"] == 0
    assert status["negative_evidence"] == 0.0
    assert status["active"] is False


def test_too_long_trains_runtime_only():
    scoped = feedback_feature_vector("too_long", _movie())
    assert scoped == {"runtime:>150": 0.55}


def test_mood_feedback_cannot_penalize_runtime_director_country_or_popularity():
    scoped = feedback_feature_vector("mood_mismatch", _movie())
    assert scoped
    assert all(
        token.startswith(("genre:", "combo:genre:", "theme:"))
        for token in scoped
    )
    assert not any(
        token.startswith(("runtime:", "director:", "country:", "decade:", "popularity:"))
        for token in scoped
    )


def test_too_similar_targets_content_identity_not_incidental_metadata():
    scoped = feedback_feature_vector("too_similar", _movie())
    assert any(token.startswith("genre:") for token in scoped)
    assert any(token.startswith("director:") for token in scoped)
    assert not any(
        token.startswith(("runtime:", "country:", "decade:", "popularity:"))
        for token in scoped
    )


def test_explicit_similarity_feedback_still_uses_the_full_feature_vector():
    scoped = feedback_feature_vector("never_similar", _movie())
    assert "runtime:>150" in scoped
    assert "country:romania" in scoped
    assert "director:director test" in scoped
