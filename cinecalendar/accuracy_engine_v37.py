from __future__ import annotations

from datetime import date
import threading

from .local_content_v36 import LocalContentCandidateGeneratorV36
from .recommender_v16 import FastRecommendationEngineV16


ENGINE_VERSION = "19.0.0-personal-calibrated-local-retrieval"
_ALLOWED_SHARES = (0.08, 0.14, 0.20)
_CLASS_CACHE: dict[tuple[type, int], type] = {}
_CLASS_LOCK = threading.RLock()


class LocalContentAccuracyMixinV37:
    """Add the 3.6 local-content lane to the *actual* approved taste baseline.

    3.6's V18 always inherited V17, which means a user whose proven baseline was V16 could test
    two changes at once (V17 + local retrieval). 3.7 removes that confounder. The challenger now
    subclasses exactly the engine already approved for this user, and only candidate discovery is
    changed. Downstream scoring, adaptive learning, Watch Success, role selection and trust gates
    stay inherited from that exact baseline.
    """

    LOCAL_CONTENT_SHARE = 0.14

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.local_content = LocalContentCandidateGeneratorV36(db)
        self._accuracy_mix_v37 = {
            "enabled": False,
            "baseline": 0,
            "local_content": 0,
            "total": 0,
            "local_share": float(self.LOCAL_CONTENT_SHARE),
        }

    def _state_token(self) -> tuple:
        return super()._state_token() + (ENGINE_VERSION, round(float(self.LOCAL_CONTENT_SHARE), 4))

    def _persistent_key(self, when, mode: str) -> str:
        share = int(round(float(self.LOCAL_CONTENT_SHARE) * 100))
        return f"decision_pool_v19_{type(self).__name__}_{share}:{when.isoformat()}:{mode}"

    @staticmethod
    def _merge_accuracy_candidates(
        baseline: list[int],
        local_ids: list[int],
        limit: int,
        local_share: float,
    ) -> tuple[list[int], dict[str, int]]:
        limit = max(1, int(limit))
        share = max(0.0, min(0.22, float(local_share)))
        local_target = min(len(local_ids), max(0, int(round(limit * share))))
        if local_target <= 0:
            count = min(len(baseline), limit)
            return list(baseline[:limit]), {"baseline": count, "local_content": 0, "total": count}

        local = list(local_ids[:local_target])
        out: list[int] = []
        seen: set[int] = set()
        bi = li = 0
        baseline_count = local_count = 0
        stride = max(3, int(round((1.0 - share) / max(0.01, share))))

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

        for source, label in ((baseline[bi:], "baseline"), (local[li:], "local")):
            for raw in source:
                if len(out) >= limit:
                    break
                mid = int(raw)
                if mid in seen:
                    continue
                seen.add(mid); out.append(mid)
                if label == "baseline":
                    baseline_count += 1
                else:
                    local_count += 1
            if len(out) >= limit:
                break

        return out, {"baseline": baseline_count, "local_content": local_count, "total": len(out)}

    def _balanced_candidate_ids(self, when: date, limit: int) -> list[int]:
        baseline = list(super()._balanced_candidate_ids(when, limit))
        try:
            local_ids = self.local_content.candidates(max(140, int(limit * 0.34)))
        except Exception:
            local_ids = []
        merged, stats = self._merge_accuracy_candidates(
            baseline,
            local_ids,
            limit,
            float(self.LOCAL_CONTENT_SHARE),
        )
        self._accuracy_mix_v37 = {
            "enabled": bool(stats.get("local_content")),
            **stats,
            "local_share": float(self.LOCAL_CONTENT_SHARE),
            "retrieval": self.local_content.status(),
        }
        return merged

    def candidate_generation_status(self) -> dict:
        status = dict(super().candidate_generation_status())
        status["accuracy_v37"] = dict(self._accuracy_mix_v37)
        return status


def calibrated_accuracy_engine_class(base_cls: type, local_share: float = 0.14) -> type:
    """Return a stable dynamic challenger for one approved baseline + one calibrated share."""
    if not issubclass(base_cls, FastRecommendationEngineV16):
        raise TypeError("3.7 accuracy challenger requires a V16-compatible baseline")
    share = min(_ALLOWED_SHARES, key=lambda value: abs(float(value) - float(local_share)))
    share_pct = int(round(share * 100))
    key = (base_cls, share_pct)
    with _CLASS_LOCK:
        cached = _CLASS_CACHE.get(key)
        if cached is not None:
            return cached
        name = f"FastRecommendationEngineV19_{base_cls.__name__}_L{share_pct:02d}"
        cls = type(
            name,
            (LocalContentAccuracyMixinV37, base_cls),
            {
                "__module__": __name__,
                "LOCAL_CONTENT_SHARE": float(share),
                "__doc__": (
                    f"3.7 calibrated challenger: {base_cls.__name__} + "
                    f"{share_pct}% local-content candidate lane."
                ),
            },
        )
        _CLASS_CACHE[key] = cls
        return cls


def allowed_local_shares() -> tuple[float, ...]:
    return _ALLOWED_SHARES
