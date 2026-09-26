from __future__ import annotations
from pathlib import Path

from .autoseed import ensure_initial_ratings
from .calendar_engine_v3 import ContextCalendarEngineV35
from .db import Database
from .logging_setup import setup_logging
from .production_engine import build_production_recommender, production_stack_status
from .quality_manager_v47 import RecommendationQualityManagerV47
from .recommender_v16 import FastRecommendationEngineV16
from .temp_workspaces import cleanup_abandoned_workspaces
from .util import AppPaths
from .full_catalog_shadow_v414 import FullCatalogShadowEvaluatorV414
from .v5_alpha_runtime import ensure_alpha_database, is_v5_alpha
from .v5_lab import V5LabRecommendationEngine


class CineCalendarService:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.portable()
        self.v5_alpha = is_v5_alpha()
        self.alpha_bootstrap = (
            ensure_alpha_database(self.paths.root)
            if self.v5_alpha
            else {"state": "stable"}
        )
        self.log = setup_logging(self.paths.logs)
        if self.v5_alpha:
            self.log.info(
                "CineCalendar V5 Alpha runtime: database=%s source=%s state=%s",
                self.alpha_bootstrap.get("target", ""),
                self.alpha_bootstrap.get("source", ""),
                self.alpha_bootstrap.get("state", ""),
            )
        temp_cleanup = cleanup_abandoned_workspaces()
        if temp_cleanup["removed"] or temp_cleanup["failed"]:
            self.log.info(
                "Backtest temp cleanup: removed=%s active=%s recent_legacy=%s failed=%s",
                temp_cleanup["removed"],
                temp_cleanup["active"],
                temp_cleanup["recent_legacy"],
                temp_cleanup["failed"],
            )
        self.db = Database(self.paths.data / "cinecalendar.db")
        self._defaults()
        self.initial_ratings_state = ensure_initial_ratings(self.db, self.paths.root.parent, self.log)
        self.calendar = ContextCalendarEngineV35()

        # 4.6 keeps the current validated engine and hybrid balance until stricter personal rolling
        # backtests have a verdict. Production and evaluation share the same canonical wrappers.
        self.quality_manager = RecommendationQualityManagerV47(self.db)
        if self.v5_alpha:
            # Alpha must exercise the real V5 retrieval/pipeline while keeping all proven
            # production wrappers around it. Stable selection and rollback policy remain untouched.
            engine_cls = V5LabRecommendationEngine
        else:
            self.quality_manager.refresh_live_guard()
            engine_cls = self.quality_manager.preferred_engine_class()
            if not isinstance(engine_cls, type) or not issubclass(engine_cls, FastRecommendationEngineV16):
                engine_cls = FastRecommendationEngineV16

        self.recommender = build_production_recommender(self.db, engine_cls, self.calendar)
        self.quality_manager.set_runtime_engine(self.recommender)
        self.production_stack = production_stack_status(self.recommender)
        self.shadow_retrieval = FullCatalogShadowEvaluatorV414(
            self.db,
            self.recommender.collaborative,
            str(self.production_stack.get("recommendation_engine_identity") or ""),
        )
        self.recommender.collaborative.start_background()

        # Stable keeps its established quality-manager schedule. Alpha deliberately freezes that
        # selector so its measurements are about V5, not a background switch to another engine.
        if not self.v5_alpha:
            self.quality_manager.start_background()

    def _defaults(self):
        # No global genre vetoes. Taste is learned from ratings instead of hard exclusions.
        if self.db.get_setting("auto_watch_enabled", None) is None:
            self.db.set_setting("auto_watch_enabled", True)
        if self.db.get_setting("imdb_public_sync_enabled", None) is None:
            self.db.set_setting("imdb_public_sync_enabled", True)
        if self.db.get_setting("imdb_public_ratings_url", None) is None:
            self.db.set_setting("imdb_public_ratings_url", "https://www.imdb.com/user/p.666yozwb6likjcvvjlu2hwmtli/ratings/")
        if self.db.get_setting("imdb_public_sync_baseline", None) is None:
            self.db.set_setting("imdb_public_sync_baseline", "2026-09-05")
        if self.db.get_setting("ratings_folder", None) is None:
            self.db.set_setting("ratings_folder", str(Path.home() / "Downloads"))
        if self.db.get_setting("theme", None) is None:
            self.db.set_setting("theme", "dark")
        if self.db.get_setting("chooser_runtime_bucket", None) is None:
            self.db.set_setting("chooser_runtime_bucket", "all")
        if self.db.get_setting("chooser_mood", None) is None:
            self.db.set_setting("chooser_mood", "neutral")
        if self.db.get_setting("watchlist_decision_mode", None) is None:
            self.db.set_setting("watchlist_decision_mode", "decide")
        self.db.set_setting("catalog_bootstrap_running", False)
