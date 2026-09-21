from __future__ import annotations

"""Bounded metadata preflight for the visible recommendation shortlist.

The recommendation page already hydrated up to six visible titles after ranking.  V4.8 keeps
that exact I/O ceiling, but reports whether factual ranking inputs were added so the caller can
run one final ranking pass.  Providers only fill missing fields; existing catalog data is never
overwritten here.
"""

from dataclasses import dataclass
from typing import Callable, Iterable

from .models import Movie, Recommendation
from .open_metadata import OpenMovieMetadataProvider
from .tmdb import TmdbProvider


CANDIDATE_METADATA_VERSION = "candidate-metadata-v4.8.0"
MAX_PREFLIGHT_TITLES = 6

_RANKING_FIELDS = (
    "genres",
    "directors",
    "countries",
    "semantic_text",
    "runtime",
)


def metadata_snapshot(movie: Movie) -> dict[str, bool]:
    """Return only factual availability flags; no quality claim is inferred."""
    return {
        "genres": bool(movie.genres),
        "directors": bool(movie.directors),
        "countries": bool(movie.countries),
        # A semantic vector can be synthesized from a title or genre alone.  Do not call that
        # rich metadata; this indicator is intentionally limited to synopsis/keywords.
        "semantic_text": bool(movie.overview or movie.keywords),
        "runtime": bool(movie.runtime_min),
        "poster": bool(movie.poster_url),
    }


def ranking_completeness(movie: Movie) -> float:
    snapshot = metadata_snapshot(movie)
    return sum(1 for field in _RANKING_FIELDS if snapshot[field]) / len(_RANKING_FIELDS)


def coverage_report(recommendations: Iterable[Recommendation]) -> dict:
    recs = list(recommendations)
    if not recs:
        return {
            "total": 0,
            "ranking_complete": 0,
            "average_percent": 0,
            "poster_complete": 0,
            "missing": {field: 0 for field in (*_RANKING_FIELDS, "poster")},
        }
    snapshots = [metadata_snapshot(rec.movie) for rec in recs]
    complete = sum(all(item[field] for field in _RANKING_FIELDS) for item in snapshots)
    average = sum(sum(item[field] for field in _RANKING_FIELDS) for item in snapshots)
    denominator = len(snapshots) * len(_RANKING_FIELDS)
    return {
        "total": len(recs),
        "ranking_complete": int(complete),
        "average_percent": int(round(100.0 * average / denominator)),
        "poster_complete": sum(item["poster"] for item in snapshots),
        "missing": {
            field: sum(not item[field] for item in snapshots)
            for field in (*_RANKING_FIELDS, "poster")
        },
    }


def ranking_fields_added(before: dict[str, bool], after: dict[str, bool]) -> list[str]:
    return [field for field in _RANKING_FIELDS if not before[field] and after[field]]


@dataclass
class CandidateMetadataPreflight:
    db: object
    token: str = ""
    tmdb_factory: Callable | None = None
    open_factory: Callable | None = None

    def _providers(self):
        tmdb = None
        if self.token.strip():
            factory = self.tmdb_factory or TmdbProvider
            try:
                tmdb = factory(self.db, self.token.strip())
            except Exception:
                tmdb = None
        factory = self.open_factory or OpenMovieMetadataProvider
        try:
            open_provider = factory(self.db)
        except Exception:
            open_provider = None
        return tmdb, open_provider

    def run(
        self,
        recommendations: Iterable[Recommendation],
        *,
        attempted_ids: set[int] | None = None,
        limit: int = MAX_PREFLIGHT_TITLES,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        recs = list(recommendations)
        before_coverage = coverage_report(recs)
        attempted_ids = attempted_ids if attempted_ids is not None else set()
        bounded_limit = max(0, min(int(limit), MAX_PREFLIGHT_TITLES))
        targets: list[Recommendation] = []
        if bounded_limit <= 0:
            return {
                "version": CANDIDATE_METADATA_VERSION,
                "state": "completed",
                "attempted": 0,
                "changed_titles": 0,
                "failed": 0,
                "ranking_fields_added": {},
                "ranking_change": False,
                "visual_added": 0,
                "providers": [],
                "before": before_coverage,
                "after": before_coverage,
                "io_limit": MAX_PREFLIGHT_TITLES,
            }
        for rec in recs:
            movie = rec.movie
            if not movie.id or not movie.imdb_id or int(movie.id) in attempted_ids:
                continue
            snapshot = metadata_snapshot(movie)
            if all(snapshot.values()):
                continue
            targets.append(rec)
            attempted_ids.add(int(movie.id))
            if len(targets) >= bounded_limit:
                break

        tmdb, open_provider = self._providers()
        ranking_added: dict[int, list[str]] = {}
        visual_added = 0
        changed_titles = 0
        failed = 0
        providers_used: set[str] = set()

        for index, rec in enumerate(targets, start=1):
            if progress:
                progress(f"Verific datele recomandărilor… {index}/{len(targets)}")
            movie = rec.movie
            before = metadata_snapshot(movie)
            had_error = False
            if tmdb is not None:
                try:
                    tmdb.enrich_by_imdb(movie)
                    providers_used.add("TMDb")
                except Exception:
                    had_error = True
            current = metadata_snapshot(movie)
            if open_provider is not None and not all(current.values()):
                try:
                    open_provider.enrich_by_imdb(movie)
                    providers_used.add("Wikidata/Wikipedia")
                except Exception:
                    had_error = True
            after = metadata_snapshot(movie)
            if had_error and before == after:
                # One unavailable public result must not block the recommendation page.
                failed += 1
            added = ranking_fields_added(before, after)
            if added:
                ranking_added[int(movie.id)] = added
            if not before["poster"] and after["poster"]:
                visual_added += 1
            if before != after:
                changed_titles += 1

        return {
            "version": CANDIDATE_METADATA_VERSION,
            "state": "completed",
            "attempted": len(targets),
            "changed_titles": changed_titles,
            "failed": failed,
            "ranking_fields_added": ranking_added,
            "ranking_change": bool(ranking_added),
            "visual_added": visual_added,
            "providers": sorted(providers_used),
            "before": before_coverage,
            "after": coverage_report(recs),
            "io_limit": MAX_PREFLIGHT_TITLES,
        }
