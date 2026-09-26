from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
import threading

from .full_catalog_shadow_v414 import FullCatalogCandidateGeneratorV414
from .local_content_v36 import LocalContentCandidateGeneratorV36
from .personal_candidates import PersonalCandidateGenerator


V5_RETRIEVAL_VERSION = "v5-unified-retrieval-alpha1"


@dataclass(frozen=True)
class CandidateEvidenceV5:
    movie_id: int
    sources: tuple[str, ...]
    support_count: int
    fusion_score: float
    best_rank: int


class UnifiedCandidateRetrieverV5:
    """Non-destructive multi-source retrieval for the V5 laboratory pipeline.

    The proven baseline pool is always retained. New candidates are appended under a bounded
    expansion budget and are ordered with weighted reciprocal-rank fusion across independent
    retrieval lanes. This means a new idea can improve recall without deleting a candidate the
    current production engine already knows how to find.
    """

    SOURCE_WEIGHTS = {
        "als": 1.00,
        "favorites": 0.95,
        "local_content": 0.82,
        "full_catalog": 0.72,
    }
    RRF_K = 40.0
    EXTRA_SHARE = 0.18
    EXTRA_MIN = 24
    EXTRA_MAX = 360

    def __init__(self, db, collaborative):
        self.db = db
        self.collaborative = collaborative
        self.personal = PersonalCandidateGenerator(db, collaborative)
        self.local_content = LocalContentCandidateGeneratorV36(db)
        self.full_catalog = FullCatalogCandidateGeneratorV414(db, collaborative)
        self._lock = threading.RLock()
        self._status = {
            "version": V5_RETRIEVAL_VERSION,
            "state": "idle",
            "baseline": 0,
            "extra": 0,
            "total": 0,
        }

    def _map_imdb_ids(self, imdb_ids: list[str]) -> list[int]:
        ordered = [str(value) for value in imdb_ids if str(value)]
        if not ordered:
            return []
        found: dict[str, int] = {}
        with self.db.connect() as con:
            for start in range(0, len(ordered), 700):
                chunk = ordered[start:start + 700]
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"""SELECT m.id,m.imdb_id
                        FROM movies m
                        LEFT JOIN ratings r ON r.movie_id=m.id
                        WHERE m.imdb_id IN ({marks})
                          AND r.movie_id IS NULL
                          AND m.id NOT IN (
                              SELECT movie_id FROM feedback
                              WHERE kind IN ('not_interested','seen','never_similar')
                          )
                          AND lower(COALESCE(m.title_type,'movie'))
                              IN ('movie','short','tvmovie','video','tv movie')""",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    iid = str(row["imdb_id"] or "")
                    if iid and iid not in found:
                        found[iid] = int(row["id"])
        out: list[int] = []
        seen: set[int] = set()
        for iid in ordered:
            movie_id = found.get(iid)
            if movie_id is None or movie_id in seen:
                continue
            seen.add(movie_id)
            out.append(movie_id)
        return out

    @classmethod
    def fuse(
        cls,
        baseline_ids: list[int],
        lanes: dict[str, list[int]],
        *,
        extra_limit: int,
    ) -> tuple[list[int], list[CandidateEvidenceV5]]:
        baseline: list[int] = []
        baseline_seen: set[int] = set()
        for raw in baseline_ids:
            movie_id = int(raw)
            if movie_id <= 0 or movie_id in baseline_seen:
                continue
            baseline_seen.add(movie_id)
            baseline.append(movie_id)

        evidence: dict[int, dict] = defaultdict(
            lambda: {"score": 0.0, "sources": set(), "best_rank": 10**9}
        )
        for source, ids in lanes.items():
            weight = float(cls.SOURCE_WEIGHTS.get(source, 0.0))
            if weight <= 0:
                continue
            seen_source: set[int] = set()
            for rank, raw in enumerate(ids, 1):
                movie_id = int(raw)
                if movie_id <= 0 or movie_id in baseline_seen or movie_id in seen_source:
                    continue
                seen_source.add(movie_id)
                item = evidence[movie_id]
                item["score"] += weight / (cls.RRF_K + rank)
                item["sources"].add(source)
                item["best_rank"] = min(int(item["best_rank"]), int(rank))

        ranked: list[CandidateEvidenceV5] = []
        for movie_id, raw in evidence.items():
            support = len(raw["sources"])
            score = float(raw["score"]) + 0.0045 * max(0, support - 1)
            ranked.append(
                CandidateEvidenceV5(
                    movie_id=int(movie_id),
                    sources=tuple(sorted(raw["sources"])),
                    support_count=support,
                    fusion_score=score,
                    best_rank=int(raw["best_rank"]),
                )
            )
        ranked.sort(
            key=lambda item: (
                item.support_count,
                item.fusion_score,
                -item.best_rank,
            ),
            reverse=True,
        )
        selected = ranked[: max(0, int(extra_limit))]
        return baseline + [item.movie_id for item in selected], selected

    def _available_ids(self, ids: list[int], when) -> list[int]:
        if not ids:
            return []
        metadata = {}
        with self.db.connect() as con:
            for start in range(0, len(ids), 700):
                chunk = [int(x) for x in ids[start:start + 700]]
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"SELECT id,year,COALESCE(release_date,'') AS release_date FROM movies WHERE id IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    metadata[int(row["id"])] = (
                        int(row["year"]) if row["year"] is not None else None,
                        str(row["release_date"] or "")[:10],
                    )
        cutoff = when.isoformat()
        out = []
        for raw in ids:
            mid = int(raw)
            year, release = metadata.get(mid, (None, ""))
            if year is not None and year > when.year:
                continue
            if release and len(release) >= 10 and release > cutoff:
                continue
            out.append(mid)
        return out

    def expand(self, baseline_ids: list[int], *, when=None, extra_share: float | None = None) -> list[int]:
        baseline = [int(value) for value in baseline_ids if int(value) > 0]
        if not baseline:
            return []
        if not self.collaborative.is_ready():
            self.collaborative.start_background()
            with self._lock:
                self._status = {
                    "version": V5_RETRIEVAL_VERSION,
                    "state": "waiting_als",
                    "baseline": len(baseline),
                    "extra": 0,
                    "total": len(baseline),
                }
            return list(baseline)

        share = self.EXTRA_SHARE if extra_share is None else max(0.0, min(0.35, float(extra_share)))
        extra_limit = min(
            self.EXTRA_MAX,
            max(self.EXTRA_MIN, int(math.ceil(len(baseline) * share))),
        )
        personal = self.personal.groups(
            als_limit=max(900, len(baseline)),
            favorite_limit=max(450, int(len(baseline) * 0.60)),
        )
        als_ids = self._map_imdb_ids(list(personal.get("als") or []))
        favorite_ids = self._map_imdb_ids(list(personal.get("favorites") or []))
        local_ids = self.local_content.candidates(max(420, extra_limit * 5))
        full_items = self.full_catalog.candidates(max(420, extra_limit * 5))
        full_ids = [int(item.movie_id) for item in full_items]
        if when is not None:
            als_ids = self._available_ids(als_ids, when)
            favorite_ids = self._available_ids(favorite_ids, when)
            local_ids = self._available_ids(local_ids, when)
            full_ids = self._available_ids(full_ids, when)

        lanes = {
            "als": als_ids,
            "favorites": favorite_ids,
            "local_content": local_ids,
            "full_catalog": full_ids,
        }
        merged, selected = self.fuse(baseline, lanes, extra_limit=extra_limit)
        support_histogram: dict[str, int] = defaultdict(int)
        source_hits: dict[str, int] = defaultdict(int)
        for item in selected:
            support_histogram[str(item.support_count)] += 1
            for source in item.sources:
                source_hits[source] += 1
        with self._lock:
            self._status = {
                "version": V5_RETRIEVAL_VERSION,
                "state": "ready",
                "baseline": len(baseline),
                "extra_budget": extra_limit,
                "extra": len(selected),
                "total": len(merged),
                "support_histogram": dict(support_histogram),
                "selected_source_counts": dict(source_hits),
                "personal": self.personal.status(),
                "local_content": self.local_content.status(),
                "full_catalog": self.full_catalog.status(),
            }
        return merged

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)
