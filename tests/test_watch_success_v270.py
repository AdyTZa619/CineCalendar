from __future__ import annotations

from datetime import date
import inspect
import json

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v15 import FastRecommendationEngineV15
from cinecalendar.service import CineCalendarService
from cinecalendar.util import utcnow_iso
from cinecalendar.watch_success import _ACTION_SIGNALS, WatchSuccessIntentLearner
from cinecalendar.watch_success_ui_patch import (
    install_watch_success_ui_patch,
    record_watch_event,
    stremio_deep_link,
    stremio_web_link,
    trailer_search_url,
)


def _insert_movie(db: Database, title: str = "Watch Success", imdb_id: str = "tt9900001") -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, f"watch-success-{imdb_id}", title, title, 2025, "movie", 105,
                json.dumps(["Thriller"]), json.dumps(["Director X"]), json.dumps(["RO"]),
                "A clear premise with enough detail to make the movie easy to evaluate before play.",
                "[]", "{}", 7.5, 45000, "test", now, now, title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def test_confirmed_playback_is_stronger_than_stremio_handoff_or_choice():
    chosen_signal, _chosen_half_life, chosen_credit = _ACTION_SIGNALS["chosen"]
    launch_signal, _launch_half_life, launch_credit = _ACTION_SIGNALS["stremio_opened"]
    play_signal, _play_half_life, play_credit = _ACTION_SIGNALS["playback_confirmed"]
    watched_signal, _watched_half_life, watched_credit = _ACTION_SIGNALS["watched"]

    assert launch_signal > chosen_signal
    assert play_signal > launch_signal
    assert watched_signal >= play_signal
    assert play_credit > launch_credit > chosen_credit
    assert watched_credit > play_credit
    assert _ACTION_SIGNALS["play_opened"][0] == _ACTION_SIGNALS["stremio_opened"][0]


def test_watch_event_changes_intent_state_token(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _insert_movie(db)
    learner = WatchSuccessIntentLearner(db)
    before = learner.state_token()

    row_id = record_watch_event(db, movie_id, "stremio_opened")
    after = learner.state_token()

    assert row_id > 0
    assert after != before
    with db.connect() as con:
        row = con.execute("SELECT action,context_date,slot FROM recommendation_history WHERE id=?", (row_id,)).fetchone()
    assert row["action"] == "stremio_opened"
    assert row["context_date"] == date.today().isoformat()
    assert row["slot"] == "watch_success_v3"


def test_same_day_funnel_collapses_to_latest_outcome(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _insert_movie(db)
    now = utcnow_iso()
    today = date.today().isoformat()
    with db.tx() as con:
        for action in ("chosen", "trailer_opened", "stremio_opened", "playback_confirmed"):
            con.execute(
                """INSERT INTO recommendation_history(
                       movie_id,recommended_at,context_date,slot,final_score,ignored,action
                   ) VALUES(?,?,?,?,?,?,?)""",
                (movie_id, now, today, "test", 0.8, 0, action),
            )

    learner = WatchSuccessIntentLearner(db)
    status = learner.status()

    assert status["raw_action_events"] == 4
    assert status["collapsed_action_events"] == 1
    assert status["playback_events"] == 1
    assert status["launch_events"] == 0
    assert status["trailer_events"] == 0


def test_chosen_then_skip_same_day_ends_as_negative_funnel(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _insert_movie(db)
    now = utcnow_iso()
    today = date.today().isoformat()
    with db.tx() as con:
        for action in ("chosen", "skip_today"):
            con.execute(
                """INSERT INTO recommendation_history(
                       movie_id,recommended_at,context_date,slot,final_score,ignored,action
                   ) VALUES(?,?,?,?,?,?,?)""",
                (movie_id, now, today, "test", 0.8, 0, action),
            )

    status = WatchSuccessIntentLearner(db).status()
    assert status["collapsed_action_events"] == 1
    assert status["negative_evidence"] > 0
    assert status["positive_evidence"] == 0


def test_official_stremio_routes_and_trailer_search_are_deterministic():
    assert stremio_deep_link("tt0133093") == "stremio:///detail/movie/tt0133093/tt0133093"
    assert stremio_web_link("tt0133093") == "https://web.stremio.com/#/detail/movie/tt0133093/tt0133093"
    trailer = trailer_search_url("The Matrix", 1999)
    assert trailer.startswith("https://www.youtube.com/results?search_query=")
    assert "The+Matrix+1999+official+trailer" in trailer


def test_ui_records_handoff_separately_from_confirmed_playback_and_clears_watched_choice():
    source = inspect.getsource(install_watch_success_ui_patch)
    assert 'record_watch_event(self.db, int(movie.id), "stremio_opened")' in source
    assert 'record_watch_event(self.db, int(movie.id), "playback_confirmed")' in source
    assert 'clear_today_choice(self.db, int(movie.id))' in source
    assert 'record_watch_event(self.db, int(movie.id), "play_opened")' not in source


class _NeutralIntent:
    @staticmethod
    def _payload():
        return {"active": False, "score": 0.5, "confidence": 0.0, "blend_weight": 0.0, "reason": ""}

    def score_many(self, movies):
        return [self._payload() for _movie in movies]


class _ExtremeIntent:
    def score_many(self, movies):
        out = []
        for movie in movies:
            score = 0.05 if int(movie.id) == 1 else 0.95
            out.append({
                "active": True,
                "score": score,
                "confidence": 1.0,
                "blend_weight": 0.28,
                "reason": "Extreme test signal.",
            })
        return out


def _hard_to_start(final: float = 0.73) -> Recommendation:
    return Recommendation(
        Movie(
            id=1, title="Long sparse film", year=2020, runtime_min=210,
            genres=["Drama"], imdb_rating=6.8, num_votes=300,
        ),
        ScoreBreakdown(final=final, predicted_rating=8.5, confidence=0.80),
    )


def _easy_to_start(final: float = 0.72) -> Recommendation:
    return Recommendation(
        Movie(
            id=2, title="Easy strong film", year=2024, runtime_min=105,
            genres=["Thriller"], directors=["Director Y"], imdb_rating=7.6, num_votes=50000,
            overview="A focused premise with enough information to make the decision easy. " * 3,
            poster_url="https://example.invalid/poster.jpg",
        ),
        ScoreBreakdown(final=final, predicted_rating=8.2, confidence=0.80),
    )


def test_startability_can_break_an_extremely_close_tie_without_changing_predicted_rating():
    engine = object.__new__(FastRecommendationEngineV15)
    engine.watch_intent = _NeutralIntent()
    hard_to_start = _hard_to_start(0.725)
    easy_to_start = _easy_to_start(0.72)

    out = engine._apply_startability([hard_to_start, easy_to_start])

    assert out[0].movie.id == 2
    assert hard_to_start.score.predicted_rating == 8.5
    assert easy_to_start.score.predicted_rating == 8.2
    assert easy_to_start.score.startability > hard_to_start.score.startability
    assert any(name == "Startability" for name, _pts, _reason in easy_to_start.score.contributions)


def test_startability_cannot_rescue_a_substantially_weaker_taste_match():
    engine = object.__new__(FastRecommendationEngineV15)
    engine.watch_intent = _NeutralIntent()
    strong_but_long = _hard_to_start(0.84)
    easy_but_weaker = _easy_to_start(0.64)

    out = engine._apply_startability([strong_but_long, easy_but_weaker])

    assert out[0].movie.id == 1
    assert not any(name == "Startability" for name, _pts, _reason in easy_but_weaker.score.contributions)


def test_extreme_short_horizon_intent_cannot_overturn_large_long_term_taste_gap():
    engine = object.__new__(FastRecommendationEngineV15)
    engine.watch_intent = _ExtremeIntent()
    strong = _hard_to_start(0.86)
    weak = _easy_to_start(0.60)

    out = engine._apply_watch_success([strong, weak])

    assert out[0].movie.id == 1
    assert strong.score.final >= 0.7599
    assert weak.score.final <= 0.60 + 1e-9
    assert not any(name == "Intenție de vizionare acum" for name, _pts, _reason in weak.score.contributions)


def test_combined_short_horizon_shift_is_capped_to_ten_points():
    engine = object.__new__(FastRecommendationEngineV15)
    assert abs(engine._cap_to_base(0.80, 0.20) - 0.70) < 1e-12
    assert abs(engine._cap_to_base(0.80, 1.00) - 0.90) < 1e-12


def test_service_uses_v16_top3_gate_over_v15_watch_success():
    source = inspect.getsource(CineCalendarService.__init__)
    assert "FastRecommendationEngineV16" in source
