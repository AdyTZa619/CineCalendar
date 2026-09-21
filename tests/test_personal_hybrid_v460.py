from __future__ import annotations

import pytest

from cinecalendar.db import Database
from cinecalendar.hybrid_calibration_v46 import (
    allowed_als_weights,
    calibrated_hybrid_engine_class,
)
from cinecalendar.quality_manager_v46 import (
    QUALITY_MANAGER_VERSION,
    RecommendationQualityManagerV46,
)
from cinecalendar.production_engine import build_production_recommender, production_stack_status
from cinecalendar.recommender_v16 import FastRecommendationEngineV16


def test_personal_hybrid_class_changes_real_blend_and_is_cached():
    hybrid50 = calibrated_hybrid_engine_class(FastRecommendationEngineV16, .50)
    same = calibrated_hybrid_engine_class(FastRecommendationEngineV16, .50)
    engine = object.__new__(hybrid50)

    score, als_weight, content_weight = engine._hybrid_blend(.90, .50)

    assert hybrid50 is same
    assert hybrid50.__mro__[1] is FastRecommendationEngineV16
    assert (als_weight, content_weight) == (.50, .50)
    assert score == .70
    assert hybrid50.__name__.endswith("Hybrid50")


def test_global_baseline_remains_70_30_when_no_challenger_wins():
    engine = object.__new__(FastRecommendationEngineV16)
    score, als_weight, content_weight = engine._hybrid_blend(.90, .50)

    assert als_weight == pytest.approx(.70)
    assert content_weight == pytest.approx(.30)
    assert score == pytest.approx(.78)
    assert allowed_als_weights() == (.50, .60, .80)


def test_quality_manager_activates_only_a_completed_approved_personal_weight(tmp_path):
    db = Database(tmp_path / "personal-hybrid.db")
    manager = RecommendationQualityManagerV46(db)
    token = manager.state_token()
    db.set_setting(
        "recommendation_quality_v46",
        {
            "manager_version": QUALITY_MANAGER_VERSION,
            "state_token": token,
            "status": "completed",
            "selected_als_weight": .60,
            "selected_verdict": {"approved": True},
        },
    )

    selected = manager.preferred_engine_class()

    assert selected.ALS_WEIGHT == .60
    assert selected.CONTENT_WEIGHT == .40
    assert selected.__mro__[1] is manager.baseline_engine_class()


def test_production_status_exposes_the_active_personal_balance(tmp_path):
    db = Database(tmp_path / "production-status.db")
    hybrid60 = calibrated_hybrid_engine_class(FastRecommendationEngineV16, .60)
    engine = build_production_recommender(db, hybrid60)

    status = production_stack_status(engine)

    assert status["als_weight"] == .60
    assert status["content_weight"] == .40
    assert status["hybrid_calibration_version"] == "hybrid-calibration-v4.6.0"


def test_quality_manager_rejects_unapproved_weight(tmp_path):
    db = Database(tmp_path / "unapproved-hybrid.db")
    manager = RecommendationQualityManagerV46(db)
    baseline = manager.baseline_engine_class()
    db.set_setting(
        "recommendation_quality_v46",
        {
            "manager_version": QUALITY_MANAGER_VERSION,
            "state_token": manager.state_token(),
            "status": "completed",
            "selected_als_weight": .50,
            "selected_verdict": {"approved": False},
        },
    )

    assert manager.preferred_engine_class() is baseline


def test_previous_validated_weight_survives_an_interrupted_recalibration(tmp_path):
    db = Database(tmp_path / "fallback-hybrid.db")
    manager = RecommendationQualityManagerV46(db)
    db.set_setting(
        "recommendation_quality_v46",
        {
            "manager_version": QUALITY_MANAGER_VERSION,
            "state_token": manager.state_token(),
            "status": "running",
            "fallback_als_weight": .80,
            "selected_verdict": {},
        },
    )

    selected = manager.preferred_engine_class()

    assert selected.ALS_WEIGHT == .80
    assert selected.CONTENT_WEIGHT == pytest.approx(.20)


def test_too_few_ratings_never_starts_expensive_calibration(tmp_path):
    db = Database(tmp_path / "small-history.db")
    manager = RecommendationQualityManagerV46(db)

    assert manager.start_background(delay_seconds=0) is False
    status = manager.cached_report()
    assert status["status"] == "insufficient_ratings"
    assert status["minimum_ratings"] == 170
