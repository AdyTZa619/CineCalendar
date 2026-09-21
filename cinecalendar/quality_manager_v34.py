from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
import time

from .production_engine import RANKING_STACK_VERSION
from .recommendation_backtest import compare_quality_engines
from .recommender_v16 import FastRecommendationEngineV16
from .recommender_v17 import FastRecommendationEngineV17
from .util import utcnow_iso


QUALITY_MANAGER_VERSION = "quality-manager-v3.4.0"
QUALITY_SETTING = "recommendation_quality_v34"
INTERRUPTED_RETRY_COOLDOWN_SECONDS = 6 * 60 * 60
ERROR_RETRY_COOLDOWN_SECONDS = 60 * 60


class RecommendationQualityManager:
    """Locally decide whether V17 has earned promotion over V16 for this user.

    No personal ratings leave the machine. The manager fingerprints the durable rating state and
    the canonical ranking-stack version. Short-lived feedback remains part of live ranking, but it
    deliberately does not invalidate an expensive historical calibration.
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
        return "|".join(
            [
                QUALITY_MANAGER_VERSION,
                RANKING_STACK_VERSION,
                str(int(ratings[0] or 0)),
                str(ratings[1] or ""),
                str(ratings[2] or ""),
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
        payload["production_stack_version"] = RANKING_STACK_VERSION
        payload["preferred_engine"] = self.preferred_engine_class().__name__
        payload["background_running"] = bool(self._thread and self._thread.is_alive())
        return payload

    def _store(self, payload: dict) -> None:
        self.db.set_setting(QUALITY_SETTING, payload)

    @staticmethod
    def _age_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None

    def _retry_cooldown_remaining(self, payload: dict, token: str) -> float:
        if (
            str(payload.get("manager_version") or "") != QUALITY_MANAGER_VERSION
            or str(payload.get("state_token") or "") != token
        ):
            return 0.0
        status = str(payload.get("status") or "")
        if status in {"running", "interrupted_cooldown"}:
            age = self._age_seconds(payload.get("started_at"))
            if age is None:
                return 0.0
            return max(0.0, INTERRUPTED_RETRY_COOLDOWN_SECONDS - age)
        if status == "error":
            age = self._age_seconds(payload.get("completed_at"))
            if age is None:
                return 0.0
            return max(0.0, ERROR_RETRY_COOLDOWN_SECONDS - age)
        return 0.0

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
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

        cooldown = self._retry_cooldown_remaining(cached, token)
        if cooldown > 0:
            if str(cached.get("status") or "") == "running":
                interrupted = dict(cached)
                interrupted.update(
                    {
                        "status": "interrupted_cooldown",
                        "worker_pid": 0,
                        "interrupted_at": utcnow_iso(),
                        "retry_after_seconds": int(cooldown),
                    }
                )
                self._store(interrupted)
            return False

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
                    "production_stack_version": RANKING_STACK_VERSION,
                    "status": "insufficient_ratings",
                    "rating_count": count,
                    "minimum_ratings": self.MIN_RATINGS,
                    "preferred_engine": FastRecommendationEngineV16.__name__,
                    "updated_at": utcnow_iso(),
                }
            )
            return False

        with self._lock:
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
                        "production_stack_version": RANKING_STACK_VERSION,
                        "status": "running",
                        "rating_count": count,
                        "worker_pid": int(os.getpid()),
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
                            "production_stack_version": RANKING_STACK_VERSION,
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
                            "production_stack_version": RANKING_STACK_VERSION,
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
