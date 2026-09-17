from __future__ import annotations

import threading

from .recommender_v16 import FastRecommendationEngineV16
from .recommender_v17 import FastRecommendationEngineV17
from .util import clamp


CONTEXT_RECOMMENDER_VERSION = "context-ranking-v3.8.0-final-period-slot"
_CONTEXT_CLASS_CACHE: dict[type, type] = {}
_CONTEXT_CLASS_LOCK = threading.RLock()


class _ContextGuardMixin:
    """Bound calendar/context influence without replacing long-term taste.

    The normal recommendation path remains rating-first. For the visible Top 3, one secondary slot
    may become period-aware only when that title is already present in the exact Top 3 selected by
    the validated engine. Context may reorder slots 2-3, but it cannot change the Top-3 membership,
    replace the primary choice or rescue a weaker title from outside the validated result set.
    """

    CONTEXT_RECOMMENDER_VERSION = CONTEXT_RECOMMENDER_VERSION
    CONTEXT_PREDICTED_FLOOR = 6.0
    CONTEXT_CONFIDENCE_FLOOR = .45
    _SOFT_CONTEXT_LANES = {"related", "season", "atmosphere"}

    PERIOD_SLOT_MAX_VISIBLE = 3
    PERIOD_POOL_SIZE = 3
    PERIOD_FINAL_GAP = .085
    PERIOD_PREDICTED_FLOOR = 6.35
    PERIOD_PREDICTED_GAP = .80
    PERIOD_CALENDAR_MIN = .16
    PERIOD_SEASON_MIN = .68

    def _state_token(self) -> tuple:
        return super()._state_token() + (CONTEXT_RECOMMENDER_VERSION,)

    def _persistent_key(self, when, mode: str) -> str:
        base = super()._persistent_key(when, mode)
        return base + ":ctx38"

    @classmethod
    def _context_candidate_allowed(cls, rec, lane: str = "related") -> bool:
        score = rec.score
        if lane not in cls._SOFT_CONTEXT_LANES:
            # Factual/direct lanes can still be shown in Program calendar as factual material;
            # they are not allowed to become the normal Top 3 merely through this exception.
            return True
        predicted = float(getattr(score, "predicted_rating", 0.0) or 0.0)
        confidence = float(getattr(score, "confidence", 0.0) or 0.0)
        if confidence >= cls.CONTEXT_CONFIDENCE_FLOOR and predicted < cls.CONTEXT_PREDICTED_FLOOR:
            return False
        return True

    @staticmethod
    def _period_red_flag(rec) -> bool:
        payload = getattr(rec.score, "trust_audit", {}) or {}
        return bool(payload.get("red_flag")) or str(payload.get("status") or "") == "red_flag"

    @classmethod
    def _period_signal(cls, rec) -> float:
        score = rec.score
        calendar = clamp(float(getattr(score, "calendar", 0.0) or 0.0))
        season = clamp(float(getattr(score, "season", 0.0) or 0.0))
        kind = str(getattr(score, "calendar_kind", "") or "").lower()
        kind_weight = {
            "directă": 1.0,
            "directa": 1.0,
            "istorică": .92,
            "istorica": .92,
            "spirituală": .86,
            "spirituala": .86,
            "atmosferică": .46,
            "atmosferica": .46,
        }.get(kind, .70)
        seasonal = clamp((season - .30) / .70)
        return clamp(calendar * kind_weight + .22 * seasonal)

    @classmethod
    def _period_candidate_allowed(cls, rec, anchor) -> bool:
        if cls._period_red_flag(rec):
            return False
        score = rec.score
        anchor_score = anchor.score
        final = float(getattr(score, "final", 0.0) or 0.0)
        anchor_final = float(getattr(anchor_score, "final", 0.0) or 0.0)
        if anchor_final - final > cls.PERIOD_FINAL_GAP:
            return False

        predicted = float(getattr(score, "predicted_rating", 0.0) or 0.0)
        anchor_predicted = float(getattr(anchor_score, "predicted_rating", 0.0) or 0.0)
        confidence = float(getattr(score, "confidence", 0.0) or 0.0)
        if predicted < cls.PERIOD_PREDICTED_FLOOR:
            return False
        if anchor_predicted > 0 and anchor_predicted - predicted > cls.PERIOD_PREDICTED_GAP:
            return False
        if confidence >= .65 and predicted < 6.4:
            return False

        calendar = clamp(float(getattr(score, "calendar", 0.0) or 0.0))
        season = clamp(float(getattr(score, "season", 0.0) or 0.0))
        return calendar >= cls.PERIOD_CALENDAR_MIN or season >= cls.PERIOD_SEASON_MIN

    @classmethod
    def _period_aware_select(cls, ordered, requested: int):
        ordered = list(ordered)
        requested = max(1, int(requested))
        if not ordered or requested <= 1:
            return ordered[:requested]

        anchor = ordered[0]
        candidates = [
            rec for rec in ordered[1:requested]
            if cls._period_candidate_allowed(rec, anchor)
        ]
        if not candidates:
            return ordered[:requested]

        def value(rec):
            payload = getattr(rec.score, "trust_audit", {}) or {}
            trust_raw = payload.get("trust")
            trust = clamp(float(trust_raw)) if trust_raw is not None else .5
            predicted = float(getattr(rec.score, "predicted_rating", 0.0) or 0.0)
            predicted_norm = clamp((predicted - 5.5) / 3.5)
            return (
                .44 * cls._period_signal(rec)
                + .34 * float(getattr(rec.score, "final", 0.0) or 0.0)
                + .12 * trust
                + .10 * predicted_norm,
                float(getattr(rec.score, "final", 0.0) or 0.0),
                predicted,
            )

        period_pick = max(candidates, key=value)
        chosen = [anchor, period_pick]
        for rec in ordered[1:requested]:
            if len(chosen) >= requested:
                break
            if rec is period_pick:
                continue
            chosen.append(rec)

        reason = str(getattr(period_pick.score, "calendar_reason", "") or "").strip()
        if not reason:
            reason = "Se potrivește mai bine cu perioada curentă, rămânând în același culoar de calitate personală."
        period_pick.score.contributions = [
            item for item in period_pick.score.contributions
            if item[0] != "Potrivire cu perioada"
        ]
        period_pick.score.contributions.insert(
            0,
            (
                "Potrivire cu perioada",
                0.0,
                reason + " Contextul doar ordonează finaliștii Top 3 deja validați; nu schimbă nota estimată pentru tine.",
            ),
        )
        payload = dict(getattr(period_pick.score, "trust_audit", {}) or {})
        payload["period_context_slot"] = True
        payload["period_context_signal"] = round(cls._period_signal(period_pick), 6)
        payload["top3_role"] = "period_context"
        period_pick.score.trust_audit = payload
        return chosen[:requested]

    def _adaptive_rerank(self, recs, count: int):
        requested = max(1, int(count))
        # First obtain the exact same result set the already-validated downstream engine would
        # return for this request. This preserves candidate membership and the primary choice.
        selected = list(super()._adaptive_rerank(recs, requested))
        if requested > self.PERIOD_SLOT_MAX_VISIBLE or requested <= 1:
            return selected
        selected = self._period_aware_select(selected, requested)

        stats = getattr(self, "_quality_gate_stats", None)
        if isinstance(stats, dict):
            stats["period_context_slot"] = any(
                bool((getattr(rec.score, "trust_audit", {}) or {}).get("period_context_slot"))
                for rec in selected
            )

        role_stats = getattr(self, "_role_stats", None)
        if isinstance(role_stats, dict):
            roles = []
            for rec in selected:
                payload = getattr(rec.score, "trust_audit", {}) or {}
                roles.append(str(payload.get("top3_role") or "standard"))
            self._role_stats = {"enabled": True, "roles": roles}
        return selected

    def _merged_semantic(self, movie):
        merged = super()._merged_semantic(movie)
        semantic_for = getattr(self.calendar, "semantic_for", None)
        if callable(semantic_for):
            for key, value in semantic_for(movie).items():
                merged[key] = max(float(merged.get(key, 0.0) or 0.0), float(value or 0.0))
        return merged

    def _build_related_recommendations(self, when, existing_ids: set[int], count: int):
        recs = list(super()._build_related_recommendations(when, existing_ids, count))
        return [rec for rec in recs if self._context_candidate_allowed(rec, "related")]

    def calendar_day_program(self, when=None, count_per_section: int = 6) -> dict:
        result = super().calendar_day_program(when, count_per_section)
        filtered = []
        removed = 0
        for section in list(result.get("sections") or []):
            lane = str(section.get("key") or "")
            recs = list(section.get("recommendations") or [])
            kept = [rec for rec in recs if self._context_candidate_allowed(rec, lane)]
            removed += len(recs) - len(kept)
            if kept:
                copy = dict(section)
                copy["recommendations"] = kept
                filtered.append(copy)
        result["sections"] = filtered
        result["context_guard_removed"] = removed
        result["context_guard_floor"] = self.CONTEXT_PREDICTED_FLOOR
        result["context_recommender_version"] = CONTEXT_RECOMMENDER_VERSION
        context = getattr(self.calendar, "day_context", None)
        if callable(context) and result.get("date") is not None:
            result["context_intelligence"] = context(result["date"])
        return result

    def context_status(self) -> dict:
        return {
            "version": CONTEXT_RECOMMENDER_VERSION,
            "engine_class": type(self).__name__,
            "calendar_class": type(self.calendar).__name__,
            "predicted_floor": self.CONTEXT_PREDICTED_FLOOR,
            "confidence_floor": self.CONTEXT_CONFIDENCE_FLOOR,
            "soft_lanes": sorted(self._SOFT_CONTEXT_LANES),
            "period_slot": {
                "max_visible": self.PERIOD_SLOT_MAX_VISIBLE,
                "pool_size": self.PERIOD_POOL_SIZE,
                "final_gap": self.PERIOD_FINAL_GAP,
                "predicted_floor": self.PERIOD_PREDICTED_FLOOR,
                "calendar_min": self.PERIOD_CALENDAR_MIN,
                "season_min": self.PERIOD_SEASON_MIN,
                "membership_preserved": True,
                "primary_preserved": True,
            },
        }


class FastRecommendationEngineV16Context35(_ContextGuardMixin, FastRecommendationEngineV16):
    pass


class FastRecommendationEngineV17Context35(_ContextGuardMixin, FastRecommendationEngineV17):
    pass


def contextual_engine_class(base_cls):
    """Add the context guard without discarding the exact approved recommendation engine."""
    if str(getattr(base_cls, "CONTEXT_RECOMMENDER_VERSION", "")) == CONTEXT_RECOMMENDER_VERSION:
        return base_cls
    if base_cls is FastRecommendationEngineV17:
        return FastRecommendationEngineV17Context35
    if base_cls is FastRecommendationEngineV16:
        return FastRecommendationEngineV16Context35
    if not issubclass(base_cls, FastRecommendationEngineV16):
        return FastRecommendationEngineV16Context35

    with _CONTEXT_CLASS_LOCK:
        cached = _CONTEXT_CLASS_CACHE.get(base_cls)
        if cached is not None:
            return cached
        name = f"{base_cls.__name__}Context38"
        cls = type(
            name,
            (_ContextGuardMixin, base_cls),
            {
                "__module__": __name__,
                "__doc__": f"Context guard preserving {base_cls.__name__} exactly.",
            },
        )
        _CONTEXT_CLASS_CACHE[base_cls] = cls
        return cls
