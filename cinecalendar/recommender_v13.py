from __future__ import annotations

from datetime import date

from .adaptive_preferences import AdaptivePreferenceLearner
from .models import Recommendation
from .recommender_v12 import FastRecommendationEngineV12
from .semantic import feature_vector
from .util import clamp, cosine_sparse, utcnow_iso


ENGINE_VERSION = "13.0.0-adaptive-personal"


class FastRecommendationEngineV13(FastRecommendationEngineV12):
    """V12 + a local model that continuously learns this user's real preference drift.

    V12 supplies robust candidates using globally calibrated MovieLens ALS and the established
    content/profile engine. V13 then reranks a wider finalist pool using a model trained only on
    the user's own 1-10 ratings and explicit feedback. This keeps collaborative discovery while
    allowing thousands of personal ratings to have substantially more influence on the final 3.
    """

    ADAPTIVE_POOL_MIN = 60
    ADAPTIVE_POOL_MAX = 120

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.adaptive = AdaptivePreferenceLearner(db)

    def _state_token(self) -> tuple:
        # Base token already changes when ratings/feedback change. ENGINE_VERSION guarantees old
        # persisted pools cannot survive the introduction of the adaptive reranker.
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when: date, mode: str) -> str:
        return f"decision_pool_v13:{when.isoformat()}:{mode}"

    def adaptive_status(self) -> dict:
        return self.adaptive.status()

    @staticmethod
    def _human_adaptive_feature(token: str) -> str:
        if token.startswith("pair:"):
            raw = token[5:].replace("|", " + ")
        else:
            raw = token
        labels = {
            "genre:": "gen ",
            "director:": "regizor ",
            "theme:": "temă ",
            "country:": "cinematografie ",
            "decade:": "deceniu ",
            "runtime:": "durată ",
            "popularity:": "popularitate ",
        }
        for prefix, label in labels.items():
            raw = raw.replace(prefix, label)
        return raw.replace("_", " ")

    def _adaptive_rerank(self, recs: list[Recommendation], count: int) -> list[Recommendation]:
        if not recs:
            return []

        candidates: list[Recommendation] = []
        for rec in recs:
            adaptive = self.adaptive.score(rec.movie)
            if not adaptive.get("active"):
                candidates.append(rec)
                continue

            predicted = float(adaptive["predicted_rating"])
            confidence = float(adaptive["confidence"])
            blend = float(adaptive["blend_weight"])

            # A high-confidence prediction that this user would rate poorly is a stronger reason
            # to suppress the title than generic popularity is a reason to keep it.
            if confidence >= .72 and predicted < 5.15:
                continue

            old_final = float(rec.score.final)
            adaptive_score = float(adaptive["score"])
            rec.score.final = clamp((1.0 - blend) * old_final + blend * adaptive_score)

            # The displayed predicted rating should also move toward the personal learner, but
            # never pretend it is certain. Low-confidence local estimates barely move the badge.
            rating_mix = min(.72, blend + .20 * confidence)
            rec.score.predicted_rating = max(
                1.0,
                min(
                    10.0,
                    (1.0 - rating_mix) * float(rec.score.predicted_rating) + rating_mix * predicted,
                ),
            )
            rec.score.confidence = clamp(
                1.0 - (1.0 - float(rec.score.confidence)) * (1.0 - .62 * confidence)
            )

            reasons = list(adaptive.get("reasons") or [])
            readable = [self._human_adaptive_feature(str(item.get("feature") or "")) for item in reasons[:3]]
            readable = [x for x in readable if x]
            reason_text = (
                f"Modelul local, învățat din ratingurile și feedbackul tău, estimează {predicted:.1f}/10"
                + (f"; repere principale: {', '.join(readable)}." if readable else ".")
            )
            rec.score.contributions.insert(
                0,
                (
                    "Preferințe adaptive locale",
                    blend * adaptive_score * 100.0,
                    reason_text,
                ),
            )
            rec.score.personal_reason = reason_text + " " + (rec.score.personal_reason or "")
            candidates.append(rec)

        candidates.sort(
            key=lambda r: (r.score.final, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )

        # Re-apply a light diversity pass after adaptive reranking. The personal learner may
        # correctly discover a strong niche, but the final row should not become three clones.
        selected: list[Recommendation] = []
        pool = list(candidates)
        while pool and len(selected) < max(1, int(count)):
            best_rec = None
            best_value = -1.0
            for rec in pool:
                if not selected:
                    diversity = 1.0
                else:
                    diversity = 1.0 - max(
                        cosine_sparse(feature_vector(rec.movie), feature_vector(chosen.movie))
                        for chosen in selected
                    )
                value = float(rec.score.final) + .018 * (diversity - .5)
                if value > best_value:
                    best_value = value
                    best_rec = (rec, diversity)
            if best_rec is None:
                break
            rec, diversity = best_rec
            rec.score.diversity = clamp(diversity)
            rec.score.final = clamp(best_value)
            selected.append(rec)
            pool.remove(rec)

        self._assert_no_blocked_leak(selected)
        return selected

    def recommend(self, when: date | None = None, count: int = 3, exclude_ids: set[int] | None = None,
                  record: bool = False, slot: str = "today", candidate_limit: int = 100000,
                  mode: str = "decide", runtime_max: int | None = None, runtime_min: int | None = None):
        when = when or date.today()
        requested = max(1, int(count))
        expanded = min(
            self.ADAPTIVE_POOL_MAX,
            max(self.ADAPTIVE_POOL_MIN, requested * 12),
        )
        base = super().recommend(
            when=when,
            count=expanded,
            exclude_ids=exclude_ids,
            record=False,
            slot=slot,
            candidate_limit=candidate_limit,
            mode=mode,
            runtime_max=runtime_max,
            runtime_min=runtime_min,
        )
        selected = self._adaptive_rerank(list(base), requested)

        if record and selected:
            now = utcnow_iso()
            with self.db.tx() as con:
                for rec in selected:
                    con.execute(
                        "INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score) "
                        "VALUES(?,?,?,?,?)",
                        (rec.movie.id, now, when.isoformat(), slot, rec.score.final),
                    )
                con.execute(
                    "INSERT INTO recommendation_runs(context_date,slot,generated_at,candidate_count,result_count,engine_version) "
                    "VALUES(?,?,?,?,?,?)",
                    (when.isoformat(), slot, now, len(base), len(selected), ENGINE_VERSION),
                )
        return selected

    def recommend_romanian(self, when: date | None = None, count: int = 9):
        when = when or date.today()
        requested = max(1, int(count))
        expanded = min(self.ADAPTIVE_POOL_MAX, max(40, requested * 8))
        base = super().recommend_romanian(when=when, count=expanded)
        return self._adaptive_rerank(list(base), requested)
