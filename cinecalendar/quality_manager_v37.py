from __future__ import annotations

import threading
import time

from .accuracy_engine_v37 import allowed_local_shares, calibrated_accuracy_engine_class
from .quality_manager_v36 import RecommendationQualityManagerV36
from .recommender_v16 import FastRecommendationEngineV16
from .recommender_v17 import FastRecommendationEngineV17
from .rolling_backtest_v37 import compare_on_windows, rolling_windows, run_window_backtest
from .util import utcnow_iso


QUALITY_MANAGER_VERSION = "quality-manager-v3.7.0-personal-calibration"
QUALITY_SETTING = "recommendation_quality_v37"


class RecommendationQualityManagerV37:
    """Choose a local-retrieval share only after non-overlapping temporal wins for this user.

    A changed production stack invalidates the old measurement through the nested V3.4 state token,
    but the last completed 3.7 engine remains the runtime fallback while recalibration is running.
    This avoids a temporary downgrade merely because the evaluator itself was corrected.
    """

    MIN_RATINGS = 170

    def __init__(self, db):
        self.db = db
        self.legacy_manager = RecommendationQualityManagerV36(db)
        self.v34_manager = self.legacy_manager.baseline_manager
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def rating_count(self) -> int:
        return self.v34_manager.rating_count()

    def canonical_baseline_class(self):
        return self.v34_manager.preferred_engine_class()

    def state_token(self) -> str:
        return "|".join(
            [
                QUALITY_MANAGER_VERSION,
                self.v34_manager.state_token(),
                self.canonical_baseline_class().__name__,
            ]
        )

    def cached_report(self) -> dict:
        payload = self.db.get_setting(QUALITY_SETTING, {})
        return dict(payload) if isinstance(payload, dict) else {}

    def _store(self, payload: dict) -> None:
        self.db.set_setting(QUALITY_SETTING, payload)

    @staticmethod
    def _baseline_class_from_name(name: str):
        text = str(name or "")
        return FastRecommendationEngineV17 if "V17" in text else FastRecommendationEngineV16

    @classmethod
    def _engine_from_snapshot(cls, snapshot: dict):
        if not isinstance(snapshot, dict) or not snapshot:
            return None
        baseline_cls = cls._baseline_class_from_name(str(snapshot.get("baseline_engine") or ""))
        if bool(snapshot.get("approved")) and snapshot.get("selected_share") is not None:
            try:
                share = float(snapshot.get("selected_share"))
            except (TypeError, ValueError):
                share = .14
            return calibrated_accuracy_engine_class(baseline_cls, share)
        return baseline_cls

    @classmethod
    def _fallback_snapshot(cls, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {}
        stored = payload.get("fallback_snapshot")
        if isinstance(stored, dict) and stored:
            return dict(stored)
        if (
            str(payload.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(payload.get("status") or "") == "completed"
        ):
            verdict = payload.get("selected_verdict") or {}
            return {
                "baseline_engine": str(payload.get("baseline_engine") or "FastRecommendationEngineV16"),
                "approved": bool(verdict.get("approved")),
                "selected_share": payload.get("selected_share"),
            }
        return {}

    def preferred_engine_class(self):
        payload = self.cached_report()
        token = self.state_token()
        if (
            str(payload.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(payload.get("state_token") or "") == token
            and str(payload.get("status") or "") == "completed"
        ):
            verdict = payload.get("selected_verdict") or {}
            if bool(verdict.get("approved")):
                share = float(payload.get("selected_share", 0.14) or 0.14)
                return calibrated_accuracy_engine_class(self.canonical_baseline_class(), share)
            return self.canonical_baseline_class()

        # A stack-version change intentionally invalidates the measurement, not the last validated
        # runtime choice. Preserve that exact old choice while the new backtest runs in background.
        fallback = self._engine_from_snapshot(self._fallback_snapshot(payload))
        if fallback is not None:
            return fallback
        return self.legacy_manager.preferred_engine_class()

    def status(self) -> dict:
        payload = self.cached_report()
        fallback = self._fallback_snapshot(payload)
        payload["current_state_token"] = self.state_token()
        payload["preferred_engine"] = self.preferred_engine_class().__name__
        payload["baseline_engine"] = self.canonical_baseline_class().__name__
        payload["legacy_36_engine"] = self.legacy_manager.preferred_engine_class().__name__
        payload["background_running"] = bool(self._thread and self._thread.is_alive())
        payload["fallback_snapshot"] = fallback
        payload["using_previous_validated_engine"] = bool(
            fallback and str(payload.get("state_token") or "") != self.state_token()
        )
        payload["legacy_v36"] = self.legacy_manager.status()
        return payload

    def _v34_ready(self) -> bool:
        token = self.v34_manager.state_token()
        report = self.v34_manager.cached_report()
        return bool(
            str(report.get("manager_version") or "") == "quality-manager-v3.4.0"
            and str(report.get("state_token") or "") == token
            and str(report.get("status") or "") == "completed"
        )

    def _wait_for_v34(self, timeout: float = 300.0) -> bool:
        if self._v34_ready():
            return True
        self.v34_manager.start_background(delay_seconds=0.0)
        deadline = time.monotonic() + max(10.0, float(timeout))
        while time.monotonic() < deadline:
            if self._v34_ready():
                return True
            report = self.v34_manager.cached_report()
            if str(report.get("status") or "") in {"error", "interrupted_cooldown"}:
                return False
            time.sleep(1.0)
        return False

    def start_background(
        self,
        *,
        delay_seconds: float = 80.0,
        candidate_limit: int = 2200,
        final_limit: int = 100,
        als_timeout: float = 180.0,
    ) -> bool:
        count = self.rating_count()
        current = self.cached_report()
        fallback_snapshot = self._fallback_snapshot(current)
        fallback_engine = self._engine_from_snapshot(fallback_snapshot)
        fallback_name = (
            fallback_engine.__name__
            if fallback_engine is not None
            else self.legacy_manager.preferred_engine_class().__name__
        )

        if count < self.MIN_RATINGS:
            self._store(
                {
                    "manager_version": QUALITY_MANAGER_VERSION,
                    "state_token": self.state_token(),
                    "status": "insufficient_ratings",
                    "rating_count": count,
                    "minimum_ratings": self.MIN_RATINGS,
                    "preferred_engine": fallback_name,
                    "fallback_snapshot": fallback_snapshot,
                    "updated_at": utcnow_iso(),
                }
            )
            return False

        if (
            str(current.get("manager_version") or "") == QUALITY_MANAGER_VERSION
            and str(current.get("state_token") or "") == self.state_token()
            and str(current.get("status") or "") == "completed"
        ):
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            def worker() -> None:
                if delay_seconds > 0:
                    time.sleep(float(delay_seconds))
                if not self._wait_for_v34():
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": self.state_token(),
                            "status": "waiting_for_v34",
                            "rating_count": self.rating_count(),
                            "preferred_engine": fallback_name,
                            "fallback_snapshot": fallback_snapshot,
                            "updated_at": utcnow_iso(),
                        }
                    )
                    return

                baseline_cls = self.canonical_baseline_class()
                token = self.state_token()
                rating_token = self.v34_manager.state_token()
                windows = rolling_windows(self.db)
                if len(windows) < 2:
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": token,
                            "status": "insufficient_temporal_windows",
                            "rating_count": self.rating_count(),
                            "window_count": len(windows),
                            "preferred_engine": fallback_name,
                            "fallback_snapshot": fallback_snapshot,
                            "updated_at": utcnow_iso(),
                        }
                    )
                    return

                self._store(
                    {
                        "manager_version": QUALITY_MANAGER_VERSION,
                        "state_token": token,
                        "status": "running",
                        "rating_count": self.rating_count(),
                        "baseline_engine": baseline_cls.__name__,
                        "shares": list(allowed_local_shares()),
                        "window_count": len(windows),
                        "preferred_engine": fallback_name,
                        "fallback_snapshot": fallback_snapshot,
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

                    candidate_results: list[dict] = []
                    selected: dict | None = None
                    for share in allowed_local_shares():
                        challenger_cls = calibrated_accuracy_engine_class(baseline_cls, share)
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
                            "share": float(share),
                            "engine": challenger_cls.__name__,
                            "approved": bool(aggregate.get("approved")),
                            "selection_score": float(aggregate.get("selection_score", -999.0) or -999.0),
                            "mean_composite_delta": float(aggregate.get("mean_composite_delta", 0.0) or 0.0),
                            "worst_composite_delta": float(aggregate.get("worst_composite_delta", 0.0) or 0.0),
                            "positive_folds": int(aggregate.get("positive_folds", 0) or 0),
                            "comparison": comparison,
                        }
                        candidate_results.append(item)
                        if item["approved"] and (
                            selected is None or item["selection_score"] > selected["selection_score"]
                        ):
                            selected = item

                    if self.v34_manager.state_token() != rating_token or self.state_token() != token:
                        return

                    summaries = [
                        {k: value for k, value in item.items() if k != "comparison"}
                        for item in candidate_results
                    ]
                    if selected is None:
                        preferred = baseline_cls.__name__
                        selected_share = None
                        selected_verdict = {
                            "approved": False,
                            "reason": "no_share_passed_all_rolling_guardrails",
                        }
                        selected_comparison = None
                    else:
                        preferred = str(selected["engine"])
                        selected_share = float(selected["share"])
                        selected_comparison = selected["comparison"]
                        selected_verdict = dict(selected_comparison.get("aggregate") or {})

                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": token,
                            "status": "completed",
                            "rating_count": self.rating_count(),
                            "baseline_engine": baseline_cls.__name__,
                            "legacy_36_engine": self.legacy_manager.preferred_engine_class().__name__,
                            "preferred_engine": preferred,
                            "window_count": len(windows),
                            "window_cutoffs": [window.cutoff_date for window in windows],
                            "candidate_summaries": summaries,
                            "selected_share": selected_share,
                            "selected_verdict": selected_verdict,
                            "selected_comparison": selected_comparison,
                            "completed_at": utcnow_iso(),
                        }
                    )
                except Exception as exc:
                    self._store(
                        {
                            "manager_version": QUALITY_MANAGER_VERSION,
                            "state_token": token,
                            "status": "error",
                            "rating_count": self.rating_count(),
                            "baseline_engine": baseline_cls.__name__,
                            "preferred_engine": fallback_name,
                            "fallback_snapshot": fallback_snapshot,
                            "error": str(exc),
                            "completed_at": utcnow_iso(),
                        }
                    )

            self._thread = threading.Thread(
                target=worker,
                name="CineCalendar-Quality-V37",
                daemon=True,
            )
            self._thread.start()
            return True
