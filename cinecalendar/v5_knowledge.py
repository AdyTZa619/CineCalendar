from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .metadata_doctor import queue_metadata_movie
from .util import json_loads


V5_KNOWLEDGE_VERSION = "v5-knowledge-alpha1"


@dataclass(frozen=True)
class V5KnowledgeReport:
    rated_total: int
    informative_total: int
    semantic_total: int
    country_total: int
    informative_semantic: int
    informative_country: int
    queued_profile: int
    open_profile_jobs: int

    @property
    def semantic_coverage(self) -> float:
        return self.semantic_total / self.rated_total if self.rated_total else 0.0

    @property
    def country_coverage(self) -> float:
        return self.country_total / self.rated_total if self.rated_total else 0.0

    @property
    def informative_semantic_coverage(self) -> float:
        return self.informative_semantic / self.informative_total if self.informative_total else 0.0

    @property
    def informative_country_coverage(self) -> float:
        return self.informative_country / self.informative_total if self.informative_total else 0.0

    @property
    def ready_for_rich_ranker(self) -> bool:
        # Rich V5 learning is not allowed to claim readiness while most positive/negative
        # examples still have no premise/country information.
        return (
            self.informative_total >= 300
            and self.informative_semantic_coverage >= 0.70
            and self.informative_country_coverage >= 0.70
        )

    def as_dict(self) -> dict:
        return {
            "version": V5_KNOWLEDGE_VERSION,
            "rated_total": self.rated_total,
            "informative_total": self.informative_total,
            "semantic_total": self.semantic_total,
            "country_total": self.country_total,
            "informative_semantic": self.informative_semantic,
            "informative_country": self.informative_country,
            "semantic_coverage": round(self.semantic_coverage, 6),
            "country_coverage": round(self.country_coverage, 6),
            "informative_semantic_coverage": round(self.informative_semantic_coverage, 6),
            "informative_country_coverage": round(self.informative_country_coverage, 6),
            "queued_profile": self.queued_profile,
            "open_profile_jobs": self.open_profile_jobs,
            "ready_for_rich_ranker": self.ready_for_rich_ranker,
        }


class V5KnowledgeBase:
    """Build the factual substrate required by V5 before adding more ranking complexity.

    IMDb's local catalog is excellent for identity, genres, directors and runtime but intentionally
    does not contain rich plots/countries for most titles. V5 therefore treats metadata coverage as
    a measurable model input, not a cosmetic poster problem.

    Rated 8-10 and 1-4 titles are highest priority because they define the user's positive and
    negative decision boundary. Metadata Doctor performs the actual bounded network I/O; this class
    only seeds/reprioritizes its persistent queue and reports readiness.
    """

    PROFILE_REASON = "v5_rated_profile"
    FRONTIER_REASON = "v5_candidate_frontier"

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _has_json_values(raw) -> bool:
        values = json_loads(raw, []) or []
        return bool(values)

    @staticmethod
    def _semantic_present(row) -> bool:
        if str(row["overview"] or "").strip():
            return True
        return V5KnowledgeBase._has_json_values(row["keywords_json"])

    @staticmethod
    def _country_present(row) -> bool:
        return V5KnowledgeBase._has_json_values(row["countries_json"])

    @staticmethod
    def _priority(rating: int) -> int:
        rating = int(rating)
        if rating >= 9:
            return 1700 + min(20, rating * 2)
        if rating == 8:
            return 1640
        if rating <= 2:
            return 1680 + (2 - rating) * 5
        if rating <= 4:
            return 1600 + (5 - rating) * 8
        return 1300

    def _rated_rows(self):
        with self.db.connect() as con:
            return con.execute(
                """SELECT m.id,m.overview,m.keywords_json,m.countries_json,r.rating,
                          COALESCE(r.date_rated,r.updated_at,'') AS rating_date
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   WHERE m.imdb_id IS NOT NULL AND m.imdb_id<>''
                   ORDER BY CASE
                       WHEN r.rating>=8 OR r.rating<=4 THEN 0 ELSE 1 END,
                       ABS(r.rating-5.5) DESC,
                       COALESCE(r.date_rated,r.updated_at,'') DESC,
                       r.id DESC"""
            ).fetchall()

    def seed_profile(self, limit: int = 0) -> dict:
        """Queue missing factual taste inputs. Zero means all rated titles.

        Queueing is local SQLite work. Provider I/O remains bounded by Metadata Doctor and may
        continue across app sessions.
        """
        rows = list(self._rated_rows())
        if int(limit) > 0:
            rows = rows[: int(limit)]
        queued = 0
        considered = 0
        for row in rows:
            if self._semantic_present(row) and self._country_present(row):
                continue
            considered += 1
            rating = int(row["rating"])
            queued += int(
                queue_metadata_movie(
                    self.db,
                    int(row["id"]),
                    priority=self._priority(rating),
                    reason=self.PROFILE_REASON,
                )
            )
        return {
            "version": V5_KNOWLEDGE_VERSION,
            "considered_missing": considered,
            "queued_or_reprioritized": queued,
        }

    def queue_frontier(self, movie_ids: Iterable[int], *, limit: int = 240) -> int:
        ids = []
        seen: set[int] = set()
        for raw in movie_ids:
            try:
                movie_id = int(raw)
            except (TypeError, ValueError):
                continue
            if movie_id <= 0 or movie_id in seen:
                continue
            seen.add(movie_id)
            ids.append(movie_id)
            if len(ids) >= max(1, int(limit)):
                break
        return sum(
            int(
                queue_metadata_movie(
                    self.db,
                    movie_id,
                    priority=1450,
                    reason=self.FRONTIER_REASON,
                )
            )
            for movie_id in ids
        )

    def report(self) -> V5KnowledgeReport:
        rows = list(self._rated_rows())
        rated_total = len(rows)
        informative = [row for row in rows if int(row["rating"]) >= 8 or int(row["rating"]) <= 4]
        semantic_total = sum(self._semantic_present(row) for row in rows)
        country_total = sum(self._country_present(row) for row in rows)
        informative_semantic = sum(self._semantic_present(row) for row in informative)
        informative_country = sum(self._country_present(row) for row in informative)
        with self.db.connect() as con:
            queued = int(
                con.execute(
                    "SELECT COUNT(*) FROM metadata_jobs WHERE reason=?",
                    (self.PROFILE_REASON,),
                ).fetchone()[0]
                or 0
            )
            open_jobs = int(
                con.execute(
                    """SELECT COUNT(*) FROM metadata_jobs
                       WHERE reason=? AND status IN ('pending','retry','running')""",
                    (self.PROFILE_REASON,),
                ).fetchone()[0]
                or 0
            )
        return V5KnowledgeReport(
            rated_total=rated_total,
            informative_total=len(informative),
            semantic_total=semantic_total,
            country_total=country_total,
            informative_semantic=informative_semantic,
            informative_country=informative_country,
            queued_profile=queued,
            open_profile_jobs=open_jobs,
        )

    def status(self) -> dict:
        return self.report().as_dict()
