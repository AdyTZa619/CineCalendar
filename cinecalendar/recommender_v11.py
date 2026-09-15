from __future__ import annotations

from datetime import date
import time

from .collaborative_als import CollaborativeALSProvider
from .models import Recommendation
from .profile import get_profile
from .recommendation import row_to_movie
from .recommender_v10 import FastRecommendationEngineV10
from .semantic import feature_vector
from .util import clamp, cosine_sparse, utcnow_iso


ENGINE_VERSION = "11.0.0-als"
ALS_WEIGHT = 0.80
CONTENT_WEIGHT = 1.0 - ALS_WEIGHT


class FastRecommendationEngineV11(FastRecommendationEngineV10):
    """Hybrid recommender with established collaborative filtering as the primary ranker.

    MovieLens 32M + implicit Alternating Least Squares supplies the dominant ranking signal
    whenever a title exists in MovieLens. The old hand-built engine is retained as a secondary
    signal and as a fallback for new/unmapped movies. Hard seen/rejected/Romance guards and the
    precision-first calendar gates remain outside ALS and therefore cannot be bypassed by it.
    """

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.collaborative = CollaborativeALSProvider(db)

    def collaborative_status(self) -> dict:
        return self.collaborative.status()

    def _state_token(self) -> tuple:
        base = super()._state_token()
        provider = getattr(self, "collaborative", None)
        if provider is None:
            collaborative_token = "als:fallback"
        else:
            state, version = provider.state_token()
            collaborative_token = f"als:{state}:{version}"
        # A flat string survives JSON persistence unchanged; when the model moves from loading
        # to ready, the token changes and invalidates any temporary fallback decision pool.
        return base + (collaborative_token,)

    def _persistent_key(self, when: date, mode: str) -> str:
        # Never reuse a v9/v10 persisted decision pool after switching to ALS ranking.
        return f"decision_pool_v11:{when.isoformat()}:{mode}"

    @staticmethod
    def _collaborative_reason(mapped_ratings: int, percentile: float) -> str:
        strength = "foarte puternic" if percentile >= .85 else "puternic" if percentile >= .68 else "moderat"
        return (
            f"ALS colaborativ MovieLens 32M: semnal {strength} (percentila {round(percentile * 100)}), "
            f"profil recalculat local din {mapped_ratings:,} ratinguri personale mapate."
        )

    def _annotate_als_explanations(self, selected: list[Recommendation], mapped_scores: dict[str, float], mapped_ratings: int) -> None:
        for rec in selected:
            iid = str(rec.movie.imdb_id or "")
            if iid not in mapped_scores:
                continue
            contributions = self.collaborative.explain(iid, 3)
            if contributions:
                examples = ", ".join(f"«{title}» ({rating}/10)" for title, rating, _value in contributions)
                rec.score.personal_reason = (
                    f"{self._collaborative_reason(mapped_ratings, mapped_scores[iid])} "
                    f"Cele mai importante repere din ratingurile tale: {examples}. "
                    f"Estimarea de notă din metadatele personale rămâne {rec.score.predicted_rating:.1f}/10."
                )
            else:
                rec.score.personal_reason = (
                    f"{self._collaborative_reason(mapped_ratings, mapped_scores[iid])} "
                    f"Estimarea de notă din profilul de conținut este {rec.score.predicted_rating:.1f}/10."
                )

    def _wait_briefly_for_first_model(self, timeout: float = 25.0) -> None:
        """First-run model download is ~20 MB and happens off the UI thread.

        Recommendation workers wait briefly so the first visible answer normally already comes
        from ALS instead of silently showing a v10 fallback while the model is finishing.
        """
        self.collaborative.start_background()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            state = self.collaborative.status().get("state")
            if state in {"ready", "error"}:
                return
            time.sleep(0.10)

    def recommend(self, when: date | None = None, count: int = 3, exclude_ids: set[int] | None = None,
                  record: bool = False, slot: str = "today", candidate_limit: int = 100000,
                  mode: str = "decide", runtime_max: int | None = None, runtime_min: int | None = None):
        when = when or date.today()
        exclude_ids = set(exclude_ids or set())
        profile = get_profile(self.db)
        rated_count = int(profile.get("rated_count", 0) or 0)
        if rated_count < self.MIN_PERSONAL_RATINGS:
            raise RuntimeError(
                "Nu am suficiente ratinguri personale încărcate pentru recomandări. "
                "CineCalendar nu va inventa un scor «pentru tine»."
            )

        with self.db.tx() as con:
            con.execute(
                "UPDATE recommendation_history SET ignored=1 "
                "WHERE ignored=0 AND action IS NULL AND context_date < ?",
                (when.isoformat(),),
            )

        exclude_romance = bool(self.db.get_setting("exclude_romance", True))
        context = self._run_context()
        effective = self._effective_limit(candidate_limit, mode)
        rows = list(self._candidate_rows(when, effective))

        if not self.collaborative.is_ready():
            self._wait_briefly_for_first_model(25.0)
        imdb_ids = [str(row["imdb_id"] or "") for row in rows if row["imdb_id"]]
        collaborative, _raw_collaborative, mapped_ratings = self.collaborative.score_candidates(imdb_ids)
        collaborative_active = bool(collaborative) and mapped_ratings >= 20

        candidates: list[Recommendation] = []
        for row in rows:
            mid = int(row["id"])
            if mid in exclude_ids:
                continue
            movie = row_to_movie(row)
            if runtime_max is not None and movie.runtime_min is not None and movie.runtime_min > runtime_max:
                continue
            if runtime_min is not None and movie.runtime_min is not None and movie.runtime_min < runtime_min:
                continue
            if mode == "short" and movie.runtime_min is not None and movie.runtime_min > 120:
                continue

            score = self._score_one(movie, when, profile, context, exclude_romance, mode)
            if score is None:
                continue
            if score.confidence >= .55 and score.predicted_rating < 5.8:
                continue

            iid = str(movie.imdb_id or "")
            if collaborative_active and iid in collaborative:
                als_score = float(collaborative[iid])
                old_final = float(score.final)
                score.final = clamp(ALS_WEIGHT * als_score + CONTENT_WEIGHT * old_final)
                score.contributions.insert(
                    0,
                    (
                        "ALS colaborativ MovieLens",
                        ALS_WEIGHT * als_score * 100.0,
                        self._collaborative_reason(mapped_ratings, als_score),
                    ),
                )
                score.contributions.append(
                    (
                        "Motor personal de conținut (secundar)",
                        CONTENT_WEIGHT * old_final * 100.0,
                        "Genuri, teme, regizori, calitate, noutate și context; folosit ca semnal secundar/fallback.",
                    )
                )
            candidates.append(Recommendation(movie, score))

        candidates.sort(
            key=lambda r: (r.score.final, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )

        selected: list[Recommendation] = []
        pool = candidates[:max(250, count * 35)]
        while pool and len(selected) < count:
            best = None
            best_value = -1.0
            for rec in pool:
                if not selected:
                    diversity = 1.0
                else:
                    diversity = 1.0 - max(
                        cosine_sparse(feature_vector(rec.movie), feature_vector(chosen.movie))
                        for chosen in selected
                    )
                diversity_weight = .055 if mode == "surprise" else .03
                adjusted = rec.score.final + diversity_weight * (diversity - .5)
                if adjusted > best_value:
                    best_value = adjusted
                    best = (rec, diversity)
            rec, diversity = best
            rec.score.diversity = clamp(diversity)
            rec.score.final = clamp(best_value)
            rec.score.contributions.append(
                ("Diversitate", rec.score.diversity * .03 * 100.0, "Evită o listă de recomandări aproape identice.")
            )
            selected.append(rec)
            pool.remove(rec)

        self._assert_no_blocked_leak(selected)
        if collaborative_active:
            self._annotate_als_explanations(selected, collaborative, mapped_ratings)

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
                    (when.isoformat(), slot, now, len(candidates), len(selected), ENGINE_VERSION),
                )
        return selected
