from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import pytest

from cinecalendar.db import Database
from cinecalendar.hybrid_calibration_v46 import calibrated_hybrid_engine_class
from cinecalendar.quality_manager_v46 import QUALITY_MANAGER_VERSION as QUALITY_V46
from cinecalendar.quality_manager_v34 import RecommendationQualityManager
from cinecalendar.quality_manager_v47 import RecommendationQualityManagerV47
from cinecalendar.production_engine import RANKING_STACK_VERSION
from cinecalendar.recalibration_policy_v47 import RecalibrationPolicyV47
from cinecalendar.recommendation_guard_v47 import compare_live_metrics
from cinecalendar.recommender_v16 import FastRecommendationEngineV16, recommendation_engine_identity
from cinecalendar.rolling_backtest_v37 import TemporalWindowV37


def _metric(*, rated=30, liked=24, chosen=35, watched=28, mae=.55, variance=.08):
    return {
        "rated": rated,
        "liked": liked,
        "liked_rate": liked / rated,
        "chosen": chosen,
        "watched": watched,
        "watched_rate": watched / chosen,
        "mae_count": rated,
        "mae": mae,
        "mae_variance": variance,
    }


def _rating(db: Database, index: int, updated_at: str):
    with db.tx() as con:
        movie = con.execute(
            """INSERT INTO movies(imdb_id,identity_key,title,title_type,source,created_at,updated_at,title_norm)
               VALUES(?,?,?,?,?,?,?,?)""",
            (f"tt{index:07d}", f"tt{index:07d}", f"Movie {index}", "movie", "test", updated_at, updated_at, f"movie {index}"),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(movie.lastrowid), 8, updated_at[:10], "test", updated_at, updated_at),
        )


def test_recalibration_requires_a_meaningful_batch_and_ignores_feedback(tmp_path):
    db = Database(tmp_path / "policy.db")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    completed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for index in range(100):
        _rating(db, index + 1, old)
    policy = RecalibrationPolicyV47(db)
    report = {
        "status": "completed",
        "rating_count": 100,
        "completed_at": completed,
        "state_token": f"stale|{RANKING_STACK_VERSION}",
    }

    with db.tx() as con:
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
            (1, "not_now", 0.0, datetime.now(timezone.utc).isoformat()),
        )
    should, status = policy.should_run(report, "changed-by-feedback", 100)
    assert should is False
    assert status["state"] == "deferred"
    assert status["changed_ratings"] == 0
    assert status["required_ratings"] == 12

    recent = datetime.now(timezone.utc).isoformat()
    for index in range(100, 112):
        _rating(db, index + 1, recent)
    should, status = policy.should_run(report, "changed-by-ratings", 112)
    assert should is True
    assert status["state"] == "ready"
    assert status["changed_ratings"] == 12


def test_contextual_feedback_does_not_invalidate_the_historical_quality_token(tmp_path):
    db = Database(tmp_path / "feedback-token.db")
    stamp = datetime.now(timezone.utc).isoformat()
    _rating(db, 1, stamp)
    manager = RecommendationQualityManager(db)
    before = manager.state_token()
    with db.tx() as con:
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,?,?,?)",
            (1, "too_long", 0.0, stamp),
        )
    assert manager.state_token() == before


def test_personal_hybrid_has_a_distinct_real_outcome_identity():
    hybrid = calibrated_hybrid_engine_class(FastRecommendationEngineV16, .60)
    assert recommendation_engine_identity(FastRecommendationEngineV16).endswith("als70-content30")
    assert recommendation_engine_identity(hybrid).endswith("als60-content40")
    assert recommendation_engine_identity(hybrid) != recommendation_engine_identity(FastRecommendationEngineV16)


def test_four_formulae_share_one_temporary_snapshot_per_window(tmp_path, monkeypatch):
    from cinecalendar import rolling_backtest_v37 as rolling

    source = tmp_path / "source.db"
    source.write_bytes(b"sqlite-placeholder")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = {"workspaces": 0, "evaluations": 0}

    @contextmanager
    def fake_workspace(_prefix):
        calls["workspaces"] += 1
        yield workspace

    def fake_backup(_source, target):
        target.write_bytes(b"")

    def fake_evaluate(_db, _window, *, engine_cls, **_kwargs):
        calls["evaluations"] += 1
        return {"engine": engine_cls.__name__}

    monkeypatch.setattr(rolling, "managed_temp_workspace", fake_workspace)
    monkeypatch.setattr(rolling, "_sqlite_backup", fake_backup)
    monkeypatch.setattr(rolling, "_remove_future", lambda _db, _window: None)
    monkeypatch.setattr(rolling, "_evaluate_window_db", fake_evaluate)
    window = TemporalWindowV37(1, 100, tuple(), tuple(), "2026-01-01")
    classes = [type(f"Engine{i}", (), {}) for i in range(4)]

    reports = rolling.run_window_backtest_group(source, window, engine_classes=classes)
    assert calls == {"workspaces": 1, "evaluations": 4}
    assert [item["engine"] for item in reports] == [cls.__name__ for cls in classes]


def test_live_guard_rolls_back_only_when_two_real_metrics_regress():
    reference = _metric()
    one_bad = _metric(liked=10, watched=28, mae=.55)
    verdict = compare_live_metrics(reference, one_bad)
    assert verdict["liked_regression"] is True
    assert verdict["rollback"] is False

    two_bad = _metric(liked=10, watched=12, mae=1.20)
    verdict = compare_live_metrics(reference, two_bad)
    assert verdict["harm_count"] >= 2
    assert verdict["rollback"] is True


def test_v47_manager_rejects_a_personal_weight_rolled_back_on_real_results(tmp_path):
    db = Database(tmp_path / "rollback.db")
    manager = RecommendationQualityManagerV47(db)
    token = manager.state_token()
    completed_at = "2026-09-21T12:00:00+00:00"
    report = {
        "manager_version": QUALITY_V46,
        "state_token": token,
        "status": "completed",
        "rating_count": 200,
        "selected_als_weight": .60,
        "selected_verdict": {"approved": True},
        "completed_at": completed_at,
    }
    db.set_setting("recommendation_quality_v46", report)
    calibration_id = manager.live_guard.calibration_id(report, .60)
    db.set_setting(
        "recommendation_live_guard_v47",
        {
            "version": "recommendation-live-guard-v4.7.0",
            "status": "rolled_back",
            "calibration_id": calibration_id,
            "selected_als_weight": .60,
        },
    )

    selected = manager.preferred_engine_class()
    assert float(getattr(selected, "ALS_WEIGHT", .70)) == pytest.approx(.70)


def test_recommendations_page_exposes_formula_guard_and_recalibration_state():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ui = (root / "cinecalendar" / "premium_ui.py").read_text(encoding="utf-8")
    accuracy = (root / "cinecalendar" / "accuracy_ui_v37.py").read_text(encoding="utf-8")
    assert "PROTECȚIA RECOMANDĂRILOR" in ui
    assert "Revenire automată activă" in ui
    assert "feedbackul temporar nu îl repornește" in ui
    assert "Accuracy 4.7" in accuracy
