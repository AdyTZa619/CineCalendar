from __future__ import annotations

from datetime import date

from .models import Recommendation
from .recommender_v13 import FastRecommendationEngineV13
from .util import clamp
from .watch_intent import WatchIntentLearner


ENGINE_VERSION = "14.0.0-watch-intent"


class FastRecommendationEngineV14(FastRecommendationEngineV13):
    """V13 + short-horizon watch intent, kept separate from long-term taste.

    V13 answers "which films fit this user?". V14 adds a deliberately bounded question before
    the adaptive reranker: "among already-good candidates, which ones look most likely to be
    chosen now?". It learns from explicit choose/skip/watchlist/feedback actions and weakly from
    recent IMDb ratings, with fast decay for session-like signals.

    Intent never changes the predicted 1-10 rating. It only nudges ordering, and its maximum voice
    grows gradually from 0% to 28% as real interaction evidence accumulates.
    """

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.watch_intent = WatchIntentLearner(db)

    def _state_token(self) -> tuple:
        base = super()._state_token()
        learner = getattr(self, "watch_intent", None)
        if learner is None:
            intent_token = ("watch-intent:init",)
        else:
            intent_token = learner.state_token()
        return base + (intent_token, ENGINE_VERSION)

    def _persistent_key(self, when: date, mode: str) -> str:
        return f"decision_pool_v14:{when.isoformat()}:{mode}"

    def watch_intent_status(self) -> dict:
        return self.watch_intent.status()

    def _apply_watch_intent(self, recs: list[Recommendation]) -> list[Recommendation]:
        if not recs:
            return []
        status = self.watch_intent.status()
        if not status.get("active"):
            return list(recs)

        adjusted: list[Recommendation] = []
        for rec in recs:
            intent = self.watch_intent.score(rec.movie)
            if not intent.get("active"):
                adjusted.append(rec)
                continue

            intent_score = float(intent.get("score", 0.5) or 0.5)
            confidence = float(intent.get("confidence", 0.0) or 0.0)
            blend = float(intent.get("blend_weight", 0.0) or 0.0)
            if blend <= 0:
                adjusted.append(rec)
                continue

            old_final = float(rec.score.final)
            rec.score.final = clamp((1.0 - blend) * old_final + blend * intent_score)
            reason = str(intent.get("reason") or "")
            rec.score.contributions.insert(
                0,
                (
                    "Intenție de vizionare acum",
                    blend * (intent_score - 0.5) * 100.0,
                    reason,
                ),
            )
            # Keep taste confidence distinct. Intent confidence describes short-horizon behaviour,
            # so it must not inflate the displayed confidence of the predicted 1-10 rating.
            if reason:
                rec.score.personal_reason = reason + " " + (rec.score.personal_reason or "")
            adjusted.append(rec)

        adjusted.sort(
            key=lambda r: (r.score.final, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )
        return adjusted

    def _adaptive_rerank(self, recs: list[Recommendation], count: int) -> list[Recommendation]:
        # Apply the cheap, cached intent nudge to the whole 60-120 candidate finalist pool first.
        # Then V13's proven adaptive model and final diversity pass remain in full control.
        intent_adjusted = self._apply_watch_intent(list(recs))
        return super()._adaptive_rerank(intent_adjusted, count)

    def _annotate_final_als(self, selected: list[Recommendation]) -> None:
        # V13 preserves its adaptive explanation around ALS explain(). Preserve watch-intent text
        # as well so the user can see that "likely to choose now" is separate from taste/rating.
        intent_prefix: dict[int, str] = {}
        for rec in selected:
            for name, _pts, reason in rec.score.contributions:
                if name == "Intenție de vizionare acum" and reason:
                    intent_prefix[int(rec.movie.id)] = str(reason)
                    break
        super()._annotate_final_als(selected)
        for rec in selected:
            prefix = intent_prefix.get(int(rec.movie.id))
            if prefix and not (rec.score.personal_reason or "").startswith(prefix):
                rec.score.personal_reason = prefix + " " + (rec.score.personal_reason or "")
