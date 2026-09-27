from cinecalendar.v5_lab import V5LabRecommendationEngine
from cinecalendar.v5_shadow_ranker import V5ShadowRankedEngine


def test_v5_shadow_ranker_is_evaluation_only_subclass():
    assert issubclass(V5ShadowRankedEngine, V5LabRecommendationEngine)
    assert V5ShadowRankedEngine._blend(0.8, 0.2, 0.10) == 0.74
    assert V5ShadowRankedEngine._blend(0.2, 0.8, 0.10) == 0.26


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
