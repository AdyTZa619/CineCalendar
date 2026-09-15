from __future__ import annotations

from datetime import date

from .models import Recommendation
from .profile import get_profile
from .recommendation import row_to_movie
from .recommender_v11 import ALS_WEIGHT, CONTENT_WEIGHT, FastRecommendationEngineV11
from .romanian_cinema import RomanianCinemaProvider
from .semantic import feature_vector
from .util import clamp, cosine_sparse, json_loads, normalize_text


ENGINE_VERSION = "12.1.0-als-daily-genre-romanian-cinema"


class FastRecommendationEngineV12(FastRecommendationEngineV11):
    """ALS recommender with optional one-day genre intent and Romanian cinema lane."""

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.romanian_cinema = RomanianCinemaProvider(db)
        self.last_romanian_candidate_count = 0

    def _daily_genre_payload(self) -> dict:
        payload = self.db.get_setting("daily_genre_filter", {})
        return payload if isinstance(payload, dict) else {}

    def _active_daily_genre(self, when: date) -> str:
        payload = self._daily_genre_payload()
        if str(payload.get("date") or "") != when.isoformat():
            return ""
        genre = str(payload.get("genre") or "").strip()
        if not genre or normalize_text(genre) in {"orice", "oricare", "any", "all"}:
            return ""
        return genre

    @staticmethod
    def _row_matches_genre(row, genre: str) -> bool:
        target = normalize_text(genre)
        if not target:
            return True
        genres = json_loads(row["genres_json"], []) or []
        return any(normalize_text(str(value)) == target for value in genres)

    def _state_token(self) -> tuple:
        base = super()._state_token()
        payload = self._daily_genre_payload()
        return base + ((str(payload.get("date") or ""), str(payload.get("genre") or "")),)

    def _candidate_rows(self, when: date, limit: int = 100000):
        genre = self._active_daily_genre(when)
        if not genre:
            return super()._candidate_rows(when, limit)

        # Pull the wider established pool first, then apply explicit one-day intent. This keeps
        # the query HDD-friendly and avoids a full-table JSON scan of the ~260k-title catalog.
        requested = max(int(limit or self.EXPLORE_POOL), self.EXPLORE_POOL)
        rows = list(super()._candidate_rows(when, requested))
        filtered = [row for row in rows if self._row_matches_genre(row, genre)]
        self.last_candidate_count = len(filtered)
        return filtered

    def _romanian_rows(self):
        imdb_ids = sorted(self.romanian_cinema.imdb_ids())
        if not imdb_ids:
            self.last_romanian_candidate_count = 0
            return []

        movie_ids: list[int] = []
        with self.db.connect() as con:
            for start in range(0, len(imdb_ids), 700):
                chunk = imdb_ids[start:start + 700]
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"""SELECT id FROM movies
                        WHERE imdb_id IN ({marks})
                          AND title_type IN ('movie','short','tvMovie','video','Movie','TV Movie','tv movie')""",
                    tuple(chunk),
                ).fetchall()
                movie_ids.extend(int(row["id"]) for row in rows)

        rows = self._hydrate_ids(sorted(set(movie_ids)))
        self.last_romanian_candidate_count = len(rows)
        return rows

    def romanian_cinema_status(self) -> dict:
        status = dict(self.romanian_cinema.status())
        status["eligible_catalog_rows"] = int(self.last_romanian_candidate_count)
        return status

    def recommend_romanian(self, when: date | None = None, count: int = 9):
        """Rank only genuine Romanian productions using the same personal ALS engine.

        Country-of-origin is an eligibility gate, not a score boost. This means a mediocre fit
        does not become a recommendation merely because it is Romanian; once the Romanian-only
        pool is built, the same personal taste signals decide the order.
        """
        when = when or date.today()
        profile = get_profile(self.db)
        rated_count = int(profile.get("rated_count", 0) or 0)
        if rated_count < self.MIN_PERSONAL_RATINGS:
            raise RuntimeError(
                "Nu am suficiente ratinguri personale încărcate pentru recomandări. "
                "CineCalendar nu va inventa un scor «pentru tine»."
            )

        rows = list(self._romanian_rows())
        if not rows:
            return []

        if not self.collaborative.is_ready():
            self._wait_briefly_for_first_model(25.0)
        imdb_ids = [str(row["imdb_id"] or "") for row in rows if row["imdb_id"]]
        collaborative, _raw_collaborative, mapped_ratings = self.collaborative.score_candidates(imdb_ids)
        collaborative_active = bool(collaborative) and mapped_ratings >= 20

        context = self._run_context()
        exclude_romance = bool(self.db.get_setting("exclude_romance", False))
        candidates: list[Recommendation] = []

        for row in rows:
            movie = row_to_movie(row)
            # Wikidata already established Romania as a country of origin. Add it in-memory if
            # the local title has not yet been metadata-enriched so country taste can participate.
            if not any(normalize_text(str(c)) == "romania" for c in (movie.countries or [])):
                movie.countries = list(movie.countries or []) + ["România"]

            score = self._score_one(movie, when, profile, context, exclude_romance, mode="decide")
            if score is None:
                continue
            if score.confidence >= .55 and score.predicted_rating < 5.8:
                continue

            score.contributions.insert(
                0,
                (
                    "Cinema românesc",
                    0.0,
                    "Eligibilitate verificată prin țara de origine România; nu este un bonus artificial de scor.",
                ),
            )

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
                        "Genuri, teme, regizori, calitate, noutate și context; semnal secundar/fallback.",
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
                adjusted = rec.score.final + .03 * (diversity - .5)
                if adjusted > best_value:
                    best_value = adjusted
                    best = (rec, diversity)
            rec, diversity = best
            rec.score.diversity = clamp(diversity)
            rec.score.final = clamp(best_value)
            rec.score.contributions.append(
                ("Diversitate", rec.score.diversity * .03 * 100.0, "Evită o listă de filme românești aproape identice.")
            )
            selected.append(rec)
            pool.remove(rec)

        self._assert_no_blocked_leak(selected)
        if collaborative_active:
            self._annotate_als_explanations(selected, collaborative, mapped_ratings)
        return selected
