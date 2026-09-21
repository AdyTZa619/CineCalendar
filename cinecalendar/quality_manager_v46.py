from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
import time

from .hybrid_calibration_v46 import allowed_als_weights, calibrated_hybrid_engine_class
from .quality_manager_v37 import RecommendationQualityManagerV37
from .rolling_backtest_v37 import compare_on_windows, rolling_windows, run_window_backtest
from .util import utcnow_iso


QUALITY_MANAGER_VERSION = "quality-manager-v4.6.0-personal-hybrid"
QUALITY_SETTING = "recommendation_quality_v46"
INTERRUPTED_RETRY_COOLDOWN_SECONDS = 6 * 60 * 60
ERROR_RETRY_COOLDOWN_SECONDS = 60 * 60


class RecommendationQualityManagerV46:
    """Select the ALS/content balance only when this user's hidden history proves it better.

    The established 70/30 blend remains the baseline. Challengers are evaluated on the same
    non-overlapping temporal windows and strict guardrails already used by the production quality
    system. A winner is activated only on a later launch, never halfway through a session.
    """

    MIN_RATINGS = 170

    def __init__(self, db):
        self.db = db
        self.base_manager = RecommendationQualityManagerV37(db)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def rating_count(self) -> int:
        return self.base_manager.rating_count()

    def baseline_engine_class(self):
        return self.base_manager.preferred_engine_class()

    def state_token(self) -> str:
        return "|".join(
            (
                QUALITY_MANAGER_VERSION,
                self.base_manager.state_token(),
                self.baseline_engine_class().__name__,
            )
        )

    def cached_report(self) -> dict:
        payload = self.db.get_setting(QUALITY_SETTING, {})
        return dict(payload) if isinstance(payload, dict) else {}

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
            return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
        except (TypeError, ValueError):
            return None

    def _retry_cooldown_remaining(self, payload: dict, token: str) -> float:
        if (
            str(payload.get("manager_version") or "") != QUALITY_MANAGER_VERSION
            or str(payload.get("state_token") or "") != token
        ):
            return 0.0
        status = str(payload.get("status") or "")
        age = self._age_seconds(
            payload.get("started_at") if status in {"running", "interrupted_cooldown"}
            else payload.get("completed_at")
        )
        if age is None:
            return 0.0
        if status in {"running", "interrupted_cooldown"}:
            return max(0.0, INTERRUPTED_RETRY_COOLDOWN_SECONDS - age)
        if status == "error":
            return max(0.0, ERROR_RETRY_COOLDOWN_SECONDS - age)
        return 0.0

    def _selected_weight(self, payload: dict, *, require_current: bool) -> float | None:
        if not isinstance(payload, dict):
            return None
        if require_current and str(payload.get("state_token") or "") != self.state_token():
            return None
        if (
            str(payload.get("manager_version") or "") != QUALITY_MANAGER_VERSION
            or str(payload.get("status") or "") != "completed"
            or not bool((payload.get("selected_verdict") or {}).get("approved"))
        ):
            return None
        try:
            weight = round(float(payload.get("selected_als_weight")), 2)
        except (TypeError, ValueError):
            return None
        return weight if weight in allowed_als_weights() else None

    def _fallback_weight(self, payload: dict) -> float | None:
        try:
            explicit = round(float(payload.get("fallback_als_weight")), 2)
        except (AttributeError, TypeError, ValueError):
            explicit = None
        if explicit in allowed_als_weights():
            return explicit
        return self._selected_weight(payload, require_current=False)

    def preferred_engine_class(self):
        baseline = self.baseline_engine_class()
        payload = self.cached_report()
        # A fresh verdict is ideal. During recalibration, retain the last proven personal balance
        # on top of the current safe baseline instead of silently falling back to a global default.
        weight = self._selected_weight(payload, require_current=True)
        if weight is None:
            weight = self._fallback_weight(payload)
        return calibrated_hybrid_engine_class(baseline, weight) if weight is not None else baseline

    def status(self) -> dict:
        payload = self.cached_report()
        payload["current_state_token"] = self.state_token()
        payload["preferred_engine"] = self.preferred_engine_class().__name__
        payload["baseline_engine"] = self.baseline_engine_class().__name__
        payload["background_running"] = bool(self._thread and self._thread.is_alive())
        payload["base_v37"] = self.base_manager.status()
        return payload

    def _v37_ready(self) -> bool:
        report = self.base_manager.cached_report()
        return bool(
            str(report.get("manager_version") or "") == "quality-manager-v3.7.0-personal-calibration"
            and str(report.get("state_token") or "") == self.base_manager.state_token()
            and str(report.get("status") or "") == "completed"
        )

    def _wait_for_v37(self, timeout: float = 1800.0) -> bool:
        if self._v37_ready():
            return True
        self.base_manager.start_background(delay_seconds=0.0)
        deadline = time.monotonic() + max(10.0, float(timeout))
        while time.monotonic() < deadline:
            if self._v37_ready():
                return True
            report = self.base_manager.cached_report()
            if str(report.get("status") or "") == "error":
                return False
            time.sleep(1.0)
        return False

    def start_background(
        self,
        *,
        delay_seconds: float = 110.0,
        candidate_limit: int = 2200,
        final_limit: int = 100,
        als_timeout: float = 180.0,
    ) -> bool:
        count = self.rating_count()
        token = self.state_token()
        current = self.cached_report()
        fallback_weight = self._fallback_weight(current)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

        cooldown = self._retry_cooldown_remaining(current, token)
        if cooldown > 0:
            if str(current.get("status") or "") == "running":
                interrupted = dict(current)
                interrupted.update(
                    status="interrupted_cooldown",
                    interrupted_at=utcnow_iso(),
                    retry_after_seconds=int(cooldown),
                )
                self._store(interrupted)
            return False

        if count < self.MIN_RATINGS:
            self._store(
                {
                    "manager_version": QUALITY_MANAGER_VERSION,
                    "state_token": token,
                    "status": "insufficient_ratings",
                    "rating_count": count,
                    "minimum_ratings": self.MIN_RATINGS,
                    "preferred_engine": self.preferred_engine_class().__name__,
                    "fallback_als_weight": fallback_weight,
                    "updated_at": utcnow_iso(),
                }
            )
            return False
        if (
            str(current.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(current.get("state_token") or "") == token
            and str(current.get("status") or "") == "completed"
        ):
            return False

        with self._lock:
            def worker() -> None:
                if delay_seconds > 0:
                    time.sleep(float(delay_seconds))
                if not self._wait_for_v37():
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": self.state_token(),
                            "status": "waiting_for_v37",
                            "rating_count": self.rating_count(),
                            "preferred_engine": self.preferred_engine_class().__name__,
                            "fallback_als_weight": fallback_weight,
                            "updated_at": utcnow_iso(),
                        }
                    )
                    return

                baseline_cls = self.baseline_engine_class()
                active_token = self.state_token()
                windows = rolling_windows(self.db)
                if len(windows) < 2:
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": active_token,
                            "status": "insufficient_temporal_windows",
                            "rating_count": self.rating_count(),
                            "window_count": len(windows),
                            "preferred_engine": baseline_cls.__name__,
                            "fallback_als_weight": fallback_weight,
                            "updated_at": utcnow_iso(),
                        }
                    )
                    return

                self._store(
                    {
                        "manager_version": QUALITY_MANAGER_VERSION,
                        "state_token": active_token,
                        "status": "running",
                        "rating_count": self.rating_count(),
                        "worker_pid": int(os.getpid()),
                        "baseline_engine": baseline_cls.__name__,
                        "als_weights": list(allowed_als_weights()),
                        "window_count": len(windows),
                        "fallback_als_weight": fallback_weight,
                        "started_at": utcnow_iso(),
                    }
                )
                try:
                    baseline_reports = [
                        run_window_backtest(
                            self.db.path,
                            window,
                            engine_cls=baseline_cls,
                            candidate_limit=candidate_limit,
                            final_limit=final_limit,
                            als_timeout=als_timeout,
                        )
                        for window in windows
                    ]
                    candidates = []
                    selected = None
                    for weight in allowed_als_weights():
                        challenger_cls = calibrated_hybrid_engine_class(baseline_cls, weight)
                        comparison = compare_on_windows(
                            self.db.path,
                            windows,
                            baseline_cls=baseline_cls,
                            challenger_cls=challenger_cls,
                            candidate_limit=candidate_limit,
                            final_limit=final_limit,
                            als_timeout=als_timeout,
                            baseline_reports=baseline_reports,
                        )
                        aggregate = dict(comparison.get("aggregate") or {})
                        item = {
                            "als_weight": weight,
                            "content_weight": round(1.0 - weight, 2),
                            "engine": challenger_cls.__name__,
                            "approved": bool(aggregate.get("approved")),
                            "selection_score": float(aggregate.get("selection_score", -999.0) or -999.0),
                            "comparison": comparison,
                        }
                        candidates.append(item)
                        if item["approved"] and (
                            selected is None or item["selection_score"] > selected["selection_score"]
                        ):
                            selected = item

                    if self.state_token() != active_token:
                        return
                    summaries = [{k: v for k, v in item.items() if k != "comparison"} for item in candidates]
                    if selected is None:
                        weight = None
                        verdict = {"approved": False, "reason": "no_weight_passed_all_rolling_guardrails"}
                        preferred = baseline_cls.__name__
                        comparison = None
                    else:
                        weight = float(selected["als_weight"])
                        comparison = selected["comparison"]
                        verdict = dict(comparison.get("aggregate") or {})
                        preferred = str(selected["engine"])
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": active_token,
                            "status": "completed",
                            "rating_count": self.rating_count(),
                            "baseline_engine": baseline_cls.__name__,
                            "preferred_engine": preferred,
                            "window_count": len(windows),
                            "candidate_summaries": summaries,
                            "selected_als_weight": weight,
                            "selected_verdict": verdict,
                            "selected_comparison": comparison,
                            "completed_at": utcnow_iso(),
                        }
                    )
                except Exception as exc:
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": active_token,
                            "status": "error",
                            "rating_count": self.rating_count(),
                            "baseline_engine": baseline_cls.__name__,
                            "preferred_engine": baseline_cls.__name__,
                            "fallback_als_weight": fallback_weight,
                            "error": str(exc),
                            "completed_at": utcnow_iso(),
                        }
                    )

            self._thread = threading.Thread(
                target=worker,
                name="CineCalendar-Quality-V46",
                daemon=True,
            )
            self._thread.start()
            return True
