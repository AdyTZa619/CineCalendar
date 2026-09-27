from __future__ import annotations

from .util import clamp
from .v5_lab import V5LabRecommendationEngine


V5_SHADOW_RANKED_VERSION = "v5-shadow-ranked-alpha1"


class V5ShadowRankedEngine(V5LabRecommendationEngine):
    """Evaluation-only V5 engine that lets the validated personal utility ranker reorder finalists.

    The normal V5 Alpha engine remains retrieval-only. This class is instantiated only by offline
    replay/evaluation, so a promising shadow model cannot silently change what the user sees.
    """

    @staticmethod
    def _blend(base_final: float, utility: float, weight: float) -> float:
        w = max(0.0, min(0.20, float(weight)))
        return clamp((1.0 - w) * clamp(float(base_final)) + w * clamp(float(utility)))

    def _adaptive_rerank(self, recs, count: int):
        requested = max(1, int(count))

        # For Top-3/Top-9 evaluation, obtain a mature finalist pool before applying the shadow
        # utility model. Calling the parent with >9 deliberately bypasses V16's final trust gate;
        # we re-run that gate after the V5 ordering so membership can actually change.
        pool_target = (
            max(requested, int(self.QUALITY_GATE_POOL_MAX))
            if requested <= int(self.QUALITY_GATE_MAX_VISIBLE)
            else requested
        )
        mature = list(super()._adaptive_rerank(recs, pool_target))

        knowledge = self.v5.knowledge.status()
        ranker_status = self.v5.personal_ranker.status()
        blend_weight = float(ranker_status.get("blend_weight", 0.0) or 0.0)
        active = bool(
            knowledge.get("ready_for_rich_ranker")
            and ranker_status.get("validated")
            and blend_weight > 0.0
        )

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
        else:
            reranked = mature

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
            "blend_weight": float(ranker.get("blend_weight", 0.0) or 0.0),
            "visible_ranking_changed": False,
        }
        return status
