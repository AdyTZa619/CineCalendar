from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace

from cinecalendar.db import Database
from cinecalendar.foundation_v33 import _record_exposures
from cinecalendar.hybrid_calibration_v46 import calibrated_hybrid_engine_class
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v16 import FastRecommendationEngineV16, recommendation_engine_identity
from cinecalendar.util import utcnow_iso


def _movie(db: Database) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        row = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9481001", "tt9481001", "Telemetry", "Telemetry", 2026, "movie", 100,
                json.dumps(["Drama"]), "[]", "[]", "", "[]", "{}", 7.1, 5000,
                "test", now, now, "telemetry", "telemetry",
            ),
        )
        return int(row.lastrowid)


def _rec(movie_id: int) -> Recommendation:
    return Recommendation(
        Movie(id=movie_id, imdb_id="tt9481001", title="Telemetry", year=2026),
        ScoreBreakdown(final=.8, predicted_rating=7.9, confidence=.72),
    )


def test_composed_ui_records_the_complete_formula_identity(tmp_path):
    db = Database(tmp_path / "telemetry.db")
    movie_id = _movie(db)
    hybrid = calibrated_hybrid_engine_class(FastRecommendationEngineV16, .60)
    engine = object.__new__(hybrid)
    window = SimpleNamespace(db=db, s=SimpleNamespace(recommender=engine))

    _record_exposures(window, [_rec(movie_id)], date.today(), "browse")

    expected = recommendation_engine_identity(engine)
    with db.connect() as con:
        run = con.execute("SELECT engine_version FROM recommendation_runs").fetchone()
        audit = con.execute("SELECT engine_version FROM recommendation_trust_audit").fetchone()
    assert run["engine_version"] == expected
    assert audit["engine_version"] == expected
    assert expected.endswith("als60-content40")


def test_repainting_the_same_final_objects_records_each_exposure_once(tmp_path):
    db = Database(tmp_path / "once.db")
    movie_id = _movie(db)
    engine = object.__new__(FastRecommendationEngineV16)
    window = SimpleNamespace(db=db, s=SimpleNamespace(recommender=engine))
    rec = _rec(movie_id)

    first = _record_exposures(window, [rec], date.today(), "browse")
    second = _record_exposures(window, [rec], date.today(), "browse")

    with db.connect() as con:
        roots = con.execute(
            "SELECT COUNT(*) FROM recommendation_history WHERE action IS NULL"
        ).fetchone()[0]
    assert first == second
    assert roots == 1
