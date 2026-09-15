from __future__ import annotations

from datetime import date

from .models import Recommendation
from .recommendation import WEIGHTS, romance_policy, row_to_movie
from .recommender_v4 import FastRecommendationEngineV4
from .semantic import feature_vector, popularity_bucket, runtime_bucket
from .util import clamp, cosine_sparse, normalize_text, utcnow_iso


ENGINE_VERSION = "5.0.0"


class FastRecommendationEngineV5(FastRecommendationEngineV4):
    """Two-stage large-catalog recommender.

    Stage 1 cheaply ranks the already-balanced shortlist using profile features that are
    present in IMDb datasets (genres, director, decade, runtime, popularity, semantic tags).
    Stage 2 runs the complete explainable scorer only on the strongest few hundred titles.
    Diversity vectors are built once per finalist instead of repeatedly inside nested loops.
    """

    FINALISTS_NORMAL = 480
    FINALISTS_EXPLORE = 620

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.last_full_score_count = 0
        self.last_pre_rank_count = 0

    @staticmethod
    def _feature_preference(profile: dict, feature: str):
        stat = profile.get("features", {}).get(feature)
        if not stat:
            return None
        return float(stat.get("preference", 0.0) or 0.0), int(stat.get("count", 0) or 0)

    def _cheap_personal_affinity(self, movie, profile: dict) -> tuple[float, float]:
        """Fast approximation used only to choose finalists, never shown as the final score."""
        votes = []

        def add(feature: str, weight: float):
            hit = self._feature_preference(profile, feature)
            if hit is None:
                return
            pref, count = hit
            reliability = min(1.0, count / 10.0)
            votes.append((pref, weight * (.35 + .65 * reliability)))

        genres = [normalize_text(g) for g in movie.genres if normalize_text(g)]
        for g in genres[:5]:
            add(f"genre:{g}", 1.00)

        for director in movie.directors[:4]:
            nd = normalize_text(director)
            if nd:
                add(f"director:{nd}", 1.18)

        if movie.year:
            add(f"decade:{movie.year // 10 * 10}s", .48)
        add(f"runtime:{runtime_bucket(movie.runtime_min)}", .34)
        add(f"popularity:{popularity_bucket(movie.num_votes)}", .22)

        for tag, strength in (movie.semantic or {}).items():
            if strength >= .35:
                add(f"theme:{tag}", .62 * min(1.0, float(strength)))

        if not votes:
            return .50, 0.0
        total_w = sum(w for _, w in votes) or 1.0
        signed = sum(pref * w for pref, w in votes) / total_w
        evidence = min(1.0, total_w / 4.0)
        return clamp(.50 + .50 * signed), evidence

    def _cheap_rank(self, movie, profile: dict, context: dict, when: date, mode: str) -> float:
        affinity, evidence = self._cheap_personal_affinity(movie, profile)
        quality = self._quality(movie)
        novelty = self._novelty(movie, context)
        calendar, _kind, _reason = self._calendar_score_cached(movie, when)
        season, _label = self._season_score_cached(movie, when)
        watch_bonus = .07 if int(movie.id) in context.get("watchlist", set()) else 0.0

        # Personal evidence dominates. Public quality only prevents very weak/noisy titles
        # from occupying the finalist set; it cannot replace the user's taste.
        score = (
            .68 * affinity + .14 * quality + .08 * novelty +
            .04 * calendar + .02 * season + .04 * evidence + watch_bonus
        )
        if mode == "safe":
            score += .05 * quality + .03 * evidence
        elif mode == "surprise":
            score += .07 * novelty - .02 * quality
        elif mode == "calendar":
            score += .12 * calendar + .04 * season
        elif mode == "short":
            if movie.runtime_min and movie.runtime_min <= 105:
                score += .05
        return score

    def _fast_diversity_select(self, candidates: list[Recommendation], count: int, mode: str):
        if not candidates or count <= 0:
            return []
        # Final ranking is already strong. Diversity only needs a compact high-quality pool.
        pool = list(candidates[:max(120, count * 6)])
        vectors = {int(r.movie.id): feature_vector(r.movie) for r in pool}
        selected: list[Recommendation] = []
        selected_ids: list[int] = []
        diversity_weight = .055 if mode == "surprise" else WEIGHTS["diversity"]

        while pool and len(selected) < count:
            best_rec = None
            best_div = 1.0
            best_value = -1.0
            for rec in pool:
                mid = int(rec.movie.id)
                if not selected_ids:
                    div = 1.0
                else:
                    vec = vectors[mid]
                    div = 1.0 - max(cosine_sparse(vec, vectors[sid]) for sid in selected_ids)
                adjusted = rec.score.final + diversity_weight * (div - .5)
                if adjusted > best_value:
                    best_value = adjusted
                    best_rec = rec
                    best_div = div
            if best_rec is None:
                break
            best_rec.score.diversity = clamp(best_div)
            best_rec.score.final = clamp(best_value)
            best_rec.score.contributions.append((
                "Diversitate",
                best_rec.score.diversity * WEIGHTS["diversity"] * 100,
                "Evită recomandări aproape identice fără a sacrifica potrivirea personală.",
            ))
            selected.append(best_rec)
            selected_ids.append(int(best_rec.movie.id))
            pool.remove(best_rec)
        return selected

    def recommend(self, when: date | None = None, count: int = 3, exclude_ids: set[int] | None = None,
                  record: bool = False, slot: str = "today", candidate_limit: int = 100000,
                  mode: str = "decide", runtime_max: int | None = None, runtime_min: int | None = None):
        when = when or date.today()
        exclude_ids = set(exclude_ids or set())
        profile = __import__("cinecalendar.profile", fromlist=["get_profile"]).get_profile(self.db)
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
        rows = self._candidate_rows(when, effective)

        pre_ranked = []
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
            allowed, _penalty, _reason = romance_policy(movie, exclude_romance)
            if not allowed:
                continue
            pre_ranked.append((self._cheap_rank(movie, profile, context, when, mode), movie))

        pre_ranked.sort(key=lambda x: x[0], reverse=True)
        self.last_pre_rank_count = len(pre_ranked)
        finalist_limit = self.FINALISTS_EXPLORE if mode in {"surprise", "calendar"} else self.FINALISTS_NORMAL

        # Keep a small representation tail from the balanced shortlist in addition to the
        # top coarse scores, protecting against an imperfect cheap approximation.
        top_n = max(1, finalist_limit - 60)
        finalists = [m for _score, m in pre_ranked[:top_n]]
        finalist_ids = {int(m.id) for m in finalists}
        if len(pre_ranked) > top_n:
            tail = pre_ranked[top_n:]
            stride = max(1, len(tail) // 60)
            for _score, movie in tail[::stride]:
                if int(movie.id) not in finalist_ids:
                    finalists.append(movie)
                    finalist_ids.add(int(movie.id))
                if len(finalists) >= finalist_limit:
                    break

        full: list[Recommendation] = []
        for movie in finalists:
            score = self._score_one(movie, when, profile, context, exclude_romance, mode)
            if score is None:
                continue
            if score.confidence >= .55 and score.predicted_rating < 5.8:
                continue
            full.append(Recommendation(movie, score))
        self.last_full_score_count = len(finalists)
        full.sort(
            key=lambda r: (r.score.final, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )

        selected = self._fast_diversity_select(full, count, mode)
        self._assert_no_blocked_leak(selected)

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
                    (when.isoformat(), slot, now, len(full), len(selected), ENGINE_VERSION),
                )
        return selected
