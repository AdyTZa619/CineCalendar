from __future__ import annotations

from .util import utcnow_iso
from .v5_rating_snapshot import report_rating_freshness


MODE_STABLE = "stable"
MODE_DISCOVERY = "discovery"
MODE_ADAPTIVE = "adaptive"
_ALLOWED = {MODE_STABLE, MODE_DISCOVERY, MODE_ADAPTIVE}


class EngineModeRouter:
    """One executable, one database, three guarded recommendation modes.

    Stabil is always available. Descoperire and Adaptiv are created lazily and can be selected
    only when the latest evaluation report is current for the present rating history.
    """

    def __init__(self, db, stable_engine, discovery_factory, adaptive_factory):
        self.db = db
        self.stable = stable_engine
        self._discovery_factory = discovery_factory
        self._adaptive_factory = adaptive_factory
        self._discovery = None
        self._adaptive = None

        saved = str(self.db.get_setting("recommendation_engine_mode", MODE_STABLE) or MODE_STABLE)
        if saved not in _ALLOWED:
            saved = MODE_STABLE
        self._mode = saved
        self._enforce_guard()

    def _report(self) -> dict:
        report = self.db.get_setting("v5_evaluation_report", {}) or {}
        return report if isinstance(report, dict) else {}

    def _decision(self) -> dict:
        report = self._report()
        decision = report.get("decision") or {}
        return decision if isinstance(decision, dict) else {}

    def _report_current(self) -> bool:
        return report_rating_freshness(self.db, self._report()) is True

    def discovery_eligible(self) -> bool:
        decision = self._decision()
        return bool(
            self._report_current()
            and decision.get("discovery_candidate_for_stable")
            and decision.get("discovery_rolling_approved")
            and decision.get("discovery_event_guard_passed")
            and decision.get("discovery_visible_decision_guard_passed")
        )

    def adaptive_eligible(self) -> bool:
        decision = self._decision()
        return bool(
            self._report_current()
            and decision.get("eligible_for_visible_alpha_trial")
            and decision.get("visible_decision_guard_passed")
            and str(decision.get("selected_variant") or "") == "20%"
        )

    def selected_discovery_variant(self) -> str:
        value = str(self._decision().get("selected_discovery_variant") or "balanced").lower()
        return value if value in {"strict", "balanced", "wide"} else "balanced"

    def _enforce_guard(self) -> None:
        if self._mode == MODE_DISCOVERY and not self.discovery_eligible():
            self._mode = MODE_STABLE
        elif self._mode == MODE_ADAPTIVE and not self.adaptive_eligible():
            self._mode = MODE_STABLE
        self.db.set_setting("recommendation_engine_mode", self._mode)

    def _start_engine(self, engine):
        collaborative = getattr(engine, "collaborative", None)
        start = getattr(collaborative, "start_background", None)
        if callable(start):
            start()
        return engine

    @property
    def discovery(self):
        if self._discovery is None:
            self._discovery = self._start_engine(self._discovery_factory())
        return self._discovery

    @property
    def adaptive(self):
        if self._adaptive is None:
            self._adaptive = self._start_engine(self._adaptive_factory())
        return self._adaptive

    @property
    def mode(self) -> str:
        self._enforce_guard()
        return self._mode

    @property
    def active(self):
        mode = self.mode
        if mode == MODE_DISCOVERY:
            return self.discovery
        if mode == MODE_ADAPTIVE:
            return self.adaptive
        return self.stable

    def set_mode(self, mode: str) -> dict:
        wanted = str(mode or "").strip().lower()
        if wanted not in _ALLOWED:
            raise ValueError("Motor necunoscut.")
        if wanted == MODE_DISCOVERY and not self.discovery_eligible():
            raise RuntimeError("Descoperire nu are încă un raport valid pentru ratingurile actuale.")
        if wanted == MODE_ADAPTIVE and not self.adaptive_eligible():
            raise RuntimeError("Adaptiv nu are încă un raport valid pentru ratingurile actuale.")
        self._mode = wanted
        self.db.set_setting("recommendation_engine_mode", wanted)
        self.db.set_setting("recommendation_engine_mode_changed_at", utcnow_iso())
        # Force a fresh visible round when switching engines.
        for engine in (self.stable, self._discovery, self._adaptive):
            invalidate = getattr(engine, "invalidate_round", None) if engine is not None else None
            if callable(invalidate):
                invalidate()
        return self.status()

    def status(self) -> dict:
        report = self._report()
        decision = self._decision()
        return {
            "available": True,
            "mode": self.mode,
            "report_current": report_rating_freshness(self.db, report),
            "report_generated_at": str(report.get("generated_at") or ""),
            "discovery_eligible": self.discovery_eligible(),
            "adaptive_eligible": self.adaptive_eligible(),
            "selected_discovery_variant": self.selected_discovery_variant(),
            "selected_adaptive_variant": str(decision.get("selected_variant") or ""),
            "loaded": {
                MODE_STABLE: True,
                MODE_DISCOVERY: self._discovery is not None,
                MODE_ADAPTIVE: self._adaptive is not None,
            },
        }

    def __getattr__(self, name):
        return getattr(self.active, name)
