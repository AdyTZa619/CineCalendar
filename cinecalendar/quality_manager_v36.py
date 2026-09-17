from __future__ import annotations

import threading
import time

from .quality_manager_v34 import RecommendationQualityManager
from .recommendation_backtest import compare_quality_engines
from .recommender_v18 import FastRecommendationEngineV18
from .util import utcnow_iso


QUALITY_MANAGER_VERSION = "quality-manager-v3.6.0"
QUALITY_SETTING = "recommendation_quality_v36"


class RecommendationQualityManagerV36:
    """Promote V18 only if it beats the already-approved 3.4 baseline on two temporal folds.

    The current V16/V17 verdict remains the production baseline. V18 is a challenger, never an
    automatic upgrade. Promotion is cached against the exact rating/feedback state and becomes
    active only on a later start, so a running session never changes engine underneath the user.
    """

    MIN_RATINGS = 100

    def __init__(self, db):
        self.db = db
        self.baseline_manager = RecommendationQualityManager(db)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def baseline_engine_class(self):
        return self.baseline_manager.preferred_engine_class()

    def state_token(self) -> str:
        baseline = self.baseline_engine_class().__name__
        return "|".join([QUALITY_MANAGER_VERSION, self.baseline_manager.state_token(), baseline])

    def rating_count(self) -> int:
        return self.baseline_manager.rating_count()

    def cached_report(self) -> dict:
        payload = self.db.get_setting(QUALITY_SETTING, {})
        return dict(payload) if isinstance(payload, dict) else {}

    def _store(self, payload: dict) -> None:
        self.db.set_setting(QUALITY_SETTING, payload)

    @staticmethod
    def _strict_approval(comparison: dict) -> dict:
        folds = list(comparison.get("folds") or [])
        aggregate = comparison.get("aggregate") or {}
        if len(folds) < 2:
            return {"approved": False, "reason": "insufficient_folds"}

        verdicts = [fold.get("verdict") or {} for fold in folds]
        all_fold_wins = all(bool(v.get("approved")) for v in verdicts)
        tight = all(
            float(v.get("candidate_8_plus_delta", 0.0) or 0.0) >= -0.01
            and float(v.get("candidate_9_plus_delta", 0.0) or 0.0) >= -0.015
            and float(v.get("final_dislike_delta", 0.0) or 0.0) <= 0.01
            and float(v.get("ndcg25_delta", 0.0) or 0.0) >= -0.005
            for v in verdicts
        )
        mean_gain = float(aggregate.get("mean_composite_delta", -1.0) or -1.0)
        meaningful = any(
            float(v.get("candidate_8_plus_delta", 0.0) or 0.0) > 0.0
            or float(v.get("candidate_9_plus_delta", 0.0) or 0.0) > 0.0
            or float(v.get("ndcg25_delta", 0.0) or 0.0) > 0.0
            for v in verdicts
        )
        approved = bool(
            aggregate.get("approved")
            and all_fold_wins
            and tight
            and meaningful
            and mean_gain >= 0.012
        )
        return {
            "approved": approved,
            "all_fold_wins": all_fold_wins,
            "tight_guardrails": tight,
            "meaningful_improvement": meaningful,
            "mean_composite_delta": mean_gain,
            "minimum_mean_gain": 0.012,
            "fold_count": len(folds),
        }

    def preferred_engine_class(self):
        baseline = self.baseline_engine_class()
        token = self.state_token()
        payload = self.cached_report()
        if (
            str(payload.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(payload.get("state_token") or "") == token
            and str(payload.get("status") or "") == "completed"
            and bool((payload.get("strict_verdict") or {}).get("approved"))
        ):
            return FastRecommendationEngineV18
        return baseline

    def status(self) -> dict:
        payload = self.cached_report()
        baseline_status = self.baseline_manager.status()
        payload["current_state_token"] = self.state_token()
        payload["preferred_engine"] = self.preferred_engine_class().__name__
        payload["baseline_engine"] = self.baseline_engine_class().__name__
        payload["background_running"] = bool(self._thread and self._thread.is_alive())
        payload["baseline_v34"] = baseline_status
        return payload

    def start_background(
        self,
        *,
        delay_seconds: float = 70.0,
        candidate_limit: int = 2200,
        final_limit: int = 100,
        als_timeout: float = 180.0,
    ) -> bool:
        count = self.rating_count()
        if count < self.MIN_RATINGS:
            self._store(
                {
                    "manager_version": QUALITY_MANAGER_VERSION,
                    "state_token": self.state_token(),
                    "status": "insufficient_ratings",
                    "rating_count": count,
                    "minimum_ratings": self.MIN_RATINGS,
                    "preferred_engine": self.baseline_engine_class().__name__,
                    "updated_at": utcnow_iso(),
                }
            )
            return False

        # Settle the existing V16/V17 decision first. 3.6 never moves the baseline while its own
        # experiment is running.
        base_token = self.baseline_manager.state_token()
        base_report = self.baseline_manager.cached_report()
        base_ready = (
            str(base_report.get("manager_version") or "") == "quality-manager-v3.4.0"
            and str(base_report.get("state_token") or "") == base_token
            and str(base_report.get("status") or "") == "completed"
        )
        if not base_ready:
            self.baseline_manager.start_background(delay_seconds=min(45.0, max(0.0, delay_seconds / 2.0)))
            self._store(
                {
                    "manager_version": QUALITY_MANAGER_VERSION,
                    "state_token": self.state_token(),
                    "status": "waiting_for_v34",
                    "rating_count": count,
                    "preferred_engine": self.baseline_engine_class().__name__,
                    "updated_at": utcnow_iso(),
                }
            )
            return False

        token = self.state_token()
        cached = self.cached_report()
        if (
            str(cached.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(cached.get("state_token") or "") == token
            and str(cached.get("status") or "") == "completed"
        ):
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            baseline_cls = self.baseline_engine_class()

            def worker() -> None:
                if delay_seconds > 0:
                    time.sleep(float(delay_seconds))
                if self.state_token() != token:
                    return
                self._store(
                    {
                        "manager_version": QUALITY_MANAGER_VERSION,
                        "state_token": token,
                        "status": "running",
                        "rating_count": count,
                        "baseline_engine": baseline_cls.__name__,
                        "challenger_engine": FastRecommendationEngineV18.__name__,
                        "preferred_engine": baseline_cls.__name__,
                        "started_at": utcnow_iso(),
                    }
                )
                try:
                    comparison = compare_quality_engines(
                        self.db.path,
                        fractions=(0.15, 0.25),
                        candidate_limit=candidate_limit,
                        final_limit=final_limit,
                        als_timeout=als_timeout,
                        baseline_cls=baseline_cls,
                        challenger_cls=FastRecommendationEngineV18,
                    )
                    strict = self._strict_approval(comparison)
                    preferred = FastRecommendationEngineV18.__name__ if strict["approved"] else baseline_cls.__name__
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": token,
                            "status": "completed",
                            "rating_count": count,
                            "baseline_engine": baseline_cls.__name__,
                            "challenger_engine": FastRecommendationEngineV18.__name__,
                            "preferred_engine": preferred,
                            "comparison": comparison,
                            "strict_verdict": strict,
                            "completed_at": utcnow_iso(),
                        }
                    )
                except Exception as exc:
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": token,
                            "status": "error",
                            "rating_count": count,
                            "baseline_engine": baseline_cls.__name__,
                            "challenger_engine": FastRecommendationEngineV18.__name__,
                            "preferred_engine": baseline_cls.__name__,
                            "error": str(exc),
                            "completed_at": utcnow_iso(),
                        }
                    )

            self._thread = threading.Thread(
                target=worker,
                name="CineCalendar-Quality-V36",
                daemon=True,
            )
            self._thread.start()
            return True
