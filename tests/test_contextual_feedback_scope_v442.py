from __future__ import annotations

import json
from datetime import date, timedelta, timezone
from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.feedback import (
    apply_feedback,
    apply_feedback_with_receipt,
    daily_contextual_feedback,
    undo_feedback,
)
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.personalization_v41 import PersonalizationBrainV41
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


def _candidate(movie_id: int, title: str, genre: str, director: str, final: float) -> Recommendation:
    return Recommendation(
        Movie(
            id=movie_id,
            title=title,
            year=2024,
            runtime_min=100,
            genres=[genre],
            directors=[director],
            countries=["Romania"],
            semantic={genre.casefold(): 1.0},
            num_votes=20_000,
        ),
        ScoreBreakdown(final=final, predicted_rating=8.0, confidence=0.8),
    )


def test_too_long_immediately_moves_to_the_next_shorter_runtime_band():
    rejected = _movie()
    assert PersonalizationBrainV41.contextual_runtime_max([("too_long", rejected)]) == 120
    rejected.runtime_min = 110
    assert PersonalizationBrainV41.contextual_runtime_max([("too_long", rejected)]) == 90
    rejected.runtime_min = 80
    assert PersonalizationBrainV41.contextual_runtime_max([("too_long", rejected)]) == 60


def test_too_similar_immediately_prefers_a_distinct_good_alternative():
    rejected = Movie(
        id=10,
        title="Rejected",
        genres=["Drama"],
        directors=["Same Director"],
        semantic={"history": 1.0},
    )
    very_similar = _candidate(11, "Very similar", "Drama", "Same Director", 0.90)
    distinct = _candidate(12, "Distinct", "Comedy", "Other Director", 0.87)

    selected = PersonalizationBrainV41.apply_contextual_session(
        [very_similar, distinct],
        [("too_similar", rejected)],
        2,
    )

    assert selected[0].movie.id == 12
    assert selected[0].score.final == 0.87
    assert any(name == "Motivul ales acum" for name, _points, _reason in selected[0].score.contributions) is False


def test_session_feedback_resolves_only_explicit_current_receipts(tmp_path):
    db = Database(tmp_path / "session-context.db")
    movie_id = _insert_movie(db)
    brain = PersonalizationBrainV41(db)

    context = brain.resolve_contextual_session(
        [("too_long", movie_id), ("not_now", movie_id), ("invalid", movie_id), ("too_similar", -1)]
    )

    assert [kind for kind, _movie_value in context] == ["too_long", "not_now"]


def test_daily_context_survives_restart_boundary_and_expires_next_local_day(tmp_path):
    db = Database(tmp_path / "daily-context.db")
    movie_id = _insert_movie(db)
    _profile, receipt = apply_feedback_with_receipt(db, movie_id, "too_long")
    with db.tx() as con:
        # 21:30 UTC is already 00:30 on 22 September in Romania (UTC+3).
        con.execute(
            "UPDATE feedback SET created_at=? WHERE id=?",
            ("2026-09-21T21:30:00+00:00", receipt.feedback_id),
        )

    romania_summer = timezone(timedelta(hours=3))
    assert daily_contextual_feedback(
        db, on_date=date(2026, 9, 22), local_tz=romania_summer
    ) == (("too_long", movie_id),)
    assert daily_contextual_feedback(
        db, on_date=date(2026, 9, 23), local_tz=romania_summer
    ) == ()


def test_daily_context_deduplicates_repeated_clicks_and_undo_removes_last_reason(tmp_path):
    db = Database(tmp_path / "daily-context-undo.db")
    movie_id = _insert_movie(db)
    _profile, first = apply_feedback_with_receipt(db, movie_id, "too_similar")
    _profile, second = apply_feedback_with_receipt(db, movie_id, "too_similar")

    assert daily_contextual_feedback(db) == (("too_similar", movie_id),)
    undo_feedback(db, second.feedback_id)
    assert daily_contextual_feedback(db) == (("too_similar", movie_id),)
    undo_feedback(db, first.feedback_id)
    assert daily_contextual_feedback(db) == ()


def test_daily_context_excludes_rejected_title_after_ui_restart():
    premium = (
        Path(__file__).resolve().parents[1] / "cinecalendar" / "premium_ui.py"
    ).read_text(encoding="utf-8")
    assert "contextual_exclusions = {movie_id for _kind, movie_id in contextual_feedback}" in premium
    assert "exclude_ids = set(self.session_skips) | contextual_exclusions" in premium
