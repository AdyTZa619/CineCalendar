from __future__ import annotations
from pathlib import Path

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from .autoseed import ensure_initial_ratings
from .availability_guard_v37 import availability_engine_class
from .calendar_engine_v3 import ContextCalendarEngineV35
from .context_recommender_v35 import contextual_engine_class
from .db import Database
from .logging_setup import setup_logging
from .quality_manager_v37 import RecommendationQualityManagerV37
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

        # 3.7 keeps the current 3.6 production decision until a stricter personal rolling backtest
        # finishes. The challenger then differs from the proven V16/V17 baseline only by the local
        # retrieval lane, whose share is calibrated on this user's own non-overlapping time windows.
        self.quality_manager = RecommendationQualityManagerV37(self.db)
        engine_cls = self.quality_manager.preferred_engine_class()
        if not issubclass(engine_cls, FastRecommendationEngineV16):
            engine_cls = FastRecommendationEngineV16

        # Known future releases are removed without changing the order/scores of eligible titles.
        # Context 3.5 then wraps that exact engine; it must never collapse V18/V19 back to V16/V17.
        available_cls = availability_engine_class(engine_cls)
        production_cls = contextual_engine_class(available_cls)
        self.recommender = production_cls(self.db, self.calendar)

        # Preserve the validated long-term adaptive, Watch Success and 3.5 context layers.
        self.recommender.adaptive = AdaptivePreferenceLearnerV2(self.db)
        self.recommender.watch_intent = WatchSuccessIntentLearnerV33(self.db)
        self.recommender.collaborative.start_background()

        # Evaluation stays off the recommendation path. A new 3.7 decision becomes active only on
        # a later launch, so a running session never changes its engine underneath the user.
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
