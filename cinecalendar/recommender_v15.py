from __future__ import annotations

from datetime import date
import math

from .models import Recommendation
from .recommender_v13 import FastRecommendationEngineV13
from .recommender_v14 import FastRecommendationEngineV14
from .util import clamp
from .watch_success import WatchSuccessIntentLearner


ENGINE_VERSION = "15.0.0-watch-success-startability"


class FastRecommendationEngineV15(FastRecommendationEngineV14):
    """V14 + a bounded Startability layer focused on actually getting to Play.

    Long-term taste remains the gatekeeper. Startability only reranks already-good candidates by
    practical friction: current intent, personal rating/confidence, runtime, premise completeness,
    metadata and public quality evidence. It never changes the estimated 1-10 rating.
    """

    STARTABILITY_BLEND_MAX = 0.18

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        # Replace v1 intent with the Watch Success learner: choosing is weaker than opening playback.
        self.watch_intent = WatchSuccessIntentLearner(db)

    def _state_token(self) -> tuple:
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when: date, mode: str) -> str:
        return f"decision_pool_v15:{when.isoformat()}:{mode}"

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

    def _startability(self, rec: Recommendation) -> tuple[float, str]:
        m, s = rec.movie, rec.score
        taste = clamp((float(s.predicted_rating) - 5.0) / 5.0)
        confidence = clamp(float(s.confidence))
        runtime = self._runtime_score(m.runtime_min)
        premise = self._premise_score(m)
        metadata = self._metadata_score(m)
        quality = self._quality_score(m)

        intent_payload = self.watch_intent.score(m)
        intent = float(intent_payload.get("score", 0.5) or 0.5) if intent_payload.get("active") else 0.5

        score = clamp(
            0.44 * taste
            + 0.14 * confidence
            + 0.14 * intent
            + 0.10 * runtime
            + 0.08 * premise
            + 0.05 * metadata
            + 0.05 * quality
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
            bits.append("destule semnale publice de calitate")
        if intent >= 0.64:
            bits.append("se potrivește cu ce ai ales recent")

        if score >= 0.74:
            lead = "Bun de pornit acum"
        elif score >= 0.62:
            lead = "Ușor de încercat acum"
        else:
            lead = "Potrivire bună, dar cu puțin mai multă fricțiune"
        detail = ", ".join(bits[:3]) if bits else "potrivirea personală rămâne criteriul principal"
        return score, f"{lead}: {detail}."

    def _apply_startability(self, recs: list[Recommendation]) -> list[Recommendation]:
        adjusted: list[Recommendation] = []
        for rec in recs:
            startability, reason = self._startability(rec)
            old_final = float(rec.score.final)
            # Confidence controls only the size of the nudge, never the predicted rating itself.
            blend = min(
                self.STARTABILITY_BLEND_MAX,
                0.10 + 0.08 * clamp(float(rec.score.confidence)),
            )
            rec.score.startability = startability
            rec.score.final = clamp((1.0 - blend) * old_final + blend * startability)
            rec.score.contributions.insert(
                0,
                (
                    "Startability",
                    blend * (startability - 0.5) * 100.0,
                    reason,
                ),
            )
            adjusted.append(rec)

        adjusted.sort(
            key=lambda r: (r.score.final, r.score.startability, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )
        return adjusted

    def _adaptive_rerank(self, recs: list[Recommendation], count: int) -> list[Recommendation]:
        # Run v14 intent once, then Startability on the same finalist pool. Call V13 directly so
        # intent is not applied twice before the adaptive taste model and final diversity pass.
        intent_adjusted = self._apply_watch_intent(list(recs))
        startability_adjusted = self._apply_startability(intent_adjusted)
        return FastRecommendationEngineV13._adaptive_rerank(self, startability_adjusted, count)

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
