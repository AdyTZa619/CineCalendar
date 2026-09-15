from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v14 import FastRecommendationEngineV14
from cinecalendar.watch_intent import WatchIntentLearner


def _insert_movie(db: Database, *, title: str, genres: list[str], director: str, country: str,
                  year: int, runtime: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                title.lower(), title, title, year, "movie", runtime,
                json.dumps(genres), json.dumps([director]), json.dumps([country]),
                "", "[]", "{}", 7.2, 10000, "test", now, now,
                title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def test_watch_intent_learns_choice_vs_skip_without_changing_long_term_rating(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    positive_id = _insert_movie(
        db, title="Positive anchor", genres=["Action", "Thriller"], director="Director A",
        country="US", year=2022, runtime=105,
    )
    negative_id = _insert_movie(
        db, title="Negative anchor", genres=["Romance", "Drama"], director="Director B",
        country="FR", year=1972, runtime=175,
    )
    now = datetime.now(timezone.utc).isoformat()
    with db.tx() as con:
        for _ in range(6):
            con.execute(
                "INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score,action) "
                "VALUES(?,?,?,?,?,?)",
                (positive_id, now, now[:10], "today", 0.8, "chosen"),
            )
            con.execute(
                "INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score,action) "
                "VALUES(?,?,?,?,?,?)",
                (negative_id, now, now[:10], "today", 0.8, "skip_today"),
            )

    learner = WatchIntentLearner(db)
    status = learner.status()
    positive_candidate = Movie(
        id=101, title="Action candidate", original_title="Action candidate", year=2023,
        title_type="movie", runtime_min=108, genres=["Action", "Thriller"],
        directors=["Director A"], countries=["US"], imdb_rating=7.3, num_votes=12000,
    )
    negative_candidate = Movie(
        id=102, title="Romance candidate", original_title="Romance candidate", year=1973,
        title_type="movie", runtime_min=178, genres=["Romance", "Drama"],
        directors=["Director B"], countries=["FR"], imdb_rating=7.3, num_votes=12000,
    )

    positive = learner.score(positive_candidate)
    negative = learner.score(negative_candidate)

    assert status["active"] is True
    assert status["explicit_events"] == 12
    assert 0 < status["max_blend_weight"] <= 0.28
    assert positive["score"] > negative["score"]
    assert positive["blend_weight"] <= 0.28
    assert negative["blend_weight"] <= 0.28


def test_skip_today_forgets_much_faster_than_a_chosen_signal():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=15)).isoformat()
    skip_decay = WatchIntentLearner._decay(old, 2.5, now)
    chosen_decay = WatchIntentLearner._decay(old, 45.0, now)

    assert skip_decay < 0.03
    assert chosen_decay > 0.70


def test_recent_ratings_alone_can_only_have_small_influence():
    assert WatchIntentLearner._blend_cap(0, 20) == 0.06
    assert WatchIntentLearner._blend_cap(2, 0) == 0.10
    assert WatchIntentLearner._blend_cap(25, 0) == 0.28


class _FakeIntent:
    def status(self):
        return {"active": True}

    def score(self, movie):
        score = 0.90 if movie.id == 2 else 0.20
        return {
            "active": True,
            "score": score,
            "confidence": 0.8,
            "blend_weight": 0.22,
            "reason": "Semnal contextual de test.",
        }


def test_v14_intent_can_change_order_but_not_predicted_rating():
    engine = object.__new__(FastRecommendationEngineV14)
    engine.watch_intent = _FakeIntent()
    a = Recommendation(Movie(id=1, title="A"), ScoreBreakdown(final=0.72, predicted_rating=8.4, confidence=0.8))
    b = Recommendation(Movie(id=2, title="B"), ScoreBreakdown(final=0.66, predicted_rating=7.9, confidence=0.8))

    out = engine._apply_watch_intent([a, b])

    assert out[0].movie.id == 2
    assert a.score.predicted_rating == 8.4
    assert b.score.predicted_rating == 7.9
    assert any(name == "Intenție de vizionare acum" for name, _pts, _reason in b.score.contributions)
