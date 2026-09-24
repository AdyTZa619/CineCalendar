from __future__ import annotations

"""Honest, outcome-backed reliability verdicts for recommendation cards.

This module never changes a score or the order of recommendations.  It separates the
model's personal-evidence coverage from measured predictive reliability and only calls
a recommendation verified after enough real IMDb outcomes exist.
"""

from dataclasses import dataclass
import math

from .candidate_metadata_v48 import metadata_snapshot
from .recommendation_outcomes_v42 import recommendation_performance, reconcile_recommendation_outcomes


RELIABILITY_GATE_VERSION = "reliability-gate-v4.12.0"
MIN_MEASURED_OUTCOMES = 30
MIN_WITHIN_ONE = 0.65
MAX_MAE = 1.0


@dataclass(frozen=True)
class ReliabilitySnapshot:
    rated_outcomes: int
    mae: float | None
    within_one: float | None
    residuals: tuple[float, ...]

    @property
    def measurement_ready(self) -> bool:
        return (
            self.rated_outcomes >= MIN_MEASURED_OUTCOMES
            and self.mae is not None
            and self.mae <= MAX_MAE
            and self.within_one is not None
            and self.within_one >= MIN_WITHIN_ONE
        )


@dataclass(frozen=True)
class ReliabilityVerdict:
    status: str
    label: str
    reason: str
    interval_low: float
    interval_high: float
    measured_outcomes: int
    empirical_interval: bool
    metadata_complete: bool


def _quantile(values: tuple[float, ...], probability: float) -> float:
    """Small dependency-free linear quantile used for empirical error bounds."""
    if not values:
        raise ValueError("quantile requires at least one value")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, probability))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class RecommendationReliabilityGate:
    """Build one measured snapshot, then evaluate any number of visible cards cheaply."""

    def __init__(self, db, engine_version: str = ""):
        self.db = db
        self.engine_version = str(engine_version or "")
        self._snapshot: ReliabilitySnapshot | None = None

    def refresh(self, *, reconcile: bool = True) -> ReliabilitySnapshot:
        if reconcile:
            reconcile_recommendation_outcomes(self.db)
        performance = recommendation_performance(
            self.db,
            recent_limit=1,
            engine_version=self.engine_version,
            reconcile=False,
        )
        with self.db.connect() as con:
            where = "predicted_rating IS NOT NULL AND actual_rating IS NOT NULL"
            params: tuple[object, ...] = ()
            if self.engine_version:
                where += " AND COALESCE(engine_version,'')=?"
                params = (self.engine_version,)
            rows = con.execute(
                f"SELECT predicted_rating,actual_rating FROM recommendation_outcomes WHERE {where}",
                params,
            ).fetchall()
        residuals = tuple(
            abs(float(row["predicted_rating"]) - float(row["actual_rating"]))
            for row in rows
        )
        self._snapshot = ReliabilitySnapshot(
            rated_outcomes=int(performance.rated_outcomes),
            mae=performance.mae,
            within_one=performance.within_one,
            residuals=residuals,
        )
        return self._snapshot

    @property
    def snapshot(self) -> ReliabilitySnapshot:
        return self._snapshot or self.refresh()

    def evaluate(self, recommendation) -> ReliabilityVerdict:
        score = recommendation.score
        predicted = min(10.0, max(1.0, float(score.predicted_rating or 0.0)))
        confidence = min(1.0, max(0.0, float(score.confidence or 0.0)))
        evidence = max(0.0, float(score.evidence or 0.0))
        audit = dict(getattr(score, "trust_audit", {}) or {})
        snapshot = self.snapshot

        if snapshot.rated_outcomes >= 8 and snapshot.residuals:
            probability = 0.80 if snapshot.rated_outcomes >= MIN_MEASURED_OUTCOMES else 0.90
            width = _quantile(snapshot.residuals, probability)
            # A tiny sample must not produce an implausibly narrow promise by accident.
            width = max(0.5 if snapshot.measurement_ready else 0.9, min(2.5, width))
            empirical = True
        else:
            # Conservative fallback. This is explicitly not presented as empirically verified.
            width = min(2.5, max(1.0, 1.0 + (1.0 - confidence) * 1.5))
            empirical = False

        low = max(1.0, predicted - width)
        high = min(10.0, predicted + width)
        fields = metadata_snapshot(recommendation.movie)
        complete = all(fields[name] for name in ("genres", "directors", "countries", "semantic_text", "runtime"))
        red_flag = bool(audit.get("red_flag")) or str(audit.get("status") or "") == "red_flag"

        if red_flag or (confidence >= 0.55 and predicted < 5.8):
            return ReliabilityVerdict(
                "reject",
                "NU RECOMAND ACUM",
                str(audit.get("red_reason") or "Semnalele de siguranță nu susțin această alegere."),
                low,
                high,
                snapshot.rated_outcomes,
                empirical,
                complete,
            )

        verified = (
            snapshot.measurement_ready
            and confidence >= 0.70
            and evidence >= 1.0
            and complete
            and low >= 6.5
            and str(audit.get("status") or "trusted") not in {"backfill", "red_flag"}
        )
        if verified:
            return ReliabilityVerdict(
                "verified",
                "RECOMANDARE VERIFICATĂ",
                f"Interval calculat din {snapshot.rated_outcomes} rezultate reale; motorul este în limitele de precizie acceptate.",
                low,
                high,
                snapshot.rated_outcomes,
                True,
                True,
            )

        if snapshot.measurement_ready and confidence >= 0.52 and evidence >= 0.6:
            missing = " Metadatele de clasare nu sunt încă complete." if not complete else ""
            return ReliabilityVerdict(
                "cautious",
                "ALEGERE PLAUZIBILĂ — CU REZERVĂ",
                "Motorul este măsurat, dar acest film nu trece toate pragurile pentru verdict ferm." + missing,
                low,
                high,
                snapshot.rated_outcomes,
                empirical,
                complete,
            )

        remaining = max(0, MIN_MEASURED_OUTCOMES - snapshot.rated_outcomes)
        reason = (
            f"Mai sunt necesare {remaining} rezultate cu rating pentru validarea preciziei. "
            "Poți alege filmul, dar programul nu îl prezintă încă drept alegere sigură."
            if remaining else
            "Rezultatele reale nu ating încă pragurile de precizie; alegerea rămâne exploratorie."
        )
        return ReliabilityVerdict(
            "insufficient",
            "ÎNCĂ NEVALIDAT PE SUFICIENTE REZULTATE",
            reason,
            low,
            high,
            snapshot.rated_outcomes,
            empirical,
            complete,
        )
