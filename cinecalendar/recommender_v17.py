from __future__ import annotations

from .models import Recommendation
from .personal_candidates_v34 import PersonalCandidateGeneratorV34
from .recommender_v15 import FastRecommendationEngineV15
from .recommender_v16 import FastRecommendationEngineV16
from .semantic import feature_vector
from .util import clamp, cosine_sparse


ENGINE_VERSION = "17.0.0-quality-measured"


class FastRecommendationEngineV17(FastRecommendationEngineV16):
    """Quality challenger for 3.4, activated only after a local temporal backtest win.

    The long-term taste model and V16 trust rules remain the safety baseline. V17 changes two
    intentionally narrow surfaces:
      * retrieval: 8/10 films can act as lower-weight positive anchors in the favourite lane;
      * visible Top 3: among already competitive/safe finalists, pick an anchor, a contextual
        alternative and a genuinely different exploration option instead of three near-clones.

    It never receives automatic production preference merely because it is newer. The 3.4 quality
    manager compares it with V16 on the user's own hidden recent ratings and keeps V16 unless V17
    wins without material recall/dislike regressions.
    """

    ROLE_FINAL_GAP = 0.11
    ROLE_TRUST_FLOOR = 0.56

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.personal_candidates = PersonalCandidateGeneratorV34(db, self.collaborative)
        self._role_stats = {"enabled": False, "roles": []}

    def _state_token(self) -> tuple:
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when, mode: str) -> str:
        return f"decision_pool_v17:{when.isoformat()}:{mode}"

    @staticmethod
    def _trust(rec: Recommendation) -> float:
        payload = getattr(rec.score, "trust_audit", {}) or {}
        value = payload.get("trust")
        return clamp(float(value)) if value is not None else 0.5

    @staticmethod
    def _safe(rec: Recommendation) -> bool:
        payload = getattr(rec.score, "trust_audit", {}) or {}
        return str(payload.get("status") or "") != "red_flag" and not bool(payload.get("red_flag"))

    @staticmethod
    def _role_note(rec: Recommendation, role: str, reason: str) -> None:
        payload = dict(getattr(rec.score, "trust_audit", {}) or {})
        payload["top3_role"] = role
        rec.score.trust_audit = payload
        rec.score.contributions = [item for item in rec.score.contributions if item[0] != "Rol Top 3"]
        rec.score.contributions.insert(0, ("Rol Top 3", 0.0, reason))

    @staticmethod
    def _diversity_against(rec: Recommendation, selected: list[Recommendation]) -> float:
        if not selected:
            return 1.0
        current = feature_vector(rec.movie)
        if not current:
            return 0.5
        similarities = []
        for chosen in selected:
            other = feature_vector(chosen.movie)
            if other:
                similarities.append(cosine_sparse(current, other))
        if not similarities:
            return 0.5
        return clamp(1.0 - max(similarities))

    def _role_select(self, ordered: list[Recommendation], requested: int) -> list[Recommendation]:
        requested = max(1, int(requested))
        if not ordered:
            self._role_stats = {"enabled": False, "roles": []}
            return []

        safe = [rec for rec in ordered if self._safe(rec)]
        source = safe or list(ordered)
        anchor = source[0]
        chosen = [anchor]
        roles = ["anchor"]
        self._role_note(
            anchor,
            "anchor",
            "Alegerea principală: cel mai puternic finalist sigur după gust, încredere și poarta V16.",
        )
        if requested == 1:
            self._role_stats = {"enabled": True, "roles": roles}
            return chosen

        anchor_final = float(anchor.score.final)
        remaining = [rec for rec in safe if rec is not anchor]
        context_candidates = [
            rec for rec in remaining
            if anchor_final - float(rec.score.final) <= self.ROLE_FINAL_GAP
            and float(rec.score.predicted_rating or 0.0) >= 6.4
        ]
        context_signal = [
            rec for rec in context_candidates
            if max(float(rec.score.calendar or 0.0), float(rec.score.season or 0.0)) >= 0.30
        ]
        context_pool = context_signal or context_candidates
        if context_pool:
            context_pick = max(
                context_pool,
                key=lambda rec: (
                    0.52 * float(rec.score.final)
                    + 0.23 * self._trust(rec)
                    + 0.17 * clamp(float(rec.score.calendar or 0.0))
                    + 0.08 * clamp(float(rec.score.season or 0.0)),
                    float(rec.score.predicted_rating or 0.0),
                ),
            )
        else:
            context_pick = remaining[0] if remaining else None
        if context_pick is not None:
            chosen.append(context_pick)
            roles.append("context")
            self._role_note(
                context_pick,
                "context",
                "Alternativă apropiată ca valoare, preferată când contextul zilei/sezonului aduce informație reală fără să învingă gustul de bază.",
            )
        if len(chosen) >= requested:
            self._role_stats = {"enabled": True, "roles": roles}
            return chosen[:requested]

        remaining = [rec for rec in safe if rec not in chosen]
        explore_pool = [
            rec for rec in remaining
            if anchor_final - float(rec.score.final) <= self.ROLE_FINAL_GAP
            and self._trust(rec) >= self.ROLE_TRUST_FLOOR
            and float(rec.score.predicted_rating or 0.0) >= 6.5
            and float(rec.score.confidence or 0.0) >= 0.40
        ]
        if explore_pool:
            def explore_value(rec: Recommendation) -> tuple[float, float, float]:
                diversity = self._diversity_against(rec, chosen)
                predicted_norm = clamp((float(rec.score.predicted_rating or 0.0) - 5.5) / 3.5)
                value = (
                    0.48 * float(rec.score.final)
                    + 0.22 * self._trust(rec)
                    + 0.18 * diversity
                    + 0.07 * clamp(float(rec.score.novelty or 0.0))
                    + 0.05 * predicted_norm
                )
                return value, diversity, float(rec.score.predicted_rating or 0.0)
            explore_pick = max(explore_pool, key=explore_value)
        else:
            explore_pick = remaining[0] if remaining else None
        if explore_pick is not None:
            chosen.append(explore_pick)
            roles.append("explore")
            self._role_note(
                explore_pick,
                "explore",
                "Explorare controlată: diferit de primele alegeri, dar păstrat în același culoar de scor, încredere și estimare personală.",
            )

        for rec in ordered:
            if len(chosen) >= requested:
                break
            if rec in chosen:
                continue
            chosen.append(rec)
            roles.append("standard")
            self._role_note(rec, "standard", "Rezultat suplimentar în ordinea standard de calitate.")

        self._role_stats = {"enabled": True, "roles": roles[:requested]}
        return chosen[:requested]

    def role_status(self) -> dict:
        return dict(self._role_stats)

    def _adaptive_rerank(self, recs: list[Recommendation], count: int) -> list[Recommendation]:
        requested = max(1, int(count))
        if requested > self.QUALITY_GATE_MAX_VISIBLE:
            self._role_stats = {"enabled": False, "roles": []}
            return FastRecommendationEngineV16._adaptive_rerank(self, recs, requested)

        gate_pool_size = min(
            len(recs),
            self.QUALITY_GATE_POOL_MAX,
            max(self.QUALITY_GATE_POOL_MIN, requested * 8),
        )
        mature_pool = FastRecommendationEngineV15._adaptive_rerank(self, recs, gate_pool_size)
        ordered = FastRecommendationEngineV16._quality_gate(self, mature_pool, len(mature_pool))
        selected = self._role_select(list(ordered), requested)

        trusted = sum(
            1 for rec in mature_pool
            if str((getattr(rec.score, "trust_audit", {}) or {}).get("status") or "") == "trusted"
        )
        red_flags = sum(
            1 for rec in mature_pool
            if str((getattr(rec.score, "trust_audit", {}) or {}).get("status") or "") == "red_flag"
        )
        trusted_returned = sum(
            1 for rec in selected
            if str((getattr(rec.score, "trust_audit", {}) or {}).get("status") or "") == "trusted"
        )
        self._quality_gate_stats = {
            "pool": len(mature_pool),
            "trusted": trusted,
            "red_flags": red_flags,
            "fallback": max(0, len(selected) - trusted_returned),
            "returned": len(selected),
            "bypassed": False,
            "role_strategy": True,
        }
        return selected
