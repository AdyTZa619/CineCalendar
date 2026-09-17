from __future__ import annotations
from pathlib import Path

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from .autoseed import ensure_initial_ratings
from .calendar_engine_v2 import RichCalendarEngine
from .db import Database
from .logging_setup import setup_logging
from .quality_manager_v34 import RecommendationQualityManager
from .recommender_v16 import FastRecommendationEngineV16
from .watch_success_v33 import WatchSuccessIntentLearnerV33
from .util import AppPaths


class CineCalendarService:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.portable()
        self.log = setup_logging(self.paths.logs)
        self.db = Database(self.paths.data / "cinecalendar.db")
        self._defaults()
        self.initial_ratings_state = ensure_initial_ratings(self.db, self.paths.root.parent, self.log)
        self.calendar = RichCalendarEngine()

        # 3.4 never promotes a newer recommender merely because it exists. A local temporal A/B
        # backtest on the user's own ratings must approve V17; otherwise V16 remains production.
        self.quality_manager = RecommendationQualityManager(self.db)
        engine_cls = self.quality_manager.preferred_engine_class()

        # V16 is the hard production compatibility/safety baseline. A future quality manager is
        # not allowed to inject an unrelated engine class even if its cached verdict is malformed.
        # V17 deliberately subclasses V16, preserving the Top-3 trust gate and all established
        # daily-genre/adaptive/Watch-Success behavior while changing only measured quality layers.
        if not issubclass(engine_cls, FastRecommendationEngineV16):
            engine_cls = FastRecommendationEngineV16
        self.recommender = engine_cls(self.db, self.calendar)

        # Production composition uses the validated adaptive V2 learner and the v3.3 exposure-level
        # Watch Success learner. Base classes remain import-compatible for old tests/backups.
        self.recommender.adaptive = AdaptivePreferenceLearnerV2(self.db)
        self.recommender.watch_intent = WatchSuccessIntentLearnerV33(self.db)
        self.recommender.collaborative.start_background()

        # Run challenger evaluation after startup, off the UI/recommendation path. A successful
        # verdict is persisted and becomes active on the next application start while the same
        # rating/feedback state remains current.
        self.quality_manager.start_background()

    def _defaults(self):
        # Genre choice is contextual/day-specific; no hidden global Romance veto.
        self.db.set_setting("exclude_romance", False)
        if self.db.get_setting("auto_watch_enabled", None) is None:
            self.db.set_setting("auto_watch_enabled", True)
        if self.db.get_setting("ratings_folder", None) is None:
            self.db.set_setting("ratings_folder", str(Path.home() / "Downloads"))
        if self.db.get_setting("theme", None) is None:
            self.db.set_setting("theme", "dark")
        self.db.set_setting("catalog_bootstrap_running", False)
