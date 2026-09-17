from __future__ import annotations
from pathlib import Path

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from .autoseed import ensure_initial_ratings
from .calendar_engine_v3 import ContextCalendarEngineV35
from .context_recommender_v35 import contextual_engine_class
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
        self.calendar = ContextCalendarEngineV35()

        # 3.4 chooses only the measured V16/V17 taste baseline. 3.5 wraps that exact verdict
        # with bounded context intelligence; context never becomes a replacement taste model.
        self.quality_manager = RecommendationQualityManager(self.db)
        engine_cls = self.quality_manager.preferred_engine_class()
        if not issubclass(engine_cls, FastRecommendationEngineV16):
            engine_cls = FastRecommendationEngineV16
        production_cls = contextual_engine_class(engine_cls)
        self.recommender = production_cls(self.db, self.calendar)

        # Keep the validated long-term adaptive and exposure-level Watch Success learners.
        self.recommender.adaptive = AdaptivePreferenceLearnerV2(self.db)
        self.recommender.watch_intent = WatchSuccessIntentLearnerV33(self.db)
        self.recommender.collaborative.start_background()

        # A fresh rating/feedback state can trigger the local V16 vs V17 backtest in the
        # background. The result is used on the next start and is then wrapped by 3.5.
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
