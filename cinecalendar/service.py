from __future__ import annotations
from pathlib import Path

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from .autoseed import ensure_initial_ratings
from .calendar_engine_v2 import RichCalendarEngine
from .db import Database
from .logging_setup import setup_logging
from .recommender_v16 import FastRecommendationEngineV16
from .util import AppPaths
from .watch_success_v33 import WatchSuccessIntentLearnerV33


class CineCalendarService:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.portable()
        self.log = setup_logging(self.paths.logs)
        self.db = Database(self.paths.data / "cinecalendar.db")
        self._defaults()
        self.initial_ratings_state = ensure_initial_ratings(self.db, self.paths.root.parent, self.log)
        self.calendar = RichCalendarEngine()
        self.recommender = FastRecommendationEngineV16(self.db, self.calendar)
        # Production composition uses the validated adaptive V2 learner and the v3.3 exposure-level
        # Watch Success learner. Base classes remain import-compatible for old tests/backups.
        self.recommender.adaptive = AdaptivePreferenceLearnerV2(self.db)
        self.recommender.watch_intent = WatchSuccessIntentLearnerV33(self.db)
        self.recommender.collaborative.start_background()

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
