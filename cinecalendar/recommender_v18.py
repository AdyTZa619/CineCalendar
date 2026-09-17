from __future__ import annotations

from datetime import date

from .local_content_v36 import LocalContentCandidateGeneratorV36
from .recommender_v17 import FastRecommendationEngineV17


ENGINE_VERSION = "18.0.0-local-content-retrieval"


class FastRecommendationEngineV18(FastRecommendationEngineV17):
    """3.6 challenger: V17 plus a small, independent local metadata retrieval lane.

    The new lane changes candidate discovery only. All scoring, adaptive learning, Watch Success,
    Top-3 roles and V16 trust/red-flag gates remain inherited unchanged. Production may use V18
    only after the 3.6 quality manager proves it better than the user's already-approved baseline.
    """

    LOCAL_CONTENT_SHARE = 0.14

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.local_content = LocalContentCandidateGeneratorV36(db)
        self._accuracy_mix = {
            "enabled": False,
            "baseline": 0,
            "local_content": 0,
            "total": 0,
        }

    def _state_token(self) -> tuple:
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when, mode: str) -> str:
        return f"decision_pool_v18:{when.isoformat()}:{mode}"

    @staticmethod
    def _merge_accuracy_candidates(baseline: list[int], local_ids: list[int], limit: int, local_share: float):
        limit = max(1, int(limit))
        local_target = min(len(local_ids), max(0, int(round(limit * max(0.0, min(0.25, float(local_share)))))))
        if local_target <= 0:
            return list(baseline[:limit]), {"baseline": min(len(baseline), limit), "local_content": 0, "total": min(len(baseline), limit)}

        # Interleave rather than prepend. Baseline discovery remains dominant at every depth while
        # the local lane gets enough exposure for candidate-recall backtests to measure it.
        local = list(local_ids[:local_target])
        out: list[int] = []
        seen: set[int] = set()
        bi = li = 0
        stride = max(3, int(round((1.0 - local_share) / max(0.01, local_share))))
        baseline_count = local_count = 0

        while len(out) < limit and (bi < len(baseline) or li < len(local)):
            for _ in range(stride):
                if bi >= len(baseline) or len(out) >= limit:
                    break
                mid = int(baseline[bi]); bi += 1
                if mid in seen:
                    continue
                seen.add(mid); out.append(mid); baseline_count += 1
            if li < len(local) and len(out) < limit:
                mid = int(local[li]); li += 1
                if mid not in seen:
                    seen.add(mid); out.append(mid); local_count += 1

        # Fill any remaining capacity from the proven baseline first, then unused local results.
        for source, label in ((baseline[bi:], "baseline"), (local[li:], "local")):
            for raw in source:
                if len(out) >= limit:
                    break
                mid = int(raw)
                if mid in seen:
                    continue
                seen.add(mid); out.append(mid)
                if label == "baseline": baseline_count += 1
                else: local_count += 1
            if len(out) >= limit:
                break

        return out, {"baseline": baseline_count, "local_content": local_count, "total": len(out)}

    def _balanced_candidate_ids(self, when: date, limit: int) -> list[int]:
        baseline = list(super()._balanced_candidate_ids(when, limit))
        try:
            local_ids = self.local_content.candidates(max(140, int(limit * 0.30)))
        except Exception:
            # Candidate enrichment is fail-open by design. A metadata/SQLite edge case must never
            # degrade or block the already-proven V17/V16 path.
            local_ids = []
        merged, stats = self._merge_accuracy_candidates(
            baseline,
            local_ids,
            limit,
            self.LOCAL_CONTENT_SHARE,
        )
        self._accuracy_mix = {
            "enabled": bool(stats.get("local_content")),
            **stats,
            "retrieval": self.local_content.status(),
        }
        return merged

    def candidate_generation_status(self) -> dict:
        status = dict(super().candidate_generation_status())
        status["accuracy_v36"] = dict(self._accuracy_mix)
        return status
