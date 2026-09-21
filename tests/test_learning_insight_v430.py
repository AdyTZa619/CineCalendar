from __future__ import annotations

from datetime import date, timedelta

from cinecalendar.backup import PROFILE_VERSION, export_profile, import_profile
from cinecalendar.db import Database, SCHEMA_VERSION
from cinecalendar.learning_insight_v43 import (
    PredictionCalibratorV43,
    TextSemanticBrainV43,
    comparison_reason,
)
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommendation_history_v43 import (
    history_engine_versions,
    recommendation_history_rows,
    record_explanation_snapshot,
)
from cinecalendar.recommendation_outcomes_v42 import recommendation_performance
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


def _movie(
    db: Database,
    idx: int,
    title: str,
    *,
    overview: str = "",
    keywords: list[str] | None = None,
    genres: list[str] | None = None,
    title_type: str = "movie",
) -> int:
    now = utcnow_iso()
    genres = genres or ["Drama"]
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{9900000+idx:07d}",
                identity_key(title, title, 2025, title_type),
                title,
                title,
                normalize_text(title),
                normalize_text(title),
                2025,
                title_type,
                105,
                json_dumps(genres),
                json_dumps(["Director"]),
                json_dumps(["Romania"]),
                overview,
                json_dumps(keywords or []),
                "{}",
                7.2,
                15000,
                "test",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def _root_exposure(
    db: Database,
    movie_id: int,
    *,
    day: str,
    predicted: float = 7.0,
    confidence: float = .7,
    final: float = .8,
    engine: str = "test-engine",
    rank: int = 1,
) -> int:
    stamp = day + "T18:00:00+00:00"
    with db.tx() as con:
        run = con.execute(
            """INSERT INTO recommendation_runs(
                   context_date,slot,generated_at,candidate_count,result_count,engine_version
               ) VALUES(?,?,?,?,?,?)""",
            (day, "decision", stamp, 20, 3, engine),
        )
        hist = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,
                   predicted_rating,confidence
               ) VALUES(?,?,?,?,?,?,?)""",
            (movie_id, stamp, day, "decision", final, predicted, confidence),
        )
        history_id = int(hist.lastrowid)
        con.execute(
            """INSERT INTO recommendation_trust_audit(
                   history_id,run_id,movie_id,context_date,slot,rank_position,engine_version,
                   trust_status,trust_score,gate_score,support_count,support_labels,
                   red_flag,created_at
               ) VALUES(?,?,?,?,?,?,?,'trusted',.8,.82,2,'["rating personal","ALS"]',0,?)""",
            (history_id, int(run.lastrowid), movie_id, day, "decision", rank, engine, stamp),
        )
        return history_id


def _outcome(
    db: Database,
    history_id: int,
    movie_id: int,
    *,
    day: str,
    predicted: float,
    actual: int,
    engine: str,
    rank: int = 1,
) -> None:
    stamp = day + "T22:00:00+00:00"
    with db.tx() as con:
        con.execute(
            """INSERT INTO recommendation_outcomes(
                   exposure_history_id,movie_id,rank_position,context_date,slot,
                   chosen_at,playback_at,watched_at,actual_rating,rating_date,
                   predicted_rating,confidence,final_score,engine_version,absolute_error,
                   resolved_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                history_id,
                movie_id,
                rank,
                day,
                "decision",
                day + "T19:00:00+00:00",
                day + "T19:05:00+00:00",
                stamp,
                actual,
                day,
                predicted,
                .75,
                .82,
                engine,
                abs(float(predicted) - float(actual)),
                stamp,
                stamp,
            ),
        )


def test_schema_v9_keeps_recommendation_explanations(tmp_path):
    db = Database(tmp_path / "schema-v9.db")
    assert SCHEMA_VERSION == 9
    with db.connect() as con:
        current = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_explanations'"
        ).fetchone()
    assert current == 9
    assert table is not None


def test_outcome_calibration_activates_only_after_measured_gain(tmp_path):
    db = Database(tmp_path / "calibration.db")
    start = date(2026, 1, 1)
    for idx in range(40):
        mid = _movie(db, idx, f"Calibration {idx}")
        day = (start + timedelta(days=idx)).isoformat()
        predicted = 6.0 + (idx % 2) * .5
        actual = 7 if idx % 2 == 0 else 8
        hid = _root_exposure(db, mid, day=day, predicted=predicted, engine="cal-test")
        _outcome(db, hid, mid, day=day, predicted=predicted, actual=actual, engine="cal-test")

    brain = PredictionCalibratorV43(db)
    status = brain.status()
    assert status["approved"] is True
    assert status["calibrated_mae"] < status["raw_mae"]

    movie = Movie(id=999, title="Candidate", title_type="movie", genres=["Drama"])
    score = ScoreBreakdown(final=.72, predicted_rating=6.0, confidence=.7, score_factors={})
    result = brain.apply(movie, score)
    assert result["approved"] is True
    assert score.predicted_rating > 6.0
    assert score.final > .72
    assert any(name == "Calibrare după rezultate reale" for name, _pts, _reason in score.contributions)


def test_calibration_stays_protected_with_too_few_outcomes(tmp_path):
    db = Database(tmp_path / "calibration-small.db")
    brain = PredictionCalibratorV43(db)
    status = brain.status()
    assert status["approved"] is False
    assert status["reason"] == "insufficient_outcomes"


def test_fine_text_semantics_validates_clear_signal(tmp_path):
    db = Database(tmp_path / "semantic.db")
    start = date(2024, 1, 1)
    for idx in range(160):
        liked = idx % 2 == 0
        overview = (
            "ancient monastery sacred pilgrimage contemplation icon prayer mountain"
            if liked
            else "corporate office paperwork routine meeting accounting cubicle deadline"
        )
        keywords = (
            ["monastery", "pilgrimage", "sacred icon"]
            if liked
            else ["office work", "accounting", "corporate meeting"]
        )
        mid = _movie(db, 1000 + idx, f"Semantic {idx}", overview=overview, keywords=keywords)
        stamp = (start + timedelta(days=idx)).isoformat()
        now = stamp + "T12:00:00+00:00"
        with db.tx() as con:
            con.execute(
                """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (mid, 9 if liked else 3, stamp, "test", now, now),
            )

    semantic = TextSemanticBrainV43(db)
    status = semantic.status()
    assert status["approved"] is True
    assert status["semantic_mae"] < status["baseline_mae"]

    movie = Movie(
        id=9999,
        title="New",
        overview="A sacred monastery pilgrimage through mountain prayer and contemplation",
        keywords=["monastery", "pilgrimage"],
        genres=["Drama"],
    )
    score = ScoreBreakdown(final=.70, predicted_rating=7.0, confidence=.65, score_factors={})
    result = semantic.apply(movie, score)
    assert result["approved"] is True
    assert result["affinity"] > 0
    assert score.final > .70
    assert "semantică text" in score.score_factors


def test_exact_explanation_snapshot_is_queryable_and_backed_up(tmp_path):
    source = Database(tmp_path / "history-source.db")
    mid = _movie(source, 2001, "Snapshot Film")
    hid = _root_exposure(
        source,
        mid,
        day="2026-09-20",
        predicted=8.4,
        confidence=.81,
        engine="learning-insight-v4.3.1",
    )
    score = ScoreBreakdown(
        final=.87,
        predicted_rating=8.4,
        confidence=.81,
        personal_reason="Motiv exact salvat.",
        why_not="Este puțin mai lung.",
        score_factors={"gust": .8, "semantică text": .007},
        contributions=[("Test", 2.0, "Explicație exactă.")],
    )
    with source.tx() as con:
        record_explanation_snapshot(con, hid, score, created_at="2026-09-20T18:00:00+00:00")

    rows = recommendation_history_rows(source)
    assert len(rows) == 1
    assert rows[0]["snapshot_available"] is True
    assert rows[0]["personal_reason"] == "Motiv exact salvat."
    assert rows[0]["why_not"] == "Este puțin mai lung."
    assert rows[0]["score_factors"]["gust"] == .8
    assert history_engine_versions(source) == ["learning-insight-v4.3.1"]

    archive = export_profile(source, tmp_path / "profile-v4.zip")
    assert PROFILE_VERSION == 4
    target = Database(tmp_path / "history-target.db")
    stats = import_profile(target, archive, mode="restore")
    assert stats["explanations"] == 1
    restored = recommendation_history_rows(target)
    assert restored[0]["personal_reason"] == "Motiv exact salvat."
    assert restored[0]["contributions"][0][0] == "Test"


def test_performance_filters_by_period_and_engine(tmp_path):
    db = Database(tmp_path / "performance.db")
    old_mid = _movie(db, 3001, "Old Engine")
    new_mid = _movie(db, 3002, "New Engine")

    old_id = _root_exposure(
        db, old_mid, day="2026-01-10", predicted=7.0, engine="engine-old"
    )
    _outcome(
        db, old_id, old_mid, day="2026-01-10", predicted=7.0, actual=7, engine="engine-old"
    )

    new_id = _root_exposure(
        db, new_mid, day="2026-09-20", predicted=8.0, engine="engine-new"
    )
    _outcome(
        db, new_id, new_mid, day="2026-09-20", predicted=8.0, actual=9, engine="engine-new"
    )

    perf = recommendation_performance(
        db,
        since_date="2026-09-01",
        engine_version="engine-new",
        reconcile=False,
    )
    assert perf.decision_exposures == 1
    assert perf.rated_outcomes == 1
    assert perf.recent[0]["title"] == "New Engine"
    assert perf.recent[0]["engine_version"] == "engine-new"


def test_comparison_reason_uses_real_score_differences():
    a = Recommendation(
        Movie(id=1, title="A"),
        ScoreBreakdown(
            final=.85,
            predicted_rating=8.5,
            confidence=.8,
            score_factors={"gust": .8, "semantică text": .008},
        ),
    )
    b = Recommendation(
        Movie(id=2, title="B"),
        ScoreBreakdown(
            final=.80,
            predicted_rating=7.8,
            confidence=.65,
            score_factors={"gust": .65, "semantică text": -.002},
        ),
    )
    text = comparison_reason(a, b)
    assert "8.5" in text and "7.8" in text
    assert "încrederea" in text


def test_production_stack_and_ui_include_v43_layers():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    production = (root / "cinecalendar" / "production_engine.py").read_text(encoding="utf-8")
    composition = (root / "cinecalendar" / "ui_composition.py").read_text(encoding="utf-8")
    premium = (root / "cinecalendar" / "premium_ui.py").read_text(encoding="utf-8")
    library = (root / "cinecalendar" / "library_ui.py").read_text(encoding="utf-8")

    assert "learning_insight_engine_class(personalized_cls)" in production
    assert "production-stack-v4.4.0-feedback-protection" in production
    assert "install_learning_insight_ui_v43(window_cls)" in composition
    assert "Față de alegerea #1:" in premium
    assert "Comparație pe versiuni de motor" in library
    assert "Learning Insight 4.3" in library
