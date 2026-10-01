import math
from types import SimpleNamespace

import pytest

from cinecalendar.recommendation_miss_audit import build_miss_audit
from cinecalendar.top3_strategy import select_controlled_top3
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.v5_personal_ranker import DISLIKE_PENALTY
from cinecalendar.v5_shadow_ranker import adaptive_engine_class
from cinecalendar.recommender_v16 import FastRecommendationEngineV16


def _trace_row(iid, rating, rank):
    return {"imdb_id": iid, "rating": rating, "rank": rank}


def _report(candidate, final, top3):
    return {
        "cutoff_date": "2026-01-01",
        "hidden_trace": {
            "candidate": candidate,
            "final": final,
            "top3": top3,
        },
    }


def test_miss_audit_separates_retrieval_ranking_and_visible_failures():
    baseline = [_report(
        [
            _trace_row("tt1", 9, None),
            _trace_row("tt2", 8, 15),
            _trace_row("tt3", 8, 20),
            _trace_row("tt4", 10, 2),
        ],
        [
            _trace_row("tt1", 9, None),
            _trace_row("tt2", 8, None),
            _trace_row("tt3", 8, 8),
            _trace_row("tt4", 10, 2),
        ],
        [
            _trace_row("tt1", 9, None),
            _trace_row("tt2", 8, None),
            _trace_row("tt3", 8, None),
            _trace_row("tt4", 10, 1),
        ],
    )]
    discovery = [_report(
        [
            _trace_row("tt1", 9, 12),
            _trace_row("tt2", 8, 10),
            _trace_row("tt3", 8, 10),
            _trace_row("tt4", 10, 2),
        ],
        [
            _trace_row("tt1", 9, 9),
            _trace_row("tt2", 8, 7),
            _trace_row("tt3", 8, 6),
            _trace_row("tt4", 10, 2),
        ],
        [
            _trace_row("tt1", 9, None),
            _trace_row("tt2", 8, None),
            _trace_row("tt3", 8, 3),
            _trace_row("tt4", 10, 1),
        ],
    )]
    adaptive = [_report([], [], [
        _trace_row("tt1", 9, 2),
        _trace_row("tt2", 8, None),
        _trace_row("tt3", 8, 3),
        _trace_row("tt4", 10, 1),
    ])]

    audit = build_miss_audit(baseline, discovery, adaptive)
    counts = audit["counts"]
    assert counts["positive_targets"] == 4
    assert counts["stable_visible_hits"] == 1
    assert counts["recovered_by_discovery_retrieval"] == 1
    assert counts["recovered_by_discovery_ranking"] == 1
    assert counts["recovered_by_discovery_visible"] == 1


def _rec(i, final, calendar=0.0, season=0.0, novelty=0.0, trust=0.7):
    movie = Movie(
        id=i,
        imdb_id=f"tt{i:07d}",
        title=f"Film {i}",
        original_title=f"Film {i}",
        year=2020 + i,
        title_type="movie",
        genres=["Drama"] if i != 3 else ["Documentary"],
        directors=[f"Director {i}"],
        countries=["RO" if i == 2 else "US"],
    )
    score = ScoreBreakdown(
        final=final,
        predicted_rating=7.2,
        confidence=0.8,
        calendar=calendar,
        season=season,
        novelty=novelty,
    )
    score.trust_audit = {"trust": trust, "status": "trusted"}
    return Recommendation(movie=movie, score=score)


def test_controlled_top3_keeps_anchor_context_and_exploration_roles():
    ordered = [
        _rec(1, 0.90),
        _rec(2, 0.87, calendar=0.8),
        _rec(3, 0.85, novelty=0.8),
        _rec(4, 0.84),
    ]
    selected, roles = select_controlled_top3(ordered, 3)
    assert selected[0].movie.id == 1
    assert roles[0] == "principal"
    assert "context" in roles
    assert len(selected) == 3
    assert len(set(rec.movie.id for rec in selected)) == 3


def test_negative_penalty_is_stronger_than_original_v5():
    assert DISLIKE_PENALTY > 0.70


def test_runtime_adaptive_supports_automatic_weight_candidates():
    learned = adaptive_engine_class(FastRecommendationEngineV16, "balanced", None)
    ten = adaptive_engine_class(FastRecommendationEngineV16, "balanced", 0.10)
    fifteen = adaptive_engine_class(FastRecommendationEngineV16, "balanced", 0.15)
    twenty = adaptive_engine_class(FastRecommendationEngineV16, "balanced", 0.20)
    assert learned.SHADOW_BLEND_OVERRIDE is None
    assert ten.SHADOW_BLEND_OVERRIDE == pytest.approx(0.10)
    assert fifteen.SHADOW_BLEND_OVERRIDE == pytest.approx(0.15)
    assert twenty.SHADOW_BLEND_OVERRIDE == pytest.approx(0.20)


def test_recent_taste_context_activates_from_recent_extreme_ratings(tmp_path):
    from cinecalendar.db import Database
    from cinecalendar.imdb_import import add_manual_rating
    from cinecalendar.recent_taste_context import RecentTasteContext

    db = Database(tmp_path / "cinecalendar.db")
    for idx in range(8):
        add_manual_rating(
            db,
            f"Liked {idx}",
            2020 + idx,
            9 if idx < 6 else 2,
            imdb_id=f"tt9{idx:06d}",
            genres=["Drama"] if idx < 6 else ["Horror"],
        )
    ctx = RecentTasteContext(db)
    status = ctx.status()
    assert status["active"] is True
    assert status["signals"] >= 8

    candidate = Movie(
        id=999,
        imdb_id="tt9999999",
        title="Candidate",
        original_title="Candidate",
        year=2026,
        title_type="movie",
        genres=["Drama"],
    )
    payload = ctx.score(candidate)
    assert payload["active"] is True
    assert -0.035 <= float(payload["nudge"]) <= 0.035


def test_comparison_ui_exposes_all_seven_improvement_outputs():
    source = open("cinecalendar/qt_ui_v2.py", encoding="utf-8").read()
    assert "Audit ratări 8–10" in source
    assert "Audit după rating" in source
    assert "Verdict automat" in source
    assert "Folosește Descoperire" in source
    assert "Folosește Adaptiv" in source
