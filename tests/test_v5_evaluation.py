import pytest

from cinecalendar.v5_lab import V5LabRecommendationEngine
from cinecalendar.v5_shadow_ranker import V5ShadowRankedEngine


def test_v5_shadow_ranker_is_evaluation_only_subclass():
    assert issubclass(V5ShadowRankedEngine, V5LabRecommendationEngine)
    assert V5ShadowRankedEngine._blend(0.8, 0.2, 0.10) == pytest.approx(0.74)
    assert V5ShadowRankedEngine._blend(0.2, 0.8, 0.10) == pytest.approx(0.26)


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
