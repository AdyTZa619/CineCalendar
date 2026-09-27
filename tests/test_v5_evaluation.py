import pytest

from cinecalendar.v5_lab import V5LabRecommendationEngine
from cinecalendar.v5_event_replay import aggregate_event_reports
from cinecalendar.v5_shadow_ranker import (
    V5ShadowRankedEngine,
    V5ShadowRankedEngine10,
    V5ShadowRankedEngine15,
    V5ShadowRankedEngine20,
)


def test_v5_shadow_ranker_is_evaluation_only_subclass():
    assert issubclass(V5ShadowRankedEngine, V5LabRecommendationEngine)
    assert V5ShadowRankedEngine._blend(0.8, 0.2, 0.10) == pytest.approx(0.74)
    assert V5ShadowRankedEngine._blend(0.2, 0.8, 0.10) == pytest.approx(0.26)
    assert V5ShadowRankedEngine10.SHADOW_BLEND_OVERRIDE == pytest.approx(0.10)
    assert V5ShadowRankedEngine15.SHADOW_BLEND_OVERRIDE == pytest.approx(0.15)
    assert V5ShadowRankedEngine20.SHADOW_BLEND_OVERRIDE == pytest.approx(0.20)


def test_v5_evaluator_wires_three_way_historical_comparison():
    source = open("cinecalendar/v5_evaluation.py", encoding="utf-8").read()
    assert "FastRecommendationEngineV16" in source
    assert "V5LabRecommendationEngine" in source
    assert "V5ShadowRankedEngine" in source
    assert "rolling_windows(" in source
    assert "event_replay_windows(" in source
    assert "run_window_backtest_group(" in source
    assert "eligible_for_visible_alpha_trial" in source
    assert '"visible_ranking_changed": False' in source


def test_v5_lab_is_exposed_only_as_alpha_runtime_page():
    source = open("cinecalendar/qt_ui_v2.py", encoding="utf-8").read()
    assert '("v5_lab", "V5 Lab")' in source
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
