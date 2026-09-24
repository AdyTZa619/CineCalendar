from __future__ import annotations

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.reliability_gate_v412 import (
    MIN_MEASURED_OUTCOMES,
    RecommendationReliabilityGate,
)
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


def _db_movie(db: Database) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9900001", identity_key("Reliable", "Reliable", 2024, "movie"),
                "Reliable", "Reliable", normalize_text("Reliable"), normalize_text("Reliable"),
                2024, "movie", 110, json_dumps(["Drama"]), json_dumps(["Director"]),
                json_dumps(["Romania"]), "A complete synopsis.", json_dumps(["family"]),
                7.4, 20000, "test", now, now,
            ),
        )
        return int(cur.lastrowid)


def _outcomes(db: Database, movie_id: int, count: int, *, error: float = 0.5) -> None:
    now = utcnow_iso()
    with db.tx() as con:
        for index in range(count):
            actual = 8 if index % 2 == 0 else 7
            predicted = actual + error
            history = con.execute(
                """INSERT INTO recommendation_history(
                       movie_id,recommended_at,context_date,slot,final_score,ignored,action,
                       exposure_history_id,predicted_rating,confidence
                   ) VALUES(?,?,?,?,?,0,NULL,NULL,?,?)""",
                (movie_id, now, "2026-09-01", "decision", .82, predicted, .82),
            )
            exposure_id = int(history.lastrowid)
            con.execute(
                """INSERT INTO recommendation_outcomes(
                       exposure_history_id,movie_id,rank_position,context_date,slot,chosen_at,
                       watched_at,actual_rating,rating_date,predicted_rating,confidence,final_score,
                       engine_version,absolute_error,resolved_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    exposure_id, movie_id, 1, "2026-09-01", "decision", now, now, actual,
                    "2026-09-02", predicted, .82, .82, "test", abs(error), now, now,
                ),
            )


def _rec(*, complete: bool = True, red_flag: bool = False) -> Recommendation:
    movie = Movie(
        id=100,
        title="Candidate",
        year=2024,
        runtime_min=105 if complete else None,
        genres=["Drama"],
        directors=["Director"],
        countries=["Romania"],
        overview="Synopsis",
    )
    score = ScoreBreakdown(
        final=.84,
        predicted_rating=7.8,
        confidence=.84,
        evidence=4.0,
        trust_audit={
            "status": "red_flag" if red_flag else "trusted",
            "red_flag": red_flag,
            "red_reason": "test red flag" if red_flag else "",
        },
    )
    return Recommendation(movie, score)


def test_high_heuristic_confidence_is_not_called_verified_without_real_outcomes(tmp_path):
    db = Database(tmp_path / "few.db")
    gate = RecommendationReliabilityGate(db)
    gate.refresh(reconcile=False)

    verdict = gate.evaluate(_rec())

    assert verdict.status == "insufficient"
    assert verdict.measured_outcomes == 0
    assert verdict.empirical_interval is False


def test_verified_requires_measured_accuracy_complete_metadata_and_strong_evidence(tmp_path):
    db = Database(tmp_path / "verified.db")
    mid = _db_movie(db)
    _outcomes(db, mid, MIN_MEASURED_OUTCOMES, error=.5)
    gate = RecommendationReliabilityGate(db)
    snapshot = gate.refresh(reconcile=False)

    verdict = gate.evaluate(_rec())

    assert snapshot.measurement_ready is True
    assert verdict.status == "verified"
    assert verdict.empirical_interval is True
    assert (verdict.interval_low, verdict.interval_high) == (7.3, 8.3)


def test_missing_ranking_metadata_prevents_verified_verdict(tmp_path):
    db = Database(tmp_path / "metadata.db")
    mid = _db_movie(db)
    _outcomes(db, mid, MIN_MEASURED_OUTCOMES, error=.5)
    gate = RecommendationReliabilityGate(db)
    gate.refresh(reconcile=False)

    verdict = gate.evaluate(_rec(complete=False))

    assert verdict.status == "cautious"
    assert verdict.metadata_complete is False


def test_red_flag_always_rejects_even_when_measurement_is_ready(tmp_path):
    db = Database(tmp_path / "red.db")
    mid = _db_movie(db)
    _outcomes(db, mid, MIN_MEASURED_OUTCOMES, error=.4)
    gate = RecommendationReliabilityGate(db)
    gate.refresh(reconcile=False)

    assert gate.evaluate(_rec(red_flag=True)).status == "reject"


def test_gate_never_mutates_score_or_ranking_inputs(tmp_path):
    db = Database(tmp_path / "pure.db")
    gate = RecommendationReliabilityGate(db)
    gate.refresh(reconcile=False)
    first = _rec()
    second = _rec()
    second.score.final = .81
    before = [(item.score.final, item.score.predicted_rating) for item in (first, second)]

    gate.evaluate(first)
    gate.evaluate(second)

    assert [(item.score.final, item.score.predicted_rating) for item in (first, second)] == before


def test_measurement_does_not_mix_outcomes_from_another_engine(tmp_path):
    db = Database(tmp_path / "engine.db")
    mid = _db_movie(db)
    _outcomes(db, mid, MIN_MEASURED_OUTCOMES, error=.5)

    snapshot = RecommendationReliabilityGate(db, "different-engine").refresh(reconcile=False)

    assert snapshot.rated_outcomes == 0
    assert snapshot.measurement_ready is False


def test_premium_ui_does_not_present_evidence_coverage_as_probability():
    source = open("cinecalendar/premium_ui.py", encoding="utf-8").read()
    assert '"dovezi personale"' in source
    assert "% încredere" not in source
    assert "RECOMANDARE VERIFICATĂ" in open(
        "cinecalendar/reliability_gate_v412.py", encoding="utf-8"
    ).read()
