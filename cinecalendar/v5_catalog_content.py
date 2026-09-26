from __future__ import annotations

"""V5 full-catalog content retrieval independent of MovieLens identity.

4.14 deliberately searched only titles without an ALS mapping. That was correct for measuring the
coverage gap of ALS, but it is the wrong contract for a unified V5 retriever: having a MovieLens
mapping does not mean ALS ranks the title well. Content must be allowed to rescue a mapped title
that collaborative retrieval places too deep.
"""

import math

from .full_catalog_shadow_v414 import (
    FullCatalogCandidateGeneratorV414,
    RetrievedCandidate,
)
from .recommendation import row_to_movie
from .semantic import feature_vector


V5_CATALOG_CONTENT_VERSION = "v5-catalog-content-alpha1"


class V5CatalogContentRetriever(FullCatalogCandidateGeneratorV414):
    """Score both ALS-mapped and unmapped titles with the learned content vocabulary."""

    def _score_rows(self, rows, profile: dict) -> list[RetrievedCandidate]:
        stats = dict(profile.get("features") or {})
        out: list[RetrievedCandidate] = []
        for row in rows:
            # V5 intentionally has no collaborative.has_mapping() exclusion here.
            movie = row_to_movie(row)
            vector = feature_vector(movie)
            weighted = weight_total = 0.0
            matches = 0
            for feature, feature_weight in vector.items():
                evidence = stats.get(feature)
                if not evidence:
                    continue
                preference = float(evidence.get("preference", 0.0) or 0.0)
                count = int(evidence.get("count", 0) or 0)
                reliability = min(1.0, count / 6.0)
                weight = float(feature_weight) * (0.55 + 0.45 * reliability)
                weighted += preference * weight
                weight_total += weight
                matches += 1
            if matches < 2 or weight_total <= 0:
                continue
            personal = max(-1.0, min(1.0, weighted / weight_total))
            if personal <= 0.02:
                continue
            completeness = sum(
                bool(value)
                for value in (
                    movie.genres,
                    movie.directors,
                    movie.countries,
                    movie.overview or movie.keywords,
                    movie.runtime_min,
                )
            ) / 5.0
            score = (
                0.82 * ((personal + 1.0) / 2.0)
                + 0.12 * self._public_quality(movie)
                + 0.06 * completeness
            )
            out.append(
                RetrievedCandidate(
                    int(movie.id),
                    min(1.0, score),
                    personal,
                    matches,
                )
            )
        out.sort(
            key=lambda item: (
                item.score,
                item.personal_score,
                item.matched_features,
            ),
            reverse=True,
        )
        return out

    def candidates(self, limit: int = 250) -> list[RetrievedCandidate]:
        result = super().candidates(limit)
        with self._lock:
            self._status["version"] = V5_CATALOG_CONTENT_VERSION
            self._status["catalog_candidates"] = int(
                self._status.get("candidate_count", len(result)) or len(result)
            )
            self._status.pop("non_als_candidates", None)
        return result
