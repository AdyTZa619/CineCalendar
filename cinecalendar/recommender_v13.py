from __future__ import annotations

from datetime import date
import threading

from .adaptive_preferences import AdaptivePreferenceLearner
from .models import Recommendation
from .recommender_v12 import FastRecommendationEngineV12
from .semantic import feature_vector
from .util import clamp, cosine_sparse, utcnow_iso


ENGINE_VERSION = "13.2.0-adaptive-fast-shortlist"


class FastRecommendationEngineV13(FastRecommendationEngineV12):
    """V12 + a local model that continuously learns this user's real preference drift.

    V12 supplies robust candidates using globally calibrated MovieLens ALS and the established
    content/profile engine. V13 reranks a wider internal pool using the user's own 1-10 ratings
    and explicit feedback. Expensive model warmup, diversity and ALS explanations are kept off
    the large internal shortlist and applied only where they improve the final visible results.
    """

    ADAPTIVE_POOL_MIN = 60
    ADAPTIVE_POOL_MAX = 120

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.adaptive = AdaptivePreferenceLearner(db)
        self._adaptive_thread: threading.Thread | None = None
        self._adaptive_thread_lock = threading.Lock()

    def _state_token(self) -> tuple:
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when: date, mode: str) -> str:
        return f"decision_pool_v13_2:{when.isoformat()}:{mode}"

    def _wait_briefly_for_first_model(self, timeout: float = 25.0) -> None:
        """Never block the recommendation worker waiting for ALS."""
        self.collaborative.start_background()

    def _adaptive_ready_for_token(self, token) -> bool:
        with self.adaptive._lock:
            return token == self.adaptive._token and self.adaptive._status.get("state") == "ready"

    def _adaptive_is_ready(self) -> bool:
        return self._adaptive_ready_for_token(self.adaptive.state_token())

    def start_adaptive_background(self) -> None:
        """Warm/retrain the personal model off the recommendation path."""
        token = self.adaptive.state_token()
        if self._adaptive_ready_for_token(token):
            return
        with self._adaptive_thread_lock:
            if self._adaptive_thread is not None and self._adaptive_thread.is_alive():
                return

            def train() -> None:
                try:
                    self.adaptive.status()
                except Exception:
                    # Recommendation quality safely falls back to V12. A later request can retry.
                    return

            self._adaptive_thread = threading.Thread(
                target=train,
                name="CineCalendar-Adaptive",
                daemon=True,
            )
            self._adaptive_thread.start()

    def adaptive_status(self) -> dict:
        # Status screens must not accidentally trigger synchronous training either.
        self.start_adaptive_background()
        with self.adaptive._lock:
            status = dict(self.adaptive._status)
        if not self._adaptive_is_ready() and status.get("state") == "not_trained":
            status["state"] = "warming"
        return status

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

        # First request: return the already-ranked V12 fallback immediately and start training
        # only after that expensive base ranking is done. This avoids HDD/CPU contention at Home.
        if not self._adaptive_is_ready():
            self.start_adaptive_background()
            selected = list(recs[:max(1, int(count))])
            self._assert_no_blocked_leak(selected)
            return selected

        candidates: list[Recommendation] = []
        for rec in recs:
            adaptive = self.adaptive.score(rec.movie)
            if not adaptive.get("active"):
                candidates.append(rec)
                continue

            predicted = float(adaptive["predicted_rating"])
            confidence = float(adaptive["confidence"])
            blend = float(adaptive["blend_weight"])

            if confidence >= .72 and predicted < 5.15:
                continue

            old_final = float(rec.score.final)
            adaptive_score = float(adaptive["score"])
            rec.score.final = clamp((1.0 - blend) * old_final + blend * adaptive_score)

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

        # Diversity is useful only for the final visible handful. Cache vectors once instead of
        # rebuilding them repeatedly inside the MMR loop.
        selected: list[Recommendation] = []
        pool = list(candidates)
        vectors = {rec.movie.id: feature_vector(rec.movie) for rec in pool}
        while pool and len(selected) < max(1, int(count)):
            best_rec = None
            best_value = -1.0
            for rec in pool:
                if not selected:
                    diversity = 1.0
                else:
                    current = vectors.get(rec.movie.id) or feature_vector(rec.movie)
                    diversity = 1.0 - max(
                        cosine_sparse(current, vectors.get(chosen.movie.id) or feature_vector(chosen.movie))
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

    def _annotate_final_als(self, selected: list[Recommendation]) -> None:
        """Explain ALS only for final visible results, never the 60-120 item internal pool."""
        if not selected or not self.collaborative.is_ready():
            return
        imdb_ids = [str(rec.movie.imdb_id or "") for rec in selected if rec.movie.imdb_id]
        mapped_scores, _raw, mapped_ratings = self.collaborative.score_candidates(imdb_ids)
        if not mapped_scores or mapped_ratings < 20:
            return

        adaptive_prefix: dict[int, str] = {}
        for rec in selected:
            for name, _pts, reason in rec.score.contributions:
                if name == "Preferințe adaptive locale" and reason:
                    adaptive_prefix[rec.movie.id] = str(reason)
                    break

        self._annotate_als_explanations(selected, mapped_scores, mapped_ratings)
        for rec in selected:
            prefix = adaptive_prefix.get(rec.movie.id)
            if prefix:
                rec.score.personal_reason = prefix + " " + (rec.score.personal_reason or "")

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
        self._annotate_final_als(selected)

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
        selected = self._adaptive_rerank(list(base), requested)
        self._annotate_final_als(selected)
        return selected
