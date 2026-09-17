from __future__ import annotations

import threading
import time

from .production_engine import PRODUCTION_STACK_VERSION
from .recommendation_backtest import compare_quality_engines
from .recommender_v16 import FastRecommendationEngineV16
from .recommender_v17 import FastRecommendationEngineV17
from .util import utcnow_iso


QUALITY_MANAGER_VERSION = "quality-manager-v3.4.0"
QUALITY_SETTING = "recommendation_quality_v34"


class RecommendationQualityManager:
    """Locally decide whether V17 has earned promotion over V16 for this user.

    No personal ratings leave the machine. The manager fingerprints the current local rating/
    feedback state and the canonical production-stack version. A stack change therefore forces a
    fresh local comparison instead of silently reusing a verdict measured under different wrappers.
    """

    MIN_RATINGS = 80

    def __init__(self, db):
        self.db = db
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def state_token(self) -> str:
        with self.db.connect() as con:
            ratings = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),''),COALESCE(MAX(date_rated),'') FROM ratings"
            ).fetchone()
            feedback = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback"
            ).fetchone()
        return "|".join(
            [
                QUALITY_MANAGER_VERSION,
                PRODUCTION_STACK_VERSION,
                str(int(ratings[0] or 0)),
                str(ratings[1] or ""),
                str(ratings[2] or ""),
                str(int(feedback[0] or 0)),
                str(feedback[1] or ""),
            ]
        )

    def rating_count(self) -> int:
        with self.db.connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])

    def cached_report(self) -> dict:
        payload = self.db.get_setting(QUALITY_SETTING, {})
        return dict(payload) if isinstance(payload, dict) else {}

    def preferred_engine_class(self):
        token = self.state_token()
        payload = self.cached_report()
        if (
            str(payload.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(payload.get("state_token") or "") == token
            and str(payload.get("status") or "") == "completed"
            and bool((payload.get("comparison") or {}).get("aggregate", {}).get("approved"))
        ):
            return FastRecommendationEngineV17
        return FastRecommendationEngineV16

    def status(self) -> dict:
        payload = self.cached_report()
        payload["current_state_token"] = self.state_token()
        payload["production_stack_version"] = PRODUCTION_STACK_VERSION
        payload["preferred_engine"] = self.preferred_engine_class().__name__
        payload["background_running"] = bool(self._thread and self._thread.is_alive())
        return payload

    def _store(self, payload: dict) -> None:
        self.db.set_setting(QUALITY_SETTING, payload)

    def start_background(
        self,
        *,
        delay_seconds: float = 45.0,
        candidate_limit: int = 1800,
        final_limit: int = 100,
        als_timeout: float = 180.0,
    ) -> bool:
        token = self.state_token()
        count = self.rating_count()
        cached = self.cached_report()
        if (
            str(cached.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(cached.get("state_token") or "") == token
            and str(cached.get("status") or "") == "completed"
        ):
            return False
        if count < self.MIN_RATINGS:
            self._store(
                {
                    "manager_version": QUALITY_MANAGER_VERSION,
                    "state_token": token,
                    "production_stack_version": PRODUCTION_STACK_VERSION,
                    "status": "insufficient_ratings",
                    "rating_count": count,
                    "minimum_ratings": self.MIN_RATINGS,
                    "preferred_engine": FastRecommendationEngineV16.__name__,
                    "updated_at": utcnow_iso(),
                }
            )
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            def worker() -> None:
                if delay_seconds > 0:
                    time.sleep(float(delay_seconds))
                current_token = self.state_token()
                if current_token != token:
                    return
                self._store(
                    {
                        "manager_version": QUALITY_MANAGER_VERSION,
                        "state_token": token,
                        "production_stack_version": PRODUCTION_STACK_VERSION,
                        "status": "running",
                        "rating_count": count,
                        "started_at": utcnow_iso(),
                        "preferred_engine": FastRecommendationEngineV16.__name__,
                    }
                )
                try:
                    comparison = compare_quality_engines(
                        self.db.path,
                        fractions=(0.20,),
                        candidate_limit=candidate_limit,
                        final_limit=final_limit,
                        als_timeout=als_timeout,
                    )
                    approved = bool(comparison.get("aggregate", {}).get("approved"))
                    preferred = FastRecommendationEngineV17.__name__ if approved else FastRecommendationEngineV16.__name__
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": token,
                            "production_stack_version": PRODUCTION_STACK_VERSION,
                            "status": "completed",
                            "rating_count": count,
                            "preferred_engine": preferred,
                            "comparison": comparison,
                            "completed_at": utcnow_iso(),
                        }
                    )
                except Exception as exc:
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": token,
                            "production_stack_version": PRODUCTION_STACK_VERSION,
                            "status": "error",
                            "rating_count": count,
                            "preferred_engine": FastRecommendationEngineV16.__name__,
                            "error": str(exc),
                            "completed_at": utcnow_iso(),
                        }
                    )

            self._thread = threading.Thread(
                target=worker,
                name="CineCalendar-Quality-V34",
                daemon=True,
            )
            self._thread.start()
            return True
