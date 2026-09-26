from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .metadata_doctor import queue_metadata_movie
from .util import json_dumps, json_loads, utcnow_iso


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
                """SELECT m.id,m.overview,m.keywords_json,m.countries_json,m.genres_json,
                          m.directors_json,m.runtime_min,m.poster_url,r.rating,
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
        payloads = []
        now = utcnow_iso()
        for row in rows:
            missing = []
            if not self._has_json_values(row["genres_json"]):
                missing.append("genres")
            if not self._has_json_values(row["directors_json"]):
                missing.append("directors")
            if not self._country_present(row):
                missing.append("countries")
            if not self._semantic_present(row):
                missing.append("overview")
            if row["runtime_min"] is None:
                missing.append("runtime")
            if not str(row["poster_url"] or "").strip():
                missing.append("poster")
            # V5's model-readiness problem is premise/country coverage. Do not enqueue a title
            # merely for a missing poster if all factual ranking inputs are already present.
            if not any(field in missing for field in ("genres","directors","countries","overview","runtime")):
                continue
            rating = int(row["rating"])
            payloads.append(
                (
                    int(row["id"]),
                    self._priority(rating),
                    self.PROFILE_REASON,
                    json_dumps(missing),
                    now,
                    now,
                    now,
                )
            )

        if payloads:
            with self.db.tx() as con:
                con.executemany(
                    """INSERT INTO metadata_jobs(
                         movie_id,priority,reason,status,attempt_count,missing_json,queued_at,
                         next_check_at,last_error,updated_at
                       ) VALUES(?,?,?,'pending',0,?,?,?,'',?)
                       ON CONFLICT(movie_id) DO UPDATE SET
                         priority=MAX(metadata_jobs.priority,excluded.priority),
                         reason=CASE
                           WHEN metadata_jobs.reason='v5_rated_profile' THEN metadata_jobs.reason
                           ELSE excluded.reason END,
                         status=CASE WHEN metadata_jobs.status='complete' THEN 'pending'
                                     ELSE metadata_jobs.status END,
                         missing_json=excluded.missing_json,
                         next_check_at=CASE WHEN metadata_jobs.status='complete'
                                            THEN excluded.next_check_at
                                            ELSE metadata_jobs.next_check_at END,
                         completed_at=NULL,updated_at=excluded.updated_at""",
                    payloads,
                )
        return {
            "version": V5_KNOWLEDGE_VERSION,
            "considered_missing": len(payloads),
            "queued_or_reprioritized": len(payloads),
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
