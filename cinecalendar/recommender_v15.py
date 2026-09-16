from __future__ import annotations

from datetime import date
import math

from .models import Recommendation
from .recommender_v13 import FastRecommendationEngineV13
from .recommender_v14 import FastRecommendationEngineV14
from .util import clamp
from .watch_success import WatchSuccessIntentLearner


ENGINE_VERSION = "15.1.0-watch-success-truthful-funnel"


class FastRecommendationEngineV15(FastRecommendationEngineV14):
    """V14 + bounded Watch Success and Startability.

    Long-term taste remains the gatekeeper. Short-horizon intent can only influence candidates that
    are still reasonably close to the best long-term fit, and Startability is stricter still: it is
    only allowed to break near-ties. Generic assumptions such as "a 105-minute film is easier to
    start" get a small voice; real Watch Success evidence can increase that voice, but never enough
    to rescue a substantially weaker taste match. The estimated personal rating is never changed by
    either short-horizon layer.
    """

    INTENT_GOOD_MATCH_MARGIN = 0.16
    STARTABILITY_GENERIC_MAX = 0.07
    STARTABILITY_EVIDENCE_MAX = 0.14
    STARTABILITY_NEAR_TIE_MARGIN = 0.08

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.watch_intent = WatchSuccessIntentLearner(db)

    def _state_token(self) -> tuple:
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when: date, mode: str) -> str:
        return f"decision_pool_v15_1:{when.isoformat()}:{mode}"

    @staticmethod
    def _runtime_score(minutes: int | None) -> float:
        if not minutes:
            return 0.66
        minutes = int(minutes)
        if 85 <= minutes <= 125:
            return 1.0
        if 70 <= minutes < 85:
            return 0.86
        if 126 <= minutes <= 145:
            return 0.88
        if 146 <= minutes <= 165:
            return 0.72
        if 166 <= minutes <= 190:
            return 0.58
        if minutes > 190:
            return 0.43
        return 0.68

    @staticmethod
    def _metadata_score(movie) -> float:
        parts = 0.0
        parts += 0.24 if movie.genres else 0.0
        parts += 0.16 if movie.directors else 0.0
        parts += 0.12 if movie.runtime_min else 0.0
        parts += 0.22 if (movie.overview or "").strip() else 0.0
        parts += 0.12 if movie.poster_url else 0.0
        parts += 0.08 if movie.year else 0.0
        parts += 0.06 if movie.keywords or movie.semantic else 0.0
        return clamp(parts)

    @staticmethod
    def _premise_score(movie) -> float:
        text = (movie.overview or "").strip()
        if len(text) >= 180:
            return 1.0
        if len(text) >= 90:
            return 0.90
        if len(text) >= 45:
            return 0.72
        if text:
            return 0.58
        return 0.42

    @staticmethod
    def _quality_score(movie) -> float:
        rating = float(movie.imdb_rating or 0.0)
        votes = max(0, int(movie.num_votes or 0))
        rating_part = clamp((rating - 5.5) / 3.0) if rating > 0 else 0.45
        if votes <= 0:
            vote_part = 0.35
        else:
            vote_part = clamp(math.log1p(votes) / math.log1p(250_000))
        return clamp(0.62 * rating_part + 0.38 * vote_part)

    def _startability(self, rec: Recommendation, intent_payload: dict | None = None) -> tuple[float, str]:
        m, s = rec.movie, rec.score
        taste = clamp((float(s.predicted_rating) - 5.0) / 5.0)
        confidence = clamp(float(s.confidence))
        runtime = self._runtime_score(m.runtime_min)
        premise = self._premise_score(m)
        metadata = self._metadata_score(m)
        quality = self._quality_score(m)

        intent_payload = intent_payload or {}
        intent = float(intent_payload.get("score", 0.5) or 0.5) if intent_payload.get("active") else 0.5

        # This is a bounded heuristic used only to break close ties. It is deliberately not called
        # a probability: there is no calibrated ground-truth dataset yet for "will start tonight".
        score = clamp(
            0.40 * taste
            + 0.15 * confidence
            + 0.18 * intent
            + 0.09 * runtime
            + 0.07 * premise
            + 0.04 * metadata
            + 0.07 * quality
        )

        bits: list[str] = []
        if m.runtime_min:
            if int(m.runtime_min) <= 125:
                bits.append(f"{int(m.runtime_min)} min")
            elif int(m.runtime_min) >= 166:
                bits.append(f"mai lung ({int(m.runtime_min)} min)")
        if premise >= 0.90:
            bits.append("premisă clară")
        if quality >= 0.70:
            bits.append("semnale publice solide")
        if intent >= 0.64:
            bits.append("se potrivește cu traseele recente de vizionare")

        if score >= 0.76:
            lead = "Ușurință de pornire ridicată"
        elif score >= 0.64:
            lead = "Ușurință de pornire bună"
        elif score >= 0.52:
            lead = "Ușurință de pornire medie"
        else:
            lead = "Ușurință de pornire scăzută"
        detail = ", ".join(bits[:3]) if bits else "potrivirea personală rămâne criteriul principal"
        return score, f"{lead}: {detail}. Folosit doar pentru departajarea recomandărilor apropiate."

    def _intent_blend(self, rec: Recommendation, best_base_final: float, requested_blend: float) -> float:
        """Taper intent as long-term fit moves away from the best base candidate."""
        requested_blend = max(0.0, float(requested_blend))
        if requested_blend <= 0:
            return 0.0
        gap = max(0.0, float(best_base_final) - float(rec.score.final))
        margin = float(self.INTENT_GOOD_MATCH_MARGIN)
        if gap >= margin:
            return 0.0
        # Full voice for candidates within 0.04 of the best, then taper linearly to zero at 0.16.
        full_voice = 0.04
        if gap <= full_voice:
            proximity = 1.0
        else:
            proximity = 1.0 - (gap - full_voice) / max(0.001, margin - full_voice)
        return requested_blend * clamp(proximity)

    def _startability_blend(self, rec: Recommendation, best_final: float, intent_payload: dict) -> float:
        gap = max(0.0, float(best_final) - float(rec.score.final))
        margin = float(self.STARTABILITY_NEAR_TIE_MARGIN)
        if gap >= margin:
            return 0.0
        proximity = 1.0 - gap / margin
        has_real_intent = bool(intent_payload.get("active")) and float(intent_payload.get("confidence", 0.0) or 0.0) > 0.0
        cap = self.STARTABILITY_EVIDENCE_MAX if has_real_intent else self.STARTABILITY_GENERIC_MAX
        taste_confidence = clamp(float(rec.score.confidence))
        return cap * proximity * (0.60 + 0.40 * taste_confidence)

    def _apply_watch_success(self, recs: list[Recommendation]) -> list[Recommendation]:
        """Apply bounded intent, then allow Startability to break only near-ties."""
        recs = list(recs)
        if not recs:
            return []
        payloads = self.watch_intent.score_many([rec.movie for rec in recs])
        best_base_final = max(float(rec.score.final) for rec in recs)

        # Learned short-horizon intent never gets permission to rescue a clearly weaker long-term
        # match. Its existing sample-size/confidence cap is multiplied by a taste-proximity gate.
        for rec, intent_payload in zip(recs, payloads):
            if not intent_payload.get("active"):
                continue
            intent_score = float(intent_payload.get("score", 0.5) or 0.5)
            requested_blend = float(intent_payload.get("blend_weight", 0.0) or 0.0)
            blend = self._intent_blend(rec, best_base_final, requested_blend)
            if blend <= 0:
                continue
            old_final = float(rec.score.final)
            rec.score.final = clamp((1.0 - blend) * old_final + blend * intent_score)
            reason = str(intent_payload.get("reason") or "")
            rec.score.contributions.insert(
                0,
                (
                    "Intenție de vizionare acum",
                    blend * (intent_score - 0.5) * 100.0,
                    reason,
                ),
            )
            if reason:
                rec.score.personal_reason = reason + " " + (rec.score.personal_reason or "")

        best_pre_start = max(float(rec.score.final) for rec in recs)
        adjusted: list[Recommendation] = []
        for rec, intent_payload in zip(recs, payloads):
            startability, reason = self._startability(rec, intent_payload)
            rec.score.startability = startability
            start_blend = self._startability_blend(rec, best_pre_start, intent_payload)
            if start_blend > 0.001:
                old_final = float(rec.score.final)
                rec.score.final = clamp((1.0 - start_blend) * old_final + start_blend * startability)
                rec.score.contributions.insert(
                    0,
                    (
                        "Startability",
                        start_blend * (startability - 0.5) * 100.0,
                        reason,
                    ),
                )
            adjusted.append(rec)

        adjusted.sort(
            key=lambda r: (r.score.final, r.score.startability, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )
        return adjusted

    # Kept public for focused tests and diagnostics.
    def _apply_startability(self, recs: list[Recommendation]) -> list[Recommendation]:
        recs = list(recs)
        if not recs:
            return []
        payloads = self.watch_intent.score_many([rec.movie for rec in recs])
        best_final = max(float(rec.score.final) for rec in recs)
        adjusted: list[Recommendation] = []
        for rec, payload in zip(recs, payloads):
            startability, reason = self._startability(rec, payload)
            rec.score.startability = startability
            blend = self._startability_blend(rec, best_final, payload)
            if blend > 0.001:
                old_final = float(rec.score.final)
                rec.score.final = clamp((1.0 - blend) * old_final + blend * startability)
                rec.score.contributions.insert(
                    0,
                    ("Startability", blend * (startability - 0.5) * 100.0, reason),
                )
            adjusted.append(rec)
        adjusted.sort(
            key=lambda r: (r.score.final, r.score.startability, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )
        return adjusted

    def _adaptive_rerank(self, recs: list[Recommendation], count: int) -> list[Recommendation]:
        # V13 remains responsible for the personal adaptive model and final diversity. Watch
        # Success and Startability modify only the already-good finalist pool before that step.
        adjusted = self._apply_watch_success(list(recs))
        return FastRecommendationEngineV13._adaptive_rerank(self, adjusted, count)

    def _annotate_final_als(self, selected: list[Recommendation]) -> None:
        start_prefix: dict[int, str] = {}
        for rec in selected:
            for name, _pts, reason in rec.score.contributions:
                if name == "Startability" and reason:
                    start_prefix[int(rec.movie.id)] = str(reason)
                    break
        super()._annotate_final_als(selected)
        for rec in selected:
            prefix = start_prefix.get(int(rec.movie.id))
            if prefix and prefix not in (rec.score.personal_reason or ""):
                rec.score.personal_reason = prefix + " " + (rec.score.personal_reason or "")
