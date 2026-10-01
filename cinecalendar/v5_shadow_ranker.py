from __future__ import annotations

from .util import clamp
import threading
from .v5_lab import DiscoveryRecommendationEngine, discovery_engine_class
from .recent_taste_context import RecentTasteContext
from .top3_strategy import select_controlled_top3


V5_SHADOW_RANKED_VERSION = "v5-shadow-ranked-alpha2"
ADAPTIVE_RUNTIME_VERSION = "adaptive-runtime-v2-recent-diverse"


class V5ShadowRankedEngine(DiscoveryRecommendationEngine):
    SHADOW_BLEND_OVERRIDE: float | None = None
    """Evaluation-only V5 engine that lets the validated personal utility ranker reorder finalists.

    The adaptive model is layered on top of the conservative Discovery engine. This class is
    instantiated for Alpha/replay only, so the personal reorder cannot silently change Stable.
    """

    @staticmethod
    def _blend(base_final: float, utility: float, weight: float) -> float:
        w = max(0.0, min(0.20, float(weight)))
        return clamp((1.0 - w) * clamp(float(base_final)) + w * clamp(float(utility)))

    def _adaptive_rerank(self, recs, count: int):
        requested = max(1, int(count))
        knowledge = self.v5.knowledge.status()
        ranker_status = self.v5.personal_ranker.status()
        learned_blend = float(ranker_status.get("blend_weight", 0.0) or 0.0)
        blend_weight = (
            learned_blend
            if self.SHADOW_BLEND_OVERRIDE is None
            else max(0.0, min(0.20, float(self.SHADOW_BLEND_OVERRIDE)))
        )
        active = bool(
            knowledge.get("ready_for_rich_ranker")
            and ranker_status.get("validated")
            and blend_weight > 0.0
        )

        # If the ranker is not valid for this historical fold, preserve the parent's exact
        # behaviour. This makes the shadow comparison neutral rather than accidentally changing
        # V16's Top-3 gate pool simply because the experimental model is inactive.
        if not active:
            return super()._adaptive_rerank(recs, requested)

        # For Top-3/Top-9 evaluation, obtain a mature finalist pool before applying the shadow
        # utility model. Calling the parent with >9 deliberately bypasses V16's final trust gate;
        # we re-run that gate after the V5 ordering so membership can actually change.
        pool_target = (
            max(requested, int(self.QUALITY_GATE_POOL_MAX))
            if requested <= int(self.QUALITY_GATE_MAX_VISIBLE)
            else requested
        )
        mature = list(super()._adaptive_rerank(recs, pool_target))

        if active:
            reranked = []
            for rec in mature:
                payload = self.v5.personal_ranker.score(rec.movie)
                if bool(payload.get("active")):
                    base = float(rec.score.final)
                    utility = float(payload.get("utility", 0.0) or 0.0)
                    combined = self._blend(base, utility, blend_weight)
                    rec.score.score_factors["v5_shadow_base_final"] = base
                    rec.score.score_factors["v5_shadow_utility"] = utility
                    rec.score.score_factors["v5_shadow_like_score"] = float(payload.get("like_score", 0.0) or 0.0)
                    rec.score.score_factors["v5_shadow_dislike_risk"] = float(payload.get("dislike_risk", 0.0) or 0.0)
                    rec.score.score_factors["v5_shadow_blend_weight"] = blend_weight
                    rec.score.final = combined
                    rec.score.contributions.insert(
                        0,
                        (
                            "V5 shadow utility",
                            (combined - base) * 100.0,
                            "Test offline: P(8+) minus riscul P(1–4); nu modifică recomandările live.",
                        ),
                    )
                reranked.append(rec)
            reranked.sort(
                key=lambda rec: (
                    float(rec.score.final),
                    float(rec.score.predicted_rating or 0.0),
                    float(rec.score.confidence or 0.0),
                ),
                reverse=True,
            )
        if requested <= int(self.QUALITY_GATE_MAX_VISIBLE):
            return self._quality_gate(reranked, requested)
        return reranked[:requested]

    def candidate_generation_status(self) -> dict:
        status = dict(super().candidate_generation_status())
        knowledge = self.v5.knowledge.status()
        ranker = self.v5.personal_ranker.status()
        status["v5_shadow_ranked"] = {
            "version": V5_SHADOW_RANKED_VERSION,
            "knowledge_ready": bool(knowledge.get("ready_for_rich_ranker")),
            "ranker_validated": bool(ranker.get("validated")),
            "blend_weight": (
                float(ranker.get("blend_weight", 0.0) or 0.0)
                if self.SHADOW_BLEND_OVERRIDE is None
                else float(self.SHADOW_BLEND_OVERRIDE)
            ),
            "learned_blend_weight": float(ranker.get("blend_weight", 0.0) or 0.0),
            "visible_ranking_changed": False,
        }
        return status


class V5ShadowRankedEngine10(V5ShadowRankedEngine):
    SHADOW_BLEND_OVERRIDE = 0.10


class V5ShadowRankedEngine15(V5ShadowRankedEngine):
    SHADOW_BLEND_OVERRIDE = 0.15


class V5ShadowRankedEngine20(V5ShadowRankedEngine):
    SHADOW_BLEND_OVERRIDE = 0.20


_ADAPTIVE_RUNTIME_CACHE: dict[tuple[type, str, str], type] = {}
_ADAPTIVE_RUNTIME_LOCK = threading.RLock()


def adaptive_engine_class(
    base_cls: type,
    discovery_variant: str = "balanced",
    blend_weight: float | None = 0.20,
) -> type:
    """Layer the personal Adaptive re-ranker over the exact Stable + Discovery engine."""
    discovery_cls = discovery_engine_class(base_cls, discovery_variant)
    override = (
        None
        if blend_weight is None
        else round(max(0.0, min(0.20, float(blend_weight))), 2)
    )
    key = (
        base_cls,
        str(discovery_variant),
        "learned" if override is None else f"{override:.2f}",
    )
    with _ADAPTIVE_RUNTIME_LOCK:
        cached = _ADAPTIVE_RUNTIME_CACHE.get(key)
        if cached is not None:
            return cached

        class RuntimeAdaptive(discovery_cls):
            SHADOW_BLEND_OVERRIDE = override
            ADAPTIVE_RUNTIME = True

            def __init__(self, db, calendar=None):
                super().__init__(db, calendar)
                self.recent_taste = RecentTasteContext(db)
                self._adaptive_roles = []

            def _state_token(self) -> tuple:
                return super()._state_token() + (
                    ADAPTIVE_RUNTIME_VERSION,
                    self.SHADOW_BLEND_OVERRIDE,
                )

            @staticmethod
            def _blend(base_final: float, utility: float, blend: float) -> float:
                w = max(0.0, min(0.20, float(blend)))
                return clamp((1.0 - w) * clamp(float(base_final)) + w * clamp(float(utility)))

            def _adaptive_rerank(self, recs, count: int):
                requested = max(1, int(count))
                knowledge = self.v5.knowledge.status()
                ranker_status = self.v5.personal_ranker.status()
                learned_blend = float(ranker_status.get("blend_weight", 0.0) or 0.0)
                effective_blend = (
                    learned_blend
                    if self.SHADOW_BLEND_OVERRIDE is None
                    else float(self.SHADOW_BLEND_OVERRIDE)
                )
                active = bool(
                    knowledge.get("ready_for_rich_ranker")
                    and ranker_status.get("validated")
                    and effective_blend > 0.0
                )
                if not active:
                    return super()._adaptive_rerank(recs, requested)

                pool_target = (
                    max(requested, int(self.QUALITY_GATE_POOL_MAX))
                    if requested <= int(self.QUALITY_GATE_MAX_VISIBLE)
                    else requested
                )
                mature = list(super()._adaptive_rerank(recs, pool_target))
                reranked = []
                personal_payloads = self.v5.personal_ranker.score_many([rec.movie for rec in mature])
                recent_payloads = self.recent_taste.score_many([rec.movie for rec in mature])
                for rec, payload, recent in zip(mature, personal_payloads, recent_payloads):
                    base = float(rec.score.final)
                    combined = base
                    if bool(payload.get("active")):
                        utility = float(payload.get("utility", 0.0) or 0.0)
                        combined = self._blend(base, utility, effective_blend)
                        rec.score.score_factors["adaptive_base_final"] = base
                        rec.score.score_factors["adaptive_utility"] = utility
                        rec.score.score_factors["adaptive_like_score"] = float(
                            payload.get("like_score", 0.0) or 0.0
                        )
                        rec.score.score_factors["adaptive_dislike_risk"] = float(
                            payload.get("dislike_risk", 0.0) or 0.0
                        )
                        rec.score.score_factors["adaptive_blend_weight"] = effective_blend
                        rec.score.contributions.insert(
                            0,
                            (
                                "Adaptiv",
                                (combined - base) * 100.0,
                                "Model personal: probabilitate 8+ minus riscul 1–4.",
                            ),
                        )

                    if bool(recent.get("active")):
                        nudge = float(recent.get("nudge", 0.0) or 0.0)
                        before_recent = combined
                        combined = clamp(combined + nudge)
                        rec.score.score_factors["recent_taste_score"] = float(
                            recent.get("score", 0.5) or 0.5
                        )
                        rec.score.score_factors["recent_taste_nudge"] = nudge
                        rec.score.score_factors["recent_taste_confidence"] = float(
                            recent.get("confidence", 0.0) or 0.0
                        )
                        if abs(nudge) >= 0.001:
                            rec.score.contributions.insert(
                                0,
                                (
                                    "Gust recent",
                                    (combined - before_recent) * 100.0,
                                    "Semnal scurt: ce ți-a plăcut sau displăcut în perioada recentă, fără a șterge gustul de bază.",
                                ),
                            )

                    rec.score.final = combined
                    reranked.append(rec)

                reranked.sort(
                    key=lambda rec: (
                        float(rec.score.final),
                        float(rec.score.predicted_rating or 0.0),
                        float(rec.score.confidence or 0.0),
                    ),
                    reverse=True,
                )
                if requested <= int(self.QUALITY_GATE_MAX_VISIBLE):
                    if requested <= 3:
                        gate_count = min(
                            len(reranked),
                            max(int(self.QUALITY_GATE_POOL_MIN), requested * 8),
                        )
                        gated = self._quality_gate(reranked, gate_count)
                        selected, roles = select_controlled_top3(list(gated), requested)
                        self._adaptive_roles = list(roles)
                        self._quality_gate_stats = {
                            **dict(self._quality_gate_stats),
                            "role_strategy": True,
                            "roles": list(roles),
                            "returned": len(selected),
                        }
                        return selected
                    self._adaptive_roles = []
                    return self._quality_gate(reranked, requested)
                self._adaptive_roles = []
                return reranked[:requested]

            def candidate_generation_status(self) -> dict:
                status = dict(super().candidate_generation_status())
                ranker = self.v5.personal_ranker.status()
                effective_blend = (
                    float(ranker.get("blend_weight", 0.0) or 0.0)
                    if self.SHADOW_BLEND_OVERRIDE is None
                    else float(self.SHADOW_BLEND_OVERRIDE)
                )
                status["adaptive"] = {
                    "version": ADAPTIVE_RUNTIME_VERSION,
                    "blend_weight": effective_blend,
                    "blend_mode": "learned" if self.SHADOW_BLEND_OVERRIDE is None else "fixed",
                    "visible_ranking_changed": True,
                    "base": base_cls.__name__,
                    "discovery_variant": str(discovery_variant),
                    "recent_taste": self.recent_taste.status(),
                    "top3_roles": list(getattr(self, "_adaptive_roles", [])),
                }
                return status

        RuntimeAdaptive.__name__ = (
            f"{base_cls.__name__}AdaptiveLearned"
            if override is None
            else f"{base_cls.__name__}Adaptive{int(override * 100):02d}"
        )
        RuntimeAdaptive.__qualname__ = RuntimeAdaptive.__name__
        _ADAPTIVE_RUNTIME_CACHE[key] = RuntimeAdaptive
        return RuntimeAdaptive
