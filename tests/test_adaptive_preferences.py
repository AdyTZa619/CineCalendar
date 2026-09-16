from __future__ import annotations

from datetime import date, timedelta
import inspect

from cinecalendar.adaptive_preferences import AdaptivePreferenceLearner
from cinecalendar.db import Database
from cinecalendar.models import Movie
from cinecalendar.recommender_v13 import FastRecommendationEngineV13
from cinecalendar.service import CineCalendarService
from cinecalendar.util import identity_key, json_dumps, utcnow_iso


def _insert_rated(db: Database, imdb_num: int, genre: str, rating: int, day: date) -> int:
    now = utcnow_iso()
    imdb_id = f"tt{imdb_num:07d}"
    title = f"{genre} sample {imdb_num}"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                 imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                 genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                 imdb_rating,num_votes,release_date,poster_url,source,created_at,updated_at,
                 title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, identity_key(title, title, 2018 + imdb_num % 7, "movie"), title, title,
                2018 + imdb_num % 7, "movie", 100, json_dumps([genre]), json_dumps([]),
                json_dumps(["United States"]), "", json_dumps([]), json_dumps({}), 7.0,
                20_000, None, None, "test", now, now, title.lower(), title.lower(),
            ),
        )
        movie_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
            (movie_id, rating, day.isoformat(), "test", now, now),
        )
        return movie_id


def _trained_db(tmp_path) -> Database:
    db = Database(tmp_path / "CineCalendarData" / "data" / "cinecalendar.db")
    start = date(2024, 1, 1)
    # Strong, consistent personal pattern: Horror is liked, Romance is disliked. The point is
    # not these genres specifically; it verifies that the model learns *this user's* evidence.
    for i in range(1, 71):
        _insert_rated(db, i, "Horror", 9 if i % 3 else 10, start + timedelta(days=i))
    for i in range(71, 141):
        _insert_rated(db, i, "Romance", 2 if i % 3 else 3, start + timedelta(days=i))
    return db


def test_adaptive_model_learns_real_personal_preference_direction(tmp_path):
    db = _trained_db(tmp_path)
    learner = AdaptivePreferenceLearner(db)
    horror = Movie(
        imdb_id="tt9000001", title="Unseen horror", year=2025, title_type="movie",
        runtime_min=100, genres=["Horror"], countries=["United States"],
        imdb_rating=7.0, num_votes=20_000,
    )
    romance = Movie(
        imdb_id="tt9000002", title="Unseen romance", year=2025, title_type="movie",
        runtime_min=100, genres=["Romance"], countries=["United States"],
        imdb_rating=7.0, num_votes=20_000,
    )

    hs = learner.score(horror)
    rs = learner.score(romance)
    status = learner.status()

    assert hs["active"] and rs["active"]
    assert hs["predicted_rating"] > rs["predicted_rating"] + 2.0
    assert hs["score"] > rs["score"]
    assert status["training_ratings"] == 140
    assert status["holdout_count"] >= 30
    assert status["model_mae"] is not None
    assert status["baseline_mae"] is not None


def test_new_explicit_feedback_invalidates_and_retrains_model(tmp_path):
    db = _trained_db(tmp_path)
    learner = AdaptivePreferenceLearner(db)
    learner.status()
    before = learner.state_token()

    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                 imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                 genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                 imdb_rating,num_votes,release_date,poster_url,source,created_at,updated_at,
                 title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9000100", "feedback-film", "Feedback film", "Feedback film", 2026, "movie", 95,
                json_dumps(["Crime"]), json_dumps([]), json_dumps([]), "", json_dumps([]),
                json_dumps({}), 7.2, 40_000, None, None, "test", now, now,
                "feedback film", "feedback film",
            ),
        )
        movie_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
            (movie_id, "more_like_this", 0.10, now),
        )

    after = learner.state_token()
    status = learner.status()
    assert after != before
    assert status["training_feedback"] == 1


def test_service_uses_v16_without_competing_with_first_recommendation():
    service_source = inspect.getsource(CineCalendarService.__init__)
    rerank_source = inspect.getsource(FastRecommendationEngineV13._adaptive_rerank)
    assert "FastRecommendationEngineV16" in service_source
    assert "collaborative.start_background()" in service_source
    assert "start_adaptive_background()" not in service_source
    assert "start_adaptive_background()" in rerank_source


def test_v13_never_waits_25_seconds_for_als():
    source = inspect.getsource(FastRecommendationEngineV13._wait_briefly_for_first_model)
    assert "start_background()" in source
    assert "sleep" not in source
    assert "monotonic" not in source


def test_v13_skips_synchronous_adaptive_training_until_background_ready():
    source = inspect.getsource(FastRecommendationEngineV13._adaptive_rerank)
    assert "if not self._adaptive_is_ready()" in source
    assert "self.start_adaptive_background()" in source
    # adaptive.score(), which can synchronously train the learner, is only reached after readiness.
    assert source.index("if not self._adaptive_is_ready()") < source.index("self.adaptive.score(rec.movie)")
