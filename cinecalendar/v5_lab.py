from __future__ import annotations

from datetime import date

from .availability_guard_v37 import AvailabilityGuardMixinV37
from .recommender_v16 import FastRecommendationEngineV16
from .v5_pipeline import V5RecommendationPipeline, V5_PIPELINE_VERSION
from .v5_retrieval import V5_RETRIEVAL_VERSION
from .v5_personal_ranker import V5_RANKER_VERSION


V5_LAB_ENGINE_VERSION = "v5-lab-alpha1-retrieval-only"


class V5LabRecommendationEngine(AvailabilityGuardMixinV37, FastRecommendationEngineV16):
    """Legacy-compatible laboratory adapter for the composition-first V5 pipeline.

    Alpha 1 exposes only V5 retrieval to the existing scorer. The new personal utility model is
    trained and reported but intentionally does not reorder results yet: the first naive blend
    failed the external replay gate, so it remains a measured component rather than another patch.
    """

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.v5 = V5RecommendationPipeline(db, self)

    def _state_token(self) -> tuple:
        return super()._state_token() + (
            V5_LAB_ENGINE_VERSION,
            V5_PIPELINE_VERSION,
            V5_RETRIEVAL_VERSION,
            V5_RANKER_VERSION,
        )

    def _persistent_key(self, when, mode: str) -> str:
        return f"v5_lab_alpha1:{when.isoformat()}:{mode}"

    def _balanced_candidate_ids(self, when: date, limit: int) -> list[int]:
        baseline = list(AvailabilityGuardMixinV37._balanced_candidate_ids(self, when, limit))
        return self.v5.retrieval.expand(baseline, when=when)

    def candidate_generation_status(self) -> dict:
        status = dict(super().candidate_generation_status())
        status["v5"] = self.v5.status()
        return status
