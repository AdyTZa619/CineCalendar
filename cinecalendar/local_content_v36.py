from __future__ import annotations

from collections import defaultdict
import math
import threading

from .util import json_loads, normalize_text


RETRIEVAL_VERSION = "local-content-v3.6.0"


class LocalContentCandidateGeneratorV36:
    """Small metadata-driven retrieval lane for titles outside the MovieLens mapping.

    This lane never scores the final recommendation and never bypasses V16/V17 trust gates. It
    only gives the established scorer a chance to inspect otherwise-missed films that share
    repeated, explicit high-rating evidence with the user. Weak/single-example evidence is ignored.
    """

    MAX_DIRECTORS = 14
    MAX_COUNTRY_GENRES = 10
    DIRECTOR_MIN_POSITIVE = 2
    COUNTRY_GENRE_MIN_POSITIVE = 3

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._cache_token = None
        self._cache: list[int] = []
        self._status = {
            "retrieval_version": RETRIEVAL_VERSION,
            "directors": 0,
            "country_genres": 0,
            "candidates": 0,
            "cache_hit": False,
        }

    def _state_token(self) -> tuple:
        with self.db.connect() as con:
            ratings = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),''),COALESCE(MAX(date_rated),'') FROM ratings"
            ).fetchone()
            feedback = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback"
            ).fetchone()
        return (
            RETRIEVAL_VERSION,
            int(ratings[0] or 0),
            str(ratings[1] or ""),
            str(ratings[2] or ""),
            int(feedback[0] or 0),
            str(feedback[1] or ""),
        )

    @staticmethod
    def _positive_weight(rating: int) -> float:
        return {8: 0.65, 9: 1.0, 10: 1.25}.get(int(rating), 0.0)

    @staticmethod
    def _negative_weight(rating: int) -> float:
        rating = int(rating)
        if rating > 4:
            return 0.0
        return 0.90 + 0.10 * max(0, 4 - rating)

    @staticmethod
    def _public_quality(rating, votes) -> float:
        r = float(rating or 0.0)
        v = max(0, int(votes or 0))
        rating_part = max(0.0, min(1.0, (r - 5.0) / 3.5)) if r else 0.42
        vote_part = min(1.0, math.log1p(v) / math.log1p(150000)) if v else 0.25
        return 0.72 * rating_part + 0.28 * vote_part

    def _evidence(self):
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT r.rating,m.directors_json,m.genres_json,m.countries_json
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   WHERE r.rating>=8 OR r.rating<=4"""
            ).fetchall()

        directors = defaultdict(lambda: [0.0, 0, 0.0, 0])
        country_genres = defaultdict(lambda: [0.0, 0, 0.0, 0])
        for row in rows:
            rating = int(row["rating"])
            pos = self._positive_weight(rating)
            neg = self._negative_weight(rating)
            ds = {normalize_text(str(x)) for x in json_loads(row["directors_json"], []) if str(x).strip()}
            gs = {normalize_text(str(x)) for x in json_loads(row["genres_json"], []) if str(x).strip()}
            cs = {normalize_text(str(x)) for x in json_loads(row["countries_json"], []) if str(x).strip()}
            for director in ds:
                stat = directors[director]
                if pos:
                    stat[0] += pos; stat[1] += 1
                if neg:
                    stat[2] += neg; stat[3] += 1
            for country in cs:
                for genre in gs:
                    stat = country_genres[(country, genre)]
                    if pos:
                        stat[0] += pos; stat[1] += 1
                    if neg:
                        stat[2] += neg; stat[3] += 1

        good_directors = []
        for key, (pos_score, pos_count, neg_score, neg_count) in directors.items():
            if pos_count < self.DIRECTOR_MIN_POSITIVE:
                continue
            if pos_score < max(1.3, neg_score * 1.30):
                continue
            strength = pos_score + 0.16 * pos_count - 0.55 * neg_score - 0.08 * neg_count
            good_directors.append((strength, key))
        good_directors.sort(reverse=True)

        good_pairs = []
        for key, (pos_score, pos_count, neg_score, neg_count) in country_genres.items():
            if pos_count < self.COUNTRY_GENRE_MIN_POSITIVE:
                continue
            if pos_score < max(1.8, neg_score * 1.45):
                continue
            strength = pos_score + 0.12 * pos_count - 0.60 * neg_score - 0.08 * neg_count
            good_pairs.append((strength, key))
        good_pairs.sort(reverse=True)
        return good_directors[: self.MAX_DIRECTORS], good_pairs[: self.MAX_COUNTRY_GENRES]

    @staticmethod
    def _eligible_sql() -> str:
        return """
            LEFT JOIN ratings r ON r.movie_id=m.id
            WHERE r.movie_id IS NULL
              AND m.id NOT IN (SELECT movie_id FROM feedback WHERE kind IN ('not_interested','seen','never_similar'))
              AND lower(COALESCE(m.title_type,'movie')) IN ('movie','short','tvmovie','video','tv movie')
        """

    def _director_candidates(self, evidence, per_director: int = 70):
        scored: dict[int, float] = {}
        with self.db.connect() as con:
            for strength, director in evidence:
                rows = con.execute(
                    """SELECT DISTINCT m.id,m.imdb_rating,m.num_votes
                       FROM movies m JOIN json_each(COALESCE(m.directors_json,'[]')) d
                       LEFT JOIN ratings r ON r.movie_id=m.id
                       WHERE r.movie_id IS NULL
                         AND m.id NOT IN (SELECT movie_id FROM feedback WHERE kind IN ('not_interested','seen','never_similar'))
                         AND lower(COALESCE(m.title_type,'movie')) IN ('movie','short','tvmovie','video','tv movie')
                         AND lower(trim(CAST(d.value AS TEXT)))=?
                       ORDER BY COALESCE(m.imdb_rating,0) DESC,COALESCE(m.num_votes,0) DESC
                       LIMIT ?""",
                    (director, int(per_director)),
                ).fetchall()
                evidence_norm = min(1.0, max(0.0, float(strength) / 5.0))
                for row in rows:
                    mid = int(row["id"])
                    value = 0.64 * evidence_norm + 0.36 * self._public_quality(row["imdb_rating"], row["num_votes"])
                    scored[mid] = max(scored.get(mid, -1.0), value)
        return scored

    def _country_genre_candidates(self, evidence, per_pair: int = 55):
        scored: dict[int, float] = {}
        with self.db.connect() as con:
            for strength, (country, genre) in evidence:
                rows = con.execute(
                    """SELECT DISTINCT m.id,m.imdb_rating,m.num_votes
                       FROM movies m
                       LEFT JOIN ratings r ON r.movie_id=m.id
                       WHERE r.movie_id IS NULL
                         AND m.id NOT IN (SELECT movie_id FROM feedback WHERE kind IN ('not_interested','seen','never_similar'))
                         AND lower(COALESCE(m.title_type,'movie')) IN ('movie','short','tvmovie','video','tv movie')
                         AND EXISTS (SELECT 1 FROM json_each(COALESCE(m.countries_json,'[]')) c
                                     WHERE lower(trim(CAST(c.value AS TEXT)))=?)
                         AND EXISTS (SELECT 1 FROM json_each(COALESCE(m.genres_json,'[]')) g
                                     WHERE lower(trim(CAST(g.value AS TEXT)))=?)
                       ORDER BY COALESCE(m.imdb_rating,0) DESC,COALESCE(m.num_votes,0) DESC
                       LIMIT ?""",
                    (country, genre, int(per_pair)),
                ).fetchall()
                evidence_norm = min(1.0, max(0.0, float(strength) / 7.0))
                for row in rows:
                    mid = int(row["id"])
                    value = 0.58 * evidence_norm + 0.42 * self._public_quality(row["imdb_rating"], row["num_votes"])
                    scored[mid] = max(scored.get(mid, -1.0), value)
        return scored

    def candidates(self, limit: int = 360) -> list[int]:
        limit = max(0, int(limit))
        if limit <= 0:
            return []
        token = self._state_token()
        with self._lock:
            if token == self._cache_token and self._cache:
                self._status["cache_hit"] = True
                return list(self._cache[:limit])

        directors, pairs = self._evidence()
        director_scores = self._director_candidates(directors)
        pair_scores = self._country_genre_candidates(pairs)
        combined = dict(director_scores)
        for mid, value in pair_scores.items():
            if mid in combined:
                combined[mid] = min(1.0, combined[mid] + 0.08 + 0.18 * value)
            else:
                combined[mid] = value
        ordered = [mid for mid, _ in sorted(combined.items(), key=lambda item: item[1], reverse=True)]

        with self._lock:
            self._cache_token = token
            self._cache = list(ordered)
            self._status = {
                "retrieval_version": RETRIEVAL_VERSION,
                "directors": len(directors),
                "country_genres": len(pairs),
                "director_candidates": len(director_scores),
                "country_genre_candidates": len(pair_scores),
                "candidates": len(ordered),
                "cache_hit": False,
            }
        return ordered[:limit]

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)
