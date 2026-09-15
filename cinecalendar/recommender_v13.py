from __future__ import annotations

from datetime import date
import threading

from .adaptive_preferences import AdaptivePreferenceLearner
from .models import Recommendation
from .recommender_v12 import FastRecommendationEngineV12
from .semantic import feature_vector
from .util import clamp, cosine_sparse, utcnow_iso


ENGINE_VERSION = "13.1.0-adaptive-nonblocking"


class FastRecommendationEngineV13(FastRecommendationEngineV12):
    """V12 + a local model that continuously learns this user's real preference drift.

    V12 supplies robust candidates using globally calibrated MovieLens ALS and the established
    content/profile engine. V13 then reranks a wider finalist pool using a model trained only on
    the user's own 1-10 ratings and explicit feedback. Expensive model warmup is never allowed to
    block the recommendation worker: until ALS/adaptive warmup finishes, the established content
    engine returns a valid fallback and the stronger models become active on the next refresh.
    """

    ADAPTIVE_POOL_MIN = 60
    ADAPTIVE_POOL_MAX = 120

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.adaptive = AdaptivePreferenceLearner(db)
        self._adaptive_thread: threading.Thread | None = None
        self._adaptive_thread_lock = threading.Lock()

    def _state_token(self) -> tuple:
        # Base token already changes when ratings/feedback change. ENGINE_VERSION guarantees old
        # persisted pools cannot survive the introduction of the adaptive reranker.
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when: date, mode: str) -> str:
        return f"decision_pool_v13_1:{when.isoformat()}:{mode}"

    def _wait_briefly_for_first_model(self, timeout: float = 25.0) -> None:
        """V13 never blocks the UI/recommendation worker waiting for ALS.

        V11/V12 call this hook before collaborative scoring.  The old implementation could wait
        up to 25 seconds on first use.  Starting the loader is enough because score_candidates()
        already fails closed to the content engine while ALS is not ready.
        """
        self.collaborative.start_background()

    def _adaptive_ready_for_token(self, token) -> bool:
        with self.adaptive._lock:
            return token == self.adaptive._token and self.adaptive._status.get("state") == "ready"

    def _adaptive_is_ready(self) -> bool:
        return self._adaptive_ready_for_token(self.adaptive.state_token())

    def start_adaptive_background(self) -> None:
        """Warm/retrain the personal model off the recommendation path.

        AdaptivePreferenceLearner.status() performs validation + training synchronously by design.
        Running it here in a daemon thread preserves exactly the same model while avoiding a long
        first recommendation on HDDs and after rating/feedback changes.
        """
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

        # The old 2.4.6/2.4.7 path called adaptive.score() here. score() synchronously trains the
        # model the first time it is touched, which meant the Home loading card could sit for a
        # long time with ~2.4k ratings. Never do that on the recommendation worker. Warm it in the
        # background and return V12's already-ranked result until the personal model is ready.
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
