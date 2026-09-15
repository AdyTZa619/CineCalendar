from __future__ import annotations

import inspect

from cinecalendar.db import Database
from cinecalendar.performance_ui_patch import install_performance_ui_patch
from cinecalendar.recommender_v11 import FastRecommendationEngineV11
from cinecalendar.recommender_v13 import FastRecommendationEngineV13
from cinecalendar.service import CineCalendarService
from cinecalendar.watcher import RatingsFolderWatcher


def _db(tmp_path) -> Database:
    return Database(tmp_path / "CineCalendarData" / "data" / "cinecalendar.db")


def test_watcher_ignores_unrelated_csv_and_missing_folder(tmp_path):
    db = _db(tmp_path)
    missing = RatingsFolderWatcher(db, tmp_path / "does-not-exist")
    assert missing.scan() == []

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "unrelated.csv").write_text("foo,bar\n1,2\n", encoding="utf-8")
    assert RatingsFolderWatcher(db, downloads).scan() == []


def test_watcher_imports_valid_imdb_export_once(tmp_path):
    db = _db(tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    export = downloads / "ratings.csv"
    export.write_text(
        "Const,Your Rating,Date Rated,Title,Title Type\n"
        "tt1234567,9,2026-09-15,Example Film,Movie\n",
        encoding="utf-8",
    )

    watcher = RatingsFolderWatcher(db, downloads)
    first = watcher.scan()
    second = watcher.scan()

    assert len(first) == 1
    assert first[0].new_ratings == [("Example Film", 9)]
    assert second == []
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM import_files").fetchone()[0] == 1


def test_sqlite_wal_mode_is_not_reapplied_on_every_connection():
    connect_src = inspect.getsource(Database.connect)
    migrate_src = inspect.getsource(Database.migrate)
    assert 'execute("PRAGMA journal_mode=WAL")' not in connect_src
    assert 'execute("PRAGMA journal_mode=WAL")' in migrate_src
    assert "busy_timeout=5000" in connect_src


def test_performance_patch_defers_auto_watcher_during_foreground_work():
    src = inspect.getsource(install_performance_ui_patch)
    assert "_foreground_busy" in src
    assert "today_worker" in src
    assert "calendar_worker" in src
    assert "metadata_worker" in src
    assert "ratings_watch_failures" in src
    assert "worker.deleteLater" in src
    assert "self.sender()" in src
    assert "60_000" in src


def test_large_internal_v13_pool_skips_v11_quadratic_diversity_and_explanations():
    select_src = inspect.getsource(FastRecommendationEngineV11._select_candidates)
    recommend_src = inspect.getsource(FastRecommendationEngineV11.recommend)
    assert "DIVERSITY_SHORTLIST_THRESHOLD" in select_src
    assert "candidates[:count]" in select_src
    assert "int(count) <= DIVERSITY_SHORTLIST_THRESHOLD" in recommend_src


def test_adaptive_warmup_is_staggered_until_after_base_recommendation():
    service_src = inspect.getsource(CineCalendarService.__init__)
    adaptive_src = inspect.getsource(FastRecommendationEngineV13._adaptive_rerank)
    assert "collaborative.start_background" in service_src
    assert "start_adaptive_background()" not in service_src
    assert "start_adaptive_background()" in adaptive_src


def test_final_visible_results_restore_als_explanations_only_after_rerank():
    helper_src = inspect.getsource(FastRecommendationEngineV13._annotate_final_als)
    recommend_src = inspect.getsource(FastRecommendationEngineV13.recommend)
    romanian_src = inspect.getsource(FastRecommendationEngineV13.recommend_romanian)
    assert "_annotate_als_explanations" in helper_src
    assert "Preferințe adaptive locale" in helper_src
    assert "_annotate_final_als(selected)" in recommend_src
    assert "_annotate_final_als(selected)" in romanian_src
