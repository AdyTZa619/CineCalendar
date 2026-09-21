from __future__ import annotations

from .hybrid_calibration_v46 import calibrated_hybrid_engine_class
from .quality_manager_v46 import RecommendationQualityManagerV46
from .recalibration_policy_v47 import RecalibrationPolicyV47
from .recommendation_guard_v47 import RecommendationLiveGuardV47
from .recommender_v16 import recommendation_engine_identity


QUALITY_MANAGER_VERSION = "quality-manager-v4.7.0-live-protected"


class RecommendationQualityManagerV47(RecommendationQualityManagerV46):
    """V4.6 personal hybrid selection plus cheap scheduling and live regression rollback."""

    def __init__(self, db):
        super().__init__(db)
        self.recalibration_policy = RecalibrationPolicyV47(db)
        self.live_guard = RecommendationLiveGuardV47(db)
        self._runtime_identity = ""

    def _candidate_weight(self) -> float | None:
        payload = self.cached_report()
        weight = self._selected_weight(payload, require_current=True)
        if weight is None:
            weight = self._fallback_weight(payload)
        return weight

    def refresh_live_guard(self) -> dict:
        report = self.cached_report()
        weight = self._candidate_weight()
        approved_complete = bool(
            str(report.get("status") or "") == "completed"
            and (report.get("selected_verdict") or {}).get("approved")
        )
        if not approved_complete:
            cached = self.live_guard.cached()
            if cached:
                return cached
        if weight is None:
            return self.live_guard.refresh(report, None, "")
        candidate = calibrated_hybrid_engine_class(self.baseline_engine_class(), weight)
        return self.live_guard.refresh(report, weight, recommendation_engine_identity(candidate))

    def preferred_engine_class(self):
        baseline = self.baseline_engine_class()
        payload = self.cached_report()
        weight = self._selected_weight(payload, require_current=True)
        if weight is None:
            weight = self._fallback_weight(payload)
        if weight is None or self.live_guard.blocks(payload, weight):
            return baseline
        return calibrated_hybrid_engine_class(baseline, weight)

    def set_runtime_engine(self, engine) -> None:
        self._runtime_identity = recommendation_engine_identity(engine)

    def start_background(self, **kwargs) -> bool:
        report = self.cached_report()
        should_run, _policy = self.recalibration_policy.should_run(
            report,
            self.state_token(),
            self.rating_count(),
        )
        if not should_run:
            return False
        return super().start_background(**kwargs)

    def status(self) -> dict:
        payload = super().status()
        payload["manager_version_v47"] = QUALITY_MANAGER_VERSION
        payload["recalibration_policy"] = self.recalibration_policy.status(
            self.cached_report(),
            self.state_token(),
            self.rating_count(),
        )
        payload["live_guard"] = self.live_guard.cached()
        payload["runtime_engine_identity"] = self._runtime_identity
        payload["live_rollback_active"] = str(
            (payload.get("live_guard") or {}).get("status") or ""
        ) == "rolled_back"
        return payload
