from __future__ import annotations
from pathlib import Path

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from .autoseed import ensure_initial_ratings
from .calendar_engine_v2 import RichCalendarEngine
from .db import Database
from .logging_setup import setup_logging
from .recommender_v15 import FastRecommendationEngineV15
from .util import AppPaths


class CineCalendarService:
    def __init__(self, paths: AppPaths | None = None):
        self.paths = paths or AppPaths.portable()
        self.log = setup_logging(self.paths.logs)
        self.db = Database(self.paths.data / "cinecalendar.db")
        self._defaults()
        self.initial_ratings_state = ensure_initial_ratings(self.db, self.paths.root.parent, self.log)
        self.calendar = RichCalendarEngine()
        self.recommender = FastRecommendationEngineV15(self.db, self.calendar)
        # V13 constructs the legacy adaptive learner for backwards-compatible engine composition.
        # The production service replaces it immediately with V2: a larger feature space and a
        # temporal ranking/calibration quality gate determine how much adaptive influence is earned.
        self.recommender.adaptive = AdaptivePreferenceLearnerV2(self.db)
        # ALS may warm in the background immediately because it is a read-mostly model load.
        # Do NOT start the adaptive trainer here: with thousands of ratings it walks the local DB
        # and uses CPU at exactly the moment Home is computing its first pool. V13-V15 start it only
        # after the base recommendation has already been ranked, so first paint gets disk priority.
        # Watch Success/Startability are deliberately lightweight and lazy.
        self.recommender.collaborative.start_background()

    def _defaults(self):
        # Filtrul global Romance este retras. Preferințele reale vin din ratinguri/ALS și din
        # modelul adaptiv local; alegerea de gen rămâne doar o intenție opțională pentru ziua curentă.
        self.db.set_setting("exclude_romance", False)
        if self.db.get_setting("auto_watch_enabled", None) is None:
            self.db.set_setting("auto_watch_enabled", True)
        if self.db.get_setting("ratings_folder", None) is None:
            self.db.set_setting("ratings_folder", str(Path.home() / "Downloads"))
        if self.db.get_setting("theme", None) is None:
            self.db.set_setting("theme", "dark")
        self.db.set_setting("catalog_bootstrap_running", False)
