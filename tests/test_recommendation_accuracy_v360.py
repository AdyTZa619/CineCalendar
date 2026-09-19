from __future__ import annotations

from datetime import date

from cinecalendar.calendar_engine_v3 import ContextCalendarEngineV35
from cinecalendar.db import Database
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.local_content_v36 import LocalContentCandidateGeneratorV36
from cinecalendar.quality_manager_v36 import RecommendationQualityManagerV36
from cinecalendar.recommender_v17 import FastRecommendationEngineV17
from cinecalendar.recommender_v18 import FastRecommendationEngineV18
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


def _rated(db, title, rating, *, director, country="Romania", genre="Drama"):
    mid = add_manual_rating(db, title, 2000 + rating, rating, genres=[genre])
    with db.tx() as con:
        con.execute(
            "UPDATE movies SET directors_json=?,countries_json=?,genres_json=? WHERE id=?",
            (json_dumps([director]), json_dumps([country]), json_dumps([genre]), mid),
        )
    return mid


def _candidate(db, title, *, director, country="Romania", genre="Drama", imdb_rating=7.4, votes=5000):
    now = utcnow_iso()
    ident = identity_key(title, title, 2024, "Movie")
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(identity_key,title,original_title,title_norm,original_title_norm,year,title_type,
                      genres_json,directors_json,countries_json,imdb_rating,num_votes,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ident, title, title, normalize_text(title), normalize_text(title), 2024, "Movie",
                json_dumps([genre]), json_dumps([director]), json_dumps([country]),
                imdb_rating, votes, "test", now, now,
            ),
        )
        return int(cur.lastrowid)


def test_local_retrieval_requires_repeated_positive_evidence(tmp_path):
    db = Database(tmp_path / "accuracy.db")
    _rated(db, "Liked A", 9, director="Repeated Director")
    _rated(db, "Liked B", 10, director="Repeated Director")
    _rated(db, "Single A", 10, director="One Off Director", country="France", genre="Mystery")
    repeated = _candidate(db, "Unseen Repeated", director="Repeated Director")
    one_off = _candidate(db, "Unseen One Off", director="One Off Director", country="France", genre="Mystery")

    ids = LocalContentCandidateGeneratorV36(db).candidates(100)
    assert repeated in ids
    assert one_off not in ids


def test_low_ratings_can_veto_a_director_lane(tmp_path):
    db = Database(tmp_path / "veto.db")
    _rated(db, "Liked A", 8, director="Mixed Director")
    _rated(db, "Liked B", 8, director="Mixed Director")
    _rated(db, "Bad A", 1, director="Mixed Director")
    _rated(db, "Bad B", 2, director="Mixed Director")
    _rated(db, "Bad C", 3, director="Mixed Director")
    candidate = _candidate(db, "Unseen Mixed", director="Mixed Director")

    ids = LocalContentCandidateGeneratorV36(db).candidates(100)
    assert candidate not in ids


def test_v18_candidate_mix_keeps_validated_baseline_dominant():
    baseline = list(range(1, 101))
    local = list(range(1001, 1101))
    merged, stats = FastRecommendationEngineV18._merge_accuracy_candidates(baseline, local, 100, 0.14)

    assert len(merged) == 100
    assert len(set(merged)) == 100
    assert stats["local_content"] <= 14
    assert stats["baseline"] >= 86


def test_v18_local_lane_is_fail_open(tmp_path, monkeypatch):
    db = Database(tmp_path / "fail-open.db")
    engine = FastRecommendationEngineV18(db, ContextCalendarEngineV35())
    baseline = list(range(1, 81))
    monkeypatch.setattr(FastRecommendationEngineV17, "_balanced_candidate_ids", lambda self, when, limit: baseline[:limit])

    def broken(_limit):
        raise RuntimeError("metadata edge case")

    monkeypatch.setattr(engine.local_content, "candidates", broken)
    assert engine._balanced_candidate_ids(date(2026, 9, 17), 80) == baseline


def _comparison(*, bad_delta=0.0, ndcg=0.02, cand8=0.02, cand9=0.01, mean=0.02):
    verdict = {
        "approved": True,
        "candidate_8_plus_delta": cand8,
        "candidate_9_plus_delta": cand9,
        "final_dislike_delta": bad_delta,
        "ndcg25_delta": ndcg,
        "composite_delta": mean,
        "guardrails": {
            "candidate_8_plus_no_material_regression": True,
            "candidate_9_plus_no_material_regression": True,
            "dislike_exposure_no_material_regression": True,
        },
    }
    return {
        "folds": [
            {"fraction": 0.15, "verdict": dict(verdict)},
            {"fraction": 0.25, "verdict": dict(verdict)},
        ],
        "aggregate": {"approved": True, "mean_composite_delta": mean},
    }


def test_v36_requires_two_strict_fold_wins():
    ok = RecommendationQualityManagerV36._strict_approval(_comparison())
    assert ok["approved"] is True

    one_bad = _comparison()
    one_bad["folds"][1]["verdict"]["approved"] = False
    assert RecommendationQualityManagerV36._strict_approval(one_bad)["approved"] is False


def test_v36_tightens_dislike_and_recall_guardrails():
    assert RecommendationQualityManagerV36._strict_approval(_comparison(bad_delta=0.02))["approved"] is False
    assert RecommendationQualityManagerV36._strict_approval(_comparison(cand8=-0.02))["approved"] is False
    assert RecommendationQualityManagerV36._strict_approval(_comparison(cand9=-0.02))["approved"] is False
    assert RecommendationQualityManagerV36._strict_approval(_comparison(ndcg=-0.01))["approved"] is False


def test_v36_requires_mean_gain_above_strict_threshold():
    assert RecommendationQualityManagerV36._strict_approval(_comparison(mean=0.011))["approved"] is False


def test_service_moves_to_v37_without_removing_v36_or_v35():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    service = (root / "cinecalendar" / "service.py").read_text(encoding="utf-8")
    production = (root / "cinecalendar" / "production_engine.py").read_text(encoding="utf-8")
    composition = (root / "cinecalendar" / "ui_composition.py").read_text(encoding="utf-8")
    init = (root / "cinecalendar" / "__init__.py").read_text(encoding="utf-8")

    assert "RecommendationQualityManagerV37" in service
    assert "RecommendationQualityManagerV36" not in service
    assert (root / "cinecalendar" / "quality_manager_v36.py").exists()
    assert "ContextCalendarEngineV35" in service
    assert "build_production_recommender" in service
    assert "availability_engine_class" in production
    assert "contextual_engine_class" in production
    assert "install_context_ui_v35" in composition
    assert "install_accuracy_ui_v37" in composition
    assert "install_accuracy_ui_v36" not in composition
    assert '__version__ = "3.9.3"' in init
