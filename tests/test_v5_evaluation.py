import pytest

from cinecalendar.v5_lab import DiscoveryRecommendationEngine, V5LabRecommendationEngine
from cinecalendar.v5_event_replay import aggregate_event_reports
from cinecalendar.v5_evaluation import _event_guard
from cinecalendar.v5_decision_replay import decision_replay_guard
from cinecalendar.v5_shadow_ranker import (
    V5ShadowRankedEngine,
    V5ShadowRankedEngine10,
    V5ShadowRankedEngine15,
    V5ShadowRankedEngine20,
)


def test_v5_shadow_ranker_is_evaluation_only_subclass():
    assert issubclass(V5ShadowRankedEngine, DiscoveryRecommendationEngine)
    assert V5ShadowRankedEngine._blend(0.8, 0.2, 0.10) == pytest.approx(0.74)
    assert V5ShadowRankedEngine._blend(0.2, 0.8, 0.10) == pytest.approx(0.26)
    assert V5ShadowRankedEngine10.SHADOW_BLEND_OVERRIDE == pytest.approx(0.10)
    assert V5ShadowRankedEngine15.SHADOW_BLEND_OVERRIDE == pytest.approx(0.15)
    assert V5ShadowRankedEngine20.SHADOW_BLEND_OVERRIDE == pytest.approx(0.20)


def test_v5_evaluator_wires_three_way_historical_comparison():
    source = open("cinecalendar/v5_evaluation.py", encoding="utf-8").read()
    assert "FastRecommendationEngineV16" in source
    assert "DiscoveryRecommendationEngine" in source
    assert "V5ShadowRankedEngine" in source
    assert "rolling_windows(" in source
    assert "event_replay_windows(" in source
    assert "run_window_backtest_group(" in source
    assert "eligible_for_visible_alpha_trial" in source
    assert '"visible_ranking_changed": False' in source


def test_v5_lab_is_exposed_only_as_alpha_runtime_page():
    source = open("cinecalendar/qt_ui_v2.py", encoding="utf-8").read()
    assert '("v5_lab", "Comparare motor")' in source
    assert "def page_v5_lab(self):" in source
    assert "def run_v5_lab_evaluation(self):" in source
    assert "run_v5_evaluation(self.db" in source
    assert "eligible_for_visible_alpha_trial" in source
    assert "if bool(self.db.get_setting(\"auto_update_check\", True)):" in source


def test_event_replay_aggregates_candidate_top25_top50_and_ndcg():
    report = {
        "candidate_recall": {
            "liked_8_plus": 2,
            "loved_9_plus": 1,
            "disliked_4_minus": 1,
            "recall_8_plus_at_1000": 0.5,
            "recall_9_plus_at_1000": 1.0,
            "dislike_recall_at_1000": 0.0,
        },
        "final_ranking": {
            "liked_8_plus": 2,
            "loved_9_plus": 1,
            "disliked_4_minus": 1,
            "recall_8_plus_at_10": 0.0,
            "recall_9_plus_at_10": 0.0,
            "dislike_recall_at_10": 0.0,
            "recall_8_plus_at_25": 0.5,
            "recall_9_plus_at_25": 1.0,
            "dislike_recall_at_25": 0.0,
            "recall_8_plus_at_50": 1.0,
            "recall_9_plus_at_50": 1.0,
            "dislike_recall_at_50": 1.0,
        },
        "final_quality": {"ndcg_at_25": 0.4},
    }
    out = aggregate_event_reports([report])
    assert out["candidate_8_plus_hits"] == 1
    assert out["top25_8_plus_hits"] == 1
    assert out["top50_8_plus_hits"] == 2
    assert out["top25_8_plus_recall"] == pytest.approx(0.5)
    assert out["top50_8_plus_recall"] == pytest.approx(1.0)
    assert out["mean_ndcg25"] == pytest.approx(0.4)


def test_external_guard_rejects_zero_final_hits_even_when_candidate_pool_has_hits():
    baseline = {
        "liked_8_plus": 6,
        "loved_9_plus": 0,
        "candidate_8_plus_hits": 4,
        "candidate_8_plus_recall": 4/6,
        "candidate_9_plus_recall": None,
        "top25_8_plus_recall": 0.0,
        "top25_9_plus_recall": None,
        "top25_dislike_rate": 0.0,
        "top50_8_plus_hits": 0,
        "top50_9_plus_hits": 0,
    }
    challenger = dict(baseline)
    out = _event_guard(baseline, challenger)
    assert out["informative"] is False
    assert out["passed"] is False
    assert "NECONCLUDENT" in out["reason"]


def test_external_guard_can_be_informative_with_positive_top50_evidence():
    baseline = {
        "liked_8_plus": 6,
        "loved_9_plus": 0,
        "candidate_8_plus_hits": 4,
        "candidate_8_plus_recall": 4/6,
        "candidate_9_plus_recall": None,
        "top25_8_plus_recall": 1/6,
        "top25_9_plus_recall": None,
        "top25_dislike_rate": 0.0,
        "top50_8_plus_hits": 1,
        "top50_9_plus_hits": 0,
    }
    challenger = dict(baseline)
    challenger["top50_8_plus_hits"] = 2
    out = _event_guard(baseline, challenger)
    assert out["informative"] is True
    assert out["passed"] is True


def test_user_facing_engine_names_are_clear():
    source = open("cinecalendar/qt_ui_v2.py", encoding="utf-8").read()
    assert 'ENGINE_CURRENT_LABEL = "Stabil"' in source
    assert 'ENGINE_DISCOVERY_LABEL = "Descoperire"' in source
    assert 'ENGINE_PERSONAL_LABEL = "Adaptiv"' in source


def test_visible_decision_guard_compares_discovery_and_adaptive_separately():
    folds = [
        {
            "v16": {"matched": [{"rating": 8}]},
            "discovery": {"matched": [{"rating": 8}, {"rating": 9}]},
            "v5_20": {"matched": [{"rating": 8}, {"rating": 3}]},
        },
        {
            "v16": {"matched": [{"rating": 6}]},
            "discovery": {"matched": [{"rating": 8}]},
            "v5_20": {"matched": [{"rating": 7}]},
        },
    ]
    discovery = decision_replay_guard(folds, "discovery", "Descoperire")
    adaptive = decision_replay_guard(folds, "v5_20", "Adaptiv")
    assert discovery["passed"] is True
    assert discovery["discovery"]["liked_8_plus"] == 3
    assert adaptive["passed"] is False
    assert adaptive["v5_20"]["disliked_4_minus"] == 1
