from __future__ import annotations

from datetime import date

from .models import Recommendation
from .profile import get_profile
from .recommendation import row_to_movie
from .recommender_v11 import (
    ALS_WEIGHT,
    CONTENT_WEIGHT,
    DIVERSITY_SHORTLIST_THRESHOLD,
    FastRecommendationEngineV11,
)
from .romanian_cinema import RomanianCinemaProvider
from .util import clamp, json_loads, normalize_text


ENGINE_VERSION = "12.4.0-fast-shortlist-romanian-language-first"


class FastRecommendationEngineV12(FastRecommendationEngineV11):
    """Calibrated ALS recommender with optional daily genre and a language-first Romanian lane."""

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
        return base + ((str(payload.get("date") or ""), str(payload.get("genre") or "")), ENGINE_VERSION)

    def _candidate_rows(self, when: date, limit: int = 100000):
        genre = self._active_daily_genre(when)
        if not genre:
            return super()._candidate_rows(when, limit)

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
        """Rank only titles verified as Romanian-language Romanian productions.

        Eligibility is separate from ranking. The original language must be Romanian and
        Romania must appear as a country of origin. Once a title qualifies, the same globally
        calibrated ALS + personal content signals used by the main recommender decide whether
        it deserves to be shown.
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
            if not self._catalog_quality_is_trustworthy(movie):
                continue

            # In the dedicated Romanian lane prefer the original-language title when IMDb's
            # primary/display title is an English international title.
            if movie.original_title and normalize_text(movie.original_title) != normalize_text(movie.title):
                movie.title = movie.original_title

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
                    "Film românesc verificat prin limbă",
                    0.0,
                    "Limba originală este româna și România figurează ca țară de origine/coproducție; eligibilitatea nu adaugă puncte la scor.",
                ),
            )

            iid = str(movie.imdb_id or "")
            if collaborative_active and iid in collaborative:
                als_score = float(collaborative[iid])
                if not self._mapped_candidate_is_trustworthy(
                    als_score, float(score.predicted_rating), float(score.confidence)
                ):
                    continue
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
                        "Motor personal de conținut",
                        CONTENT_WEIGHT * old_final * 100.0,
                        "Genuri, teme, regizori, calitate, noutate și context; verificare independentă a potrivirii.",
                    )
                )
            candidates.append(Recommendation(movie, score))

        candidates.sort(
            key=lambda r: (r.score.final, r.score.predicted_rating, r.score.confidence),
            reverse=True,
        )

        # V13 requests a large Romanian pool only to rerank it adaptively. Avoid doing a full
        # quadratic diversity pass and dozens of ALS explanations for that internal shortlist;
        # both are applied after V13 has reduced it to the visible final results.
        selected = self._select_candidates(candidates, count, "decide")
        self._assert_no_blocked_leak(selected)
        if collaborative_active and int(count) <= DIVERSITY_SHORTLIST_THRESHOLD:
            self._annotate_als_explanations(selected, collaborative, mapped_ratings)
        return selected
