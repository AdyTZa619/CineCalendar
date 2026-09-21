from __future__ import annotations

import math
from statistics import mean, pvariance

from .recommendation_outcomes_v42 import reconcile_recommendation_outcomes
from .util import utcnow_iso


GUARD_VERSION = "recommendation-live-guard-v4.7.0"
GUARD_SETTING = "recommendation_live_guard_v47"
REFERENCE_LIMIT = 80
MIN_RATED = 20
MIN_CHOSEN = 25


def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 1.0)
    n = float(total)
    p = max(0.0, min(1.0, float(successes) / n))
    den = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / den
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / den
    return (max(0.0, center - margin), min(1.0, center + margin))


def _metrics(rows) -> dict:
    rated = [row for row in rows if row["actual_rating"] is not None]
    errors = [
        float(row["absolute_error"])
        for row in rated
        if row["absolute_error"] is not None
    ]
    chosen = [row for row in rows if row["chosen_at"]]
    liked = sum(int(row["actual_rating"]) >= 8 for row in rated)
    watched = sum(bool(row["watched_at"]) for row in chosen)
    return {
        "rated": len(rated),
        "liked": int(liked),
        "liked_rate": (liked / len(rated)) if rated else None,
        "chosen": len(chosen),
        "watched": int(watched),
        "watched_rate": (watched / len(chosen)) if chosen else None,
        "mae_count": len(errors),
        "mae": mean(errors) if errors else None,
        "mae_variance": pvariance(errors) if len(errors) >= 2 else 0.0,
    }


def compare_live_metrics(reference: dict, active: dict) -> dict:
    """Conservative regression verdict from real post-recommendation outcomes.

    No single noisy metric can roll the engine back.  At least two independently measured harms
    must agree, and rate regressions also require non-overlapping 95% Wilson intervals.
    """

    ref_rated = int(reference.get("rated", 0) or 0)
    act_rated = int(active.get("rated", 0) or 0)
    ref_chosen = int(reference.get("chosen", 0) or 0)
    act_chosen = int(active.get("chosen", 0) or 0)

    liked_bad = False
    liked_delta = None
    if min(ref_rated, act_rated) >= MIN_RATED:
        ref_rate = float(reference.get("liked_rate", 0.0) or 0.0)
        act_rate = float(active.get("liked_rate", 0.0) or 0.0)
        liked_delta = act_rate - ref_rate
        ref_low, _ref_high = _wilson(int(reference.get("liked", 0) or 0), ref_rated)
        _act_low, act_high = _wilson(int(active.get("liked", 0) or 0), act_rated)
        liked_bad = liked_delta <= -0.15 and act_high < ref_low

    watch_bad = False
    watched_delta = None
    if min(ref_chosen, act_chosen) >= MIN_CHOSEN:
        ref_rate = float(reference.get("watched_rate", 0.0) or 0.0)
        act_rate = float(active.get("watched_rate", 0.0) or 0.0)
        watched_delta = act_rate - ref_rate
        ref_low, _ref_high = _wilson(int(reference.get("watched", 0) or 0), ref_chosen)
        _act_low, act_high = _wilson(int(active.get("watched", 0) or 0), act_chosen)
        watch_bad = watched_delta <= -0.15 and act_high < ref_low

    mae_bad = False
    mae_delta = None
    if min(int(reference.get("mae_count", 0) or 0), int(active.get("mae_count", 0) or 0)) >= MIN_RATED:
        ref_mae = float(reference.get("mae", 0.0) or 0.0)
        act_mae = float(active.get("mae", 0.0) or 0.0)
        mae_delta = act_mae - ref_mae
        se = math.sqrt(
            float(reference.get("mae_variance", 0.0) or 0.0) / ref_rated
            + float(active.get("mae_variance", 0.0) or 0.0) / act_rated
        )
        mae_bad = mae_delta >= 0.35 and mae_delta > 1.96 * se

    harms = int(liked_bad) + int(watch_bad) + int(mae_bad)
    enough = act_rated >= MIN_RATED and ref_rated >= MIN_RATED
    rollback = bool(enough and harms >= 2)
    return {
        "approved": not rollback,
        "rollback": rollback,
        "enough_evidence": enough,
        "harm_count": harms,
        "liked_regression": liked_bad,
        "watched_regression": watch_bad,
        "mae_regression": mae_bad,
        "liked_rate_delta": liked_delta,
        "watched_rate_delta": watched_delta,
        "mae_delta": mae_delta,
        "minimum_rated_each": MIN_RATED,
        "minimum_chosen_each": MIN_CHOSEN,
    }


class RecommendationLiveGuardV47:
    def __init__(self, db):
        self.db = db

    def cached(self) -> dict:
        payload = self.db.get_setting(GUARD_SETTING, {})
        return dict(payload) if isinstance(payload, dict) else {}

    def _store(self, payload: dict) -> dict:
        self.db.set_setting(GUARD_SETTING, payload)
        return payload

    def _rows(
        self,
        *,
        engine_version: str | None = None,
        before: str | None = None,
        after: str | None = None,
    ):
        where = ["chosen_at IS NOT NULL"]
        params: list[object] = []
        if engine_version is not None:
            where.append("COALESCE(engine_version,'')=?")
            params.append(str(engine_version))
        if before:
            where.append("COALESCE(chosen_at,context_date,'')<?")
            params.append(str(before))
        if after:
            where.append("COALESCE(chosen_at,context_date,'')>=?")
            params.append(str(after))
        with self.db.connect() as con:
            return con.execute(
                f"""SELECT actual_rating,absolute_error,chosen_at,watched_at,engine_version
                    FROM recommendation_outcomes
                    WHERE {' AND '.join(where)}
                    ORDER BY COALESCE(rating_date,watched_at,playback_at,chosen_at,context_date) DESC,
                             exposure_history_id DESC
                    LIMIT ?""",
                tuple(params + [REFERENCE_LIMIT]),
            ).fetchall()

    @staticmethod
    def calibration_id(report: dict, weight: float | None) -> str:
        if weight is None:
            return ""
        return "|".join(
            (
                str(report.get("completed_at") or ""),
                str(report.get("state_token") or ""),
                f"als{int(round(float(weight) * 100)):02d}",
            )
        )

    def refresh(self, report: dict, weight: float | None, engine_version: str) -> dict:
        report = dict(report or {})
        verdict = report.get("selected_verdict") or {}
        if weight is None or not bool(verdict.get("approved")):
            existing = self.cached()
            if str(existing.get("status") or "") == "rolled_back":
                return existing
            return self._store(
                {
                    "version": GUARD_VERSION,
                    "status": "baseline",
                    "reason": "nu este activă o pondere personală nouă",
                    "updated_at": utcnow_iso(),
                }
            )

        reconcile_recommendation_outcomes(self.db)
        calibration_id = self.calibration_id(report, weight)
        current = self.cached()
        if (
            str(current.get("calibration_id") or "") != calibration_id
            or str(current.get("engine_version") or "") != str(engine_version)
        ):
            activated_at = utcnow_iso()
            reference = _metrics(self._rows(before=activated_at))
            current = {
                "version": GUARD_VERSION,
                "status": "collecting",
                "reason": "se strâng rezultate reale pentru validare",
                "calibration_id": calibration_id,
                "selected_als_weight": float(weight),
                "engine_version": str(engine_version),
                "activated_at": activated_at,
                "reference": reference,
            }
        if str(current.get("status") or "") == "rolled_back":
            return current

        active = _metrics(
            self._rows(
                engine_version=engine_version,
                after=str(current.get("activated_at") or "") or None,
            )
        )
        reference = dict(current.get("reference") or {})
        comparison = compare_live_metrics(reference, active)
        if comparison["rollback"]:
            status = "rolled_back"
            reason = "două semnale reale independente au regresat; s-a revenit la motorul sigur"
        elif comparison["enough_evidence"]:
            status = "protected"
            reason = "formula personală trece verificarea pe rezultate reale"
        else:
            status = "collecting"
            reason = "se strâng rezultate reale pentru validare"
        current.update(
            status=status,
            reason=reason,
            active=active,
            comparison=comparison,
            updated_at=utcnow_iso(),
        )
        if status == "rolled_back":
            current["rolled_back_at"] = utcnow_iso()
        return self._store(current)

    def blocks(self, report: dict, weight: float | None) -> bool:
        if weight is None:
            return False
        payload = self.cached()
        if str(payload.get("status") or "") != "rolled_back":
            return False
        current_id = self.calibration_id(report, weight)
        if current_id and str(report.get("status") or "") == "completed":
            return str(payload.get("calibration_id") or "") == current_id
        try:
            blocked_weight = round(float(payload.get("selected_als_weight")), 2)
        except (TypeError, ValueError):
            return False
        return blocked_weight == round(float(weight), 2)
