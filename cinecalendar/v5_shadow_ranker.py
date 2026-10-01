from __future__ import annotations

from .util import clamp
from .v5_lab import DiscoveryRecommendationEngine


V5_SHADOW_RANKED_VERSION = "v5-shadow-ranked-alpha1"


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
