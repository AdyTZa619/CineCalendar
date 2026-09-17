from __future__ import annotations

from .recommender_v16 import FastRecommendationEngineV16
from .recommender_v17 import FastRecommendationEngineV17


CONTEXT_RECOMMENDER_VERSION = "context-ranking-v3.5.0"


class _ContextGuardMixin:
    """Bound calendar/context influence without replacing long-term taste.

    The normal recommendation path already gives calendar a small weight. This layer focuses on
    the dedicated calendar program, where a contextual/thematic lane is useful but must not rescue
    a movie that the personal model actively expects the user to dislike.
    """

    CONTEXT_RECOMMENDER_VERSION = CONTEXT_RECOMMENDER_VERSION
    CONTEXT_PREDICTED_FLOOR = 6.0
    CONTEXT_CONFIDENCE_FLOOR = .45
    _SOFT_CONTEXT_LANES = {"related", "season", "atmosphere"}

    def _state_token(self) -> tuple:
        return super()._state_token() + (CONTEXT_RECOMMENDER_VERSION,)

    def _persistent_key(self, when, mode: str) -> str:
        base = super()._persistent_key(when, mode)
        return base + ":ctx35"

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
        }


class FastRecommendationEngineV16Context35(_ContextGuardMixin, FastRecommendationEngineV16):
    pass


class FastRecommendationEngineV17Context35(_ContextGuardMixin, FastRecommendationEngineV17):
    pass


def contextual_engine_class(base_cls):
    """Map the 3.4 quality verdict to its 3.5 context-bounded production class."""
    if issubclass(base_cls, FastRecommendationEngineV17):
        return FastRecommendationEngineV17Context35
    if issubclass(base_cls, FastRecommendationEngineV16):
        return FastRecommendationEngineV16Context35
    # Safety: never accept an unrelated recommender from a corrupted/stale quality verdict.
    return FastRecommendationEngineV16Context35
