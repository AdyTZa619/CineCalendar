from __future__ import annotations

from .util import utcnow_iso
from .recommender_v16 import recommendation_engine_identity
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

    def __init__(
        self, db, stable_engine, discovery_factory, adaptive_factory,
        *, stable_identity: str | None = None,
    ):
        self.db = db
        self.stable = stable_engine
        self.stable_identity = str(
            stable_identity or recommendation_engine_identity(stable_engine)
        )
        self._discovery_factory = discovery_factory
        self._adaptive_factory = adaptive_factory
        self._discovery = None
        self._adaptive = None
        self._freshness_token = None
        self._freshness_value = None
        self._loaded_report_token = None

        saved_raw = self.db.get_setting("recommendation_engine_mode", None)
        saved = str(saved_raw or MODE_STABLE)
        if saved not in _ALLOWED:
            saved = MODE_STABLE
        self._mode = saved
        self._enforce_guard()
        if saved_raw is None or str(saved_raw) not in _ALLOWED:
            self.db.set_setting("recommendation_engine_mode", self._mode)

    def _report(self) -> dict:
        report = self.db.get_setting("v5_evaluation_report", {}) or {}
        return report if isinstance(report, dict) else {}

    def _decision(self) -> dict:
        report = self._report()
        decision = report.get("decision") or {}
        return decision if isinstance(decision, dict) else {}

    def _rating_state_token(self) -> tuple:
        try:
            with self.db.connect() as con:
                row = con.execute(
                    "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings"
                ).fetchone()
            return int(row[0]), str(row[1] or "")
        except Exception:
            return ()

    def _report_current(self) -> bool:
        report = self._report()
        snapshot = report.get("rating_snapshot") if isinstance(report, dict) else {}
        report_token = (
            str(report.get("generated_at") or "") if isinstance(report, dict) else "",
            str((snapshot or {}).get("sha256") or "") if isinstance(snapshot, dict) else "",
        )
        token = (self._rating_state_token(), report_token)
        if token != self._freshness_token:
            self._freshness_token = token
            self._freshness_value = report_rating_freshness(self.db, report) is True
        return bool(self._freshness_value)

    def _report_matches_stable(self) -> bool:
        report = self._report()
        stable = report.get("stable_engine") if isinstance(report, dict) else {}
        return bool(
            isinstance(stable, dict)
            and str(stable.get("identity") or "") == self.stable_identity
        )

    def discovery_eligible(self) -> bool:
        decision = self._decision()
        return bool(
            self._report_current()
            and self._report_matches_stable()
            and decision.get("discovery_candidate_for_stable")
            and decision.get("discovery_rolling_approved")
            and decision.get("discovery_event_guard_passed")
            and decision.get("discovery_visible_decision_guard_passed")
        )

    def adaptive_eligible(self) -> bool:
        decision = self._decision()
        return bool(
            self._report_current()
            and self._report_matches_stable()
            and decision.get("eligible_for_visible_alpha_trial")
            and decision.get("visible_decision_guard_passed")
            and str(decision.get("selected_variant") or "") in {"learned", "10%", "15%", "20%"}
        )

    def selected_discovery_variant(self) -> str:
        value = str(self._decision().get("selected_discovery_variant") or "balanced").lower()
        return value if value in {"strict", "balanced", "wide"} else "balanced"

    def selected_adaptive_variant(self) -> str:
        value = str(self._decision().get("selected_variant") or "learned")
        return value if value in {"learned", "10%", "15%", "20%"} else "learned"

    def recommended_mode(self) -> str:
        if not self._report_current() or not self._report_matches_stable():
            return MODE_STABLE
        wanted = str(self._decision().get("recommended_mode") or MODE_STABLE)
        if wanted == MODE_ADAPTIVE and self.adaptive_eligible():
            return MODE_ADAPTIVE
        if wanted == MODE_DISCOVERY and self.discovery_eligible():
            return MODE_DISCOVERY
        return MODE_STABLE

    def _report_configuration_token(self) -> tuple:
        report = self._report()
        decision = self._decision()
        return (
            str(report.get("generated_at") or ""),
            self.selected_discovery_variant(),
            self.selected_adaptive_variant(),
        )

    def _refresh_lazy_engines_if_report_changed(self) -> None:
        token = self._report_configuration_token()
        if self._loaded_report_token is None:
            self._loaded_report_token = token
            return
        if token != self._loaded_report_token:
            self._discovery = None
            self._adaptive = None
            self._loaded_report_token = token

    def _enforce_guard(self) -> None:
        self._refresh_lazy_engines_if_report_changed()
        original = self._mode
        if self._mode == MODE_DISCOVERY and not self.discovery_eligible():
            self._mode = MODE_STABLE
        elif self._mode == MODE_ADAPTIVE and not self.adaptive_eligible():
            self._mode = MODE_STABLE
        if self._mode != original:
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
        self._freshness_token = None
        self._freshness_value = None
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
            "report_current": self._report_current(),
            "report_matches_stable": self._report_matches_stable(),
            "stable_identity": self.stable_identity,
            "report_generated_at": str(report.get("generated_at") or ""),
            "discovery_eligible": self.discovery_eligible(),
            "adaptive_eligible": self.adaptive_eligible(),
            "selected_discovery_variant": self.selected_discovery_variant(),
            "selected_adaptive_variant": self.selected_adaptive_variant(),
            "recommended_mode": self.recommended_mode(),
            "loaded": {
                MODE_STABLE: True,
                MODE_DISCOVERY: self._discovery is not None,
                MODE_ADAPTIVE: self._adaptive is not None,
            },
        }

    def __getattr__(self, name):
        return getattr(self.active, name)
