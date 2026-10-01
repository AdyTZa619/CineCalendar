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
from .v5_lab import V5LabRecommendationEngine, discovery_engine_class
from .v5_visible_trial import AlphaTrialRecommender, V5VisibleTrialEngine20
from .v5_shadow_ranker import adaptive_engine_class
from .engine_modes import EngineModeRouter


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

        # One executable / one DB: Stabil is the validated production base. Descoperire and
        # Adaptiv are lazy guarded layers over that exact taste engine and use the same database.
        self.quality_manager = RecommendationQualityManagerV47(self.db)
        self.quality_manager.refresh_live_guard()
        engine_cls = self.quality_manager.preferred_engine_class()
        if not isinstance(engine_cls, type) or not issubclass(engine_cls, FastRecommendationEngineV16):
            engine_cls = FastRecommendationEngineV16
        self.stable_engine_class = engine_cls
        self.stable_recommender = build_production_recommender(
            self.db, engine_cls, self.calendar
        )
        self.stable_recommender.collaborative.start_background()

        def build_discovery():
            report = self.db.get_setting("v5_evaluation_report", {}) or {}
            decision = report.get("decision") if isinstance(report, dict) else {}
            variant = str((decision or {}).get("selected_discovery_variant") or "balanced")
            cls = discovery_engine_class(engine_cls, variant)
            return build_production_recommender(self.db, cls, self.calendar)

        def build_adaptive():
            report = self.db.get_setting("v5_evaluation_report", {}) or {}
            decision = report.get("decision") if isinstance(report, dict) else {}
            variant = str((decision or {}).get("selected_discovery_variant") or "balanced")
            cls = adaptive_engine_class(engine_cls, variant, 0.20)
            return build_production_recommender(self.db, cls, self.calendar)

        self.recommender = EngineModeRouter(
            self.db,
            self.stable_recommender,
            build_discovery,
            build_adaptive,
            stable_identity=recommendation_engine_identity(engine_cls),
        )
        runtime_engine = self.recommender.active

        self.quality_manager.set_runtime_engine(runtime_engine)
        self.production_stack = production_stack_status(runtime_engine)
        self.shadow_retrieval = FullCatalogShadowEvaluatorV414(
            self.db,
            runtime_engine.collaborative,
            str(self.production_stack.get("recommendation_engine_identity") or ""),
        )

        # Stable keeps its established quality-manager schedule. Alpha deliberately freezes that
        # selector so its measurements are about the explicit V16/V5 trial, not a background switch.
        if not self.v5_alpha:
            self.quality_manager.start_background()

    def engine_mode_status(self) -> dict:
        status = getattr(self.recommender, "status", None)
        if not callable(status):
            return {"available": False, "mode": "stable"}
        return dict(status())

    def set_engine_mode(self, mode: str) -> dict:
        setter = getattr(self.recommender, "set_mode", None)
        if not callable(setter):
            raise RuntimeError("Schimbarea motorului nu este disponibilă.")
        status = dict(setter(mode))
        runtime_engine = self.recommender.active
        self.quality_manager.set_runtime_engine(runtime_engine)
        self.production_stack = production_stack_status(runtime_engine)
        self.shadow_retrieval = FullCatalogShadowEvaluatorV414(
            self.db,
            runtime_engine.collaborative,
            str(self.production_stack.get("recommendation_engine_identity") or ""),
        )
        self.log.info(
            "Recommendation engine mode changed: mode=%s discovery=%s adaptive=%s",
            status.get("mode"),
            status.get("discovery_eligible"),
            status.get("adaptive_eligible"),
        )
        return status

    # Backward-compatible aliases for older Alpha UI code.
    def alpha_trial_status(self) -> dict:
        return self.engine_mode_status()

    def set_alpha_trial_mode(self, mode: str) -> dict:
        mapped = {"v16": "stable", "v5_20": "adaptive"}.get(str(mode), str(mode))
        return self.set_engine_mode(mapped)

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
