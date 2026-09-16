from __future__ import annotations

from datetime import date
import json

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v15 import FastRecommendationEngineV15
from cinecalendar.service import CineCalendarService
from cinecalendar.util import utcnow_iso
from cinecalendar.watch_success import _ACTION_SIGNALS, WatchSuccessIntentLearner
from cinecalendar.watch_success_ui_patch import (
    record_watch_event,
    stremio_deep_link,
    stremio_web_link,
    trailer_search_url,
)


def _insert_movie(db: Database, title: str = "Watch Success") -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9900001", "watch-success", title, title, 2025, "movie", 105,
                json.dumps(["Thriller"]), json.dumps(["Director X"]), json.dumps(["RO"]),
                "A clear premise with enough detail to make the movie easy to evaluate before play.",
                "[]", "{}", 7.5, 45000, "test", now, now, title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def test_play_is_stronger_evidence_than_merely_choosing():
    chosen_signal, _chosen_half_life, chosen_credit = _ACTION_SIGNALS["chosen"]
    play_signal, _play_half_life, play_credit = _ACTION_SIGNALS["play_opened"]
    assert play_signal > chosen_signal
    assert play_credit > chosen_credit


def test_watch_event_changes_intent_state_token(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _insert_movie(db)
    learner = WatchSuccessIntentLearner(db)
    before = learner.state_token()

    row_id = record_watch_event(db, movie_id, "play_opened")
    after = learner.state_token()

    assert row_id > 0
    assert after != before
    with db.connect() as con:
        row = con.execute("SELECT action,context_date FROM recommendation_history WHERE id=?", (row_id,)).fetchone()
    assert row["action"] == "play_opened"
    assert row["context_date"] == date.today().isoformat()


def test_official_stremio_routes_and_trailer_search_are_deterministic():
    assert stremio_deep_link("tt0133093") == "stremio:///detail/movie/tt0133093/tt0133093"
    assert stremio_web_link("tt0133093") == "https://web.stremio.com/#/detail/movie/tt0133093/tt0133093"
    trailer = trailer_search_url("The Matrix", 1999)
    assert trailer.startswith("https://www.youtube.com/results?search_query=")
    assert "The+Matrix+1999+official+trailer" in trailer


class _NeutralIntent:
    def status(self):
        return {"active": False}

    def score(self, movie):
        return {"active": False, "score": 0.5, "confidence": 0.0, "blend_weight": 0.0, "reason": ""}


def test_startability_can_reorder_close_candidates_without_changing_predicted_rating():
    engine = object.__new__(FastRecommendationEngineV15)
    engine.watch_intent = _NeutralIntent()

    hard_to_start = Recommendation(
        Movie(
            id=1, title="Long sparse film", year=2020, runtime_min=210,
            genres=["Drama"], imdb_rating=6.8, num_votes=300,
        ),
        ScoreBreakdown(final=0.73, predicted_rating=8.5, confidence=0.80),
    )
    easy_to_start = Recommendation(
        Movie(
            id=2, title="Easy strong film", year=2024, runtime_min=105,
            genres=["Thriller"], directors=["Director Y"], imdb_rating=7.6, num_votes=50000,
            overview="A focused premise with enough information to make the decision easy. " * 3,
            poster_url="https://example.invalid/poster.jpg",
        ),
        ScoreBreakdown(final=0.72, predicted_rating=8.2, confidence=0.80),
    )

    out = engine._apply_startability([hard_to_start, easy_to_start])

    assert out[0].movie.id == 2
    assert hard_to_start.score.predicted_rating == 8.5
    assert easy_to_start.score.predicted_rating == 8.2
    assert easy_to_start.score.startability > hard_to_start.score.startability
    assert any(name == "Startability" for name, _pts, _reason in easy_to_start.score.contributions)


def test_service_uses_v15_watch_success_engine():
    import inspect

    source = inspect.getsource(CineCalendarService.__init__)
    assert "FastRecommendationEngineV15" in source
