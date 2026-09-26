from __future__ import annotations

from .v5_knowledge import V5KnowledgeBase
from .v5_personal_ranker import PersonalUtilityRankerV5
from .v5_retrieval import UnifiedCandidateRetrieverV5


V5_PIPELINE_VERSION = "v5-pipeline-alpha1"


class V5RecommendationPipeline:
    """Composition-first V5 core.

    Alpha 1 deliberately delegates the proven scoring surface to the supplied baseline engine,
    while candidate discovery and the new personal utility model live as independent components.
    This is the migration seam away from the V12/V15/V16 inheritance chain: future V5 scoring can
    replace one component without changing retrieval, context or evaluation code.
    """

    def __init__(self, db, baseline_engine):
        self.db = db
        self.baseline_engine = baseline_engine
        self.retrieval = UnifiedCandidateRetrieverV5(db, baseline_engine.collaborative)
        self.knowledge = V5KnowledgeBase(db)
        self.personal_ranker = PersonalUtilityRankerV5(db)

    def candidate_ids(self, when, limit: int) -> list[int]:
        baseline = list(self.baseline_engine._balanced_candidate_ids(when, limit))
        expanded = self.retrieval.expand(baseline, when=when)
        baseline_set = set(int(mid) for mid in baseline)
        frontier = [int(mid) for mid in expanded if int(mid) not in baseline_set]
        if frontier:
            self.knowledge.queue_frontier(frontier)
        return expanded

    def prepare_knowledge(self, limit: int = 0) -> dict:
        return self.knowledge.seed_profile(limit=limit)

    def utility(self, movie) -> dict:
        score = self.personal_ranker.score(movie)
        readiness = self.knowledge.status()
        score["knowledge_ready"] = bool(readiness.get("ready_for_rich_ranker"))
        if not score["knowledge_ready"]:
            score["active"] = False
            score["inactive_reason"] = "rich_metadata_not_ready"
        return score

    def status(self) -> dict:
        return {
            "version": V5_PIPELINE_VERSION,
            "baseline_engine": type(self.baseline_engine).__name__,
            "retrieval": self.retrieval.status(),
            "knowledge": self.knowledge.status(),
            "personal_ranker": self.personal_ranker.status(),
        }
