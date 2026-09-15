import inspect

from cinecalendar.performance_ui_patch import install_performance_ui_patch
from cinecalendar.recommender_v9 import FastRecommendationEngineV9


def test_candidate_discovery_is_skinny_before_hydration():
    src = inspect.getsource(FastRecommendationEngineV9._balanced_candidate_ids)
    assert "SELECT m.*" not in src
    assert src.count("SELECT m.id") >= 5

    hydrate = inspect.getsource(FastRecommendationEngineV9._hydrate_ids)
    assert "ORDER BY id" in hydrate
    assert "HYDRATE_CHUNK" in inspect.getsource(FastRecommendationEngineV9)


def test_daily_decision_pool_is_persistent():
    src = inspect.getsource(FastRecommendationEngineV9.decision_pick)
    assert "_load_persisted_decision_pool" in src
    assert "_save_persisted_decision_pool" in src
    assert "_state_token" in inspect.getsource(FastRecommendationEngineV9._load_persisted_decision_pool)


def test_ui_performance_patch_avoids_legacy_left_join_and_sync_watcher():
    src = inspect.getsource(install_performance_ui_patch)
    assert "LEFT JOIN" not in src.split("def catalog_count", 1)[1].split("def scan_ratings_folder", 1)[0]
    assert "60_000" in src
    assert "WorkerThread" in src
    assert "build_profile" not in src
