from __future__ import annotations

from datetime import datetime, timezone
import math

from .production_engine import RANKING_STACK_VERSION
from .util import utcnow_iso


POLICY_VERSION = "recalibration-policy-v4.7.0"
POLICY_SETTING = "recommendation_recalibration_v47"


class RecalibrationPolicyV47:
    """Run expensive ranking backtests only after a meaningful rating change.

    Short-lived feedback (for example ``not_now`` or ``too_long``) still influences the live
    recommendation session, but no longer invalidates a multi-gigabyte historical backtest.  The
    last proven engine remains active until enough new/edited ratings can materially change the
    evaluation sample.
    """

    MIN_NEW_RATINGS = 12
    MAX_NEW_RATINGS = 40
    FRACTION_OF_LIBRARY = 0.01

    def __init__(self, db):
        self.db = db

    @classmethod
    def required_changes(cls, rating_count: int) -> int:
        proportional = int(math.ceil(max(0, int(rating_count)) * cls.FRACTION_OF_LIBRARY))
        return max(cls.MIN_NEW_RATINGS, min(cls.MAX_NEW_RATINGS, proportional))

    @staticmethod
    def _normalise_stamp(value: str | None) -> str:
        if not value:
            return ""
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            return str(value)

    def changed_ratings_since(self, completed_at: str | None, previous_count: int) -> int:
        stamp = self._normalise_stamp(completed_at)
        with self.db.connect() as con:
            current_count = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] or 0)
            if stamp:
                updated = int(
                    con.execute(
                        """SELECT COUNT(*) FROM ratings
                           WHERE datetime(COALESCE(updated_at,imported_at,''))>datetime(?)""",
                        (stamp,),
                    ).fetchone()[0]
                    or 0
                )
            else:
                updated = current_count
        return max(abs(current_count - max(0, int(previous_count or 0))), updated)

    def _store(self, payload: dict) -> dict:
        self.db.set_setting(POLICY_SETTING, payload)
        return payload

    def status(self, report: dict, current_token: str, rating_count: int) -> dict:
        report = dict(report or {})
        count = max(0, int(rating_count or 0))
        required = self.required_changes(count)
        completed = str(report.get("status") or "") == "completed"
        exact = completed and str(report.get("state_token") or "") == str(current_token)
        previous_count = int(report.get("rating_count", 0) or 0)
        changed = self.changed_ratings_since(report.get("completed_at"), previous_count) if completed else count
        ranking_changed = bool(
            completed
            and RANKING_STACK_VERSION not in str(report.get("state_token") or "")
        )
        if exact:
            state = "up_to_date"
            reason = "verdictul validat corespunde exact datelor curente"
            changed = 0
        elif not completed:
            state = "first_calibration"
            reason = "nu există încă un verdict complet"
        elif ranking_changed:
            state = "ranking_changed"
            reason = "motorul de clasare s-a schimbat"
        elif changed >= required:
            state = "ready"
            reason = "există suficiente ratinguri noi sau modificate"
        else:
            state = "deferred"
            reason = "schimbările sunt prea puține pentru a justifica un nou backtest"
        return {
            "version": POLICY_VERSION,
            "state": state,
            "reason": reason,
            "changed_ratings": int(changed),
            "required_ratings": int(required),
            "rating_count": count,
            "feedback_invalidates_backtest": False,
            "updated_at": utcnow_iso(),
        }

    def should_run(self, report: dict, current_token: str, rating_count: int) -> tuple[bool, dict]:
        payload = self.status(report, current_token, rating_count)
        should = payload["state"] in {"first_calibration", "ranking_changed", "ready"}
        payload["will_run"] = bool(should)
        return should, self._store(payload)
