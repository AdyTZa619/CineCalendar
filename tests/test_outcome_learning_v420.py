from __future__ import annotations

from pathlib import Path

from cinecalendar.db import Database, SCHEMA_VERSION
from cinecalendar.feedback import apply_feedback
from cinecalendar.recommendation_outcomes_v42 import (
    recommendation_performance,
    reconcile_recommendation_outcomes,
)
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso
from cinecalendar.watch_success import _FEEDBACK_SIGNALS


def _movie(db: Database, idx: int, title: str = "Film test") -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{9100000+idx:07d}",
                identity_key(title, title, 2025, "movie"),
                title,
                title,
                normalize_text(title),
                normalize_text(title),
                2025,
                "movie",
                105,
                json_dumps(["Drama"]),
                json_dumps(["Director Test"]),
                json_dumps(["Romania"]),
                7.2,
                12000,
                "test",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def _exposure(
    db: Database,
    movie_id: int,
    *,
    day: str,
    predicted: float,
    confidence: float = .75,
    rank: int = 1,
) -> int:
    now = day + "T18:00:00+00:00"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action,
                   exposure_history_id,predicted_rating,confidence
               ) VALUES(?,?,?,?,?,0,NULL,NULL,?,?)""",
            (movie_id, now, day, "decision", .82, predicted, confidence),
        )
        eid = int(cur.lastrowid)
        con.execute(
            """INSERT INTO recommendation_trust_audit(
                   history_id,run_id,movie_id,context_date,slot,rank_position,engine_version,
                   trust_status,trust_score,gate_score,support_count,support_labels,
                   red_flag,red_reason,score_gap,als_score,public_bayes,created_at
               ) VALUES(?,NULL,?,?,?,?,?,'trusted',.8,.8,2,'[]',0,NULL,NULL,NULL,NULL,?)""",
            (eid, movie_id, day, "decision", rank, "test-engine", now),
        )
        return eid


def _event(db: Database, exposure_id: int, movie_id: int, day: str, action: str, hour: int) -> None:
    with db.tx() as con:
        con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action,
                   exposure_history_id
               ) VALUES(?,?,?,?,?,0,?,?)""",
            (
                movie_id,
                f"{day}T{hour:02d}:00:00+00:00",
                day,
                "decision_action",
                .82,
                action,
                exposure_id,
            ),
        )


def _rating(db: Database, movie_id: int, rating: int, day: str) -> None:
    now = day + "T23:00:00+00:00"
    with db.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, rating, day, "test", now, now),
        )


def test_schema_v7_adds_prediction_snapshot_and_outcome_table(tmp_path):
    db = Database(tmp_path / "schema-v7.db")
    assert SCHEMA_VERSION == 7
    with db.connect() as con:
        hist = {row[1] for row in con.execute("PRAGMA table_info(recommendation_history)")}
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "predicted_rating" in hist
    assert "confidence" in hist
    assert "recommendation_outcomes" in tables


def test_reconcile_closes_chosen_started_watched_rating_loop(tmp_path):
    db = Database(tmp_path / "outcome.db")
    mid = _movie(db, 1)
    eid = _exposure(db, mid, day="2026-09-18", predicted=8.2, rank=1)
    _event(db, eid, mid, "2026-09-18", "chosen", 19)
    _event(db, eid, mid, "2026-09-18", "playback_confirmed", 20)
    _event(db, eid, mid, "2026-09-18", "watched", 22)
    _rating(db, mid, 9, "2026-09-19")

    assert reconcile_recommendation_outcomes(db) == 1
    with db.connect() as con:
        row = con.execute(
            "SELECT * FROM recommendation_outcomes WHERE exposure_history_id=?",
            (eid,),
        ).fetchone()
    assert row is not None
    assert row["actual_rating"] == 9
    assert abs(float(row["absolute_error"]) - .8) < 1e-9
    assert row["chosen_at"]
    assert row["playback_at"]
    assert row["watched_at"]

    perf = recommendation_performance(db)
    assert perf.chosen == 1
    assert perf.playback_confirmed == 1
    assert perf.watched == 1
    assert perf.rated_outcomes == 1
    assert abs(float(perf.mae) - .8) < 1e-9
    assert perf.within_one == 1.0
    assert perf.liked_rate == 1.0
    assert perf.top1_liked_rate == 1.0


def test_one_real_rating_is_owned_by_latest_explicit_exposure(tmp_path):
    db = Database(tmp_path / "dedupe-outcome.db")
    mid = _movie(db, 2, "Repeated choice")
    first = _exposure(db, mid, day="2026-09-17", predicted=7.1, rank=2)
    _event(db, first, mid, "2026-09-17", "chosen", 19)

    second = _exposure(db, mid, day="2026-09-19", predicted=8.4, rank=1)
    _event(db, second, mid, "2026-09-19", "chosen", 20)
    _event(db, second, mid, "2026-09-19", "watched", 22)
    _rating(db, mid, 9, "2026-09-20")

    reconcile_recommendation_outcomes(db)
    with db.connect() as con:
        a = con.execute(
            "SELECT actual_rating FROM recommendation_outcomes WHERE exposure_history_id=?",
            (first,),
        ).fetchone()
        b = con.execute(
            "SELECT actual_rating FROM recommendation_outcomes WHERE exposure_history_id=?",
            (second,),
        ).fetchone()
    assert a["actual_rating"] is None
    assert b["actual_rating"] == 9
    assert recommendation_performance(db).rated_outcomes == 1




def test_skipped_choice_is_not_retroactively_counted_as_success(tmp_path):
    db = Database(tmp_path / "skipped-outcome.db")
    mid = _movie(db, 4, "Skipped choice")
    eid = _exposure(db, mid, day="2026-09-18", predicted=7.8, rank=1)
    _event(db, eid, mid, "2026-09-18", "chosen", 19)

    # The first choice is a live outcome while it is still active.
    reconcile_recommendation_outcomes(db)
    with db.connect() as con:
        assert con.execute(
            "SELECT 1 FROM recommendation_outcomes WHERE exposure_history_id=?",
            (eid,),
        ).fetchone() is not None

    # A later skip must remove that already-created outcome, not only prevent future inserts.
    _event(db, eid, mid, "2026-09-18", "skip_today", 20)
    _rating(db, mid, 9, "2026-09-20")
    reconcile_recommendation_outcomes(db)
    with db.connect() as con:
        row = con.execute(
            "SELECT * FROM recommendation_outcomes WHERE exposure_history_id=?",
            (eid,),
        ).fetchone()
    assert row is None

def test_contextual_feedback_is_short_term_only(tmp_path):
    db = Database(tmp_path / "feedback.db")
    mid = _movie(db, 3, "Not tonight")

    apply_feedback(db, mid, "not_now")
    apply_feedback(db, mid, "too_long")
    apply_feedback(db, mid, "mood_mismatch")
    apply_feedback(db, mid, "too_similar")

    with db.connect() as con:
        rows = con.execute(
            "SELECT kind,weight FROM feedback WHERE movie_id=? ORDER BY id",
            (mid,),
        ).fetchall()
    assert [row["kind"] for row in rows] == [
        "not_now", "too_long", "mood_mismatch", "too_similar"
    ]
    assert all(float(row["weight"]) == 0.0 for row in rows)
    assert _FEEDBACK_SIGNALS["not_now"][1] <= 3.0
    assert _FEEDBACK_SIGNALS["too_long"][1] <= 14.0


def test_v42_ui_exposes_dashboard_and_context_feedback():
    root = Path(__file__).resolve().parents[1]
    premium = (root / "cinecalendar" / "premium_ui.py").read_text(encoding="utf-8")
    library = (root / "cinecalendar" / "library_ui.py").read_text(encoding="utf-8")
    foundation = (root / "cinecalendar" / "foundation_v33.py").read_text(encoding="utf-8")

    assert "Nu acum / motiv" in premium
    assert "Prea lung pentru moment" in premium
    assert "nu modifică permanent" in (root / "cinecalendar" / "qt_ui.py").read_text(encoding="utf-8")
    assert "Performanța reală a recomandărilor" in library
    assert "predicted_rating,confidence" in foundation.replace("\n", "")
