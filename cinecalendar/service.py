from __future__ import annotations
from pathlib import Path

from .autoseed import ensure_initial_ratings
from .calendar_engine_v3 import ContextCalendarEngineV35
from .db import Database
from .logging_setup import setup_logging
from .production_engine import build_production_recommender, production_stack_status
from .quality_manager_v37 import RecommendationQualityManagerV37
from .recommender_v16 import FastRecommendationEngineV16
from .util import AppPaths


class CineCalendarService:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.portable()
        self.log = setup_logging(self.paths.logs)
        self.db = Database(self.paths.data / "cinecalendar.db")
        self._defaults()
        self.initial_ratings_state = ensure_initial_ratings(self.db, self.paths.root.parent, self.log)
        self.calendar = ContextCalendarEngineV35()

        # 3.7 keeps the current validated engine until a stricter personal rolling backtest has a
        # verdict. 3.8 centralizes every wrapper/learner so production and evaluation cannot drift.
        self.quality_manager = RecommendationQualityManagerV37(self.db)
        engine_cls = self.quality_manager.preferred_engine_class()
        if not isinstance(engine_cls, type) or not issubclass(engine_cls, FastRecommendationEngineV16):
            engine_cls = FastRecommendationEngineV16

        self.recommender = build_production_recommender(self.db, engine_cls, self.calendar)
        self.production_stack = production_stack_status(self.recommender)
        self.recommender.collaborative.start_background()

        # Evaluation stays off the recommendation path. A new 3.7 decision becomes active only on
        # a later launch, so a running session never changes its engine underneath the user.
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
        self.db.set_setting("catalog_bootstrap_running", False)
