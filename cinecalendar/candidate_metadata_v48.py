from __future__ import annotations

"""Bounded metadata preflight for the visible recommendation shortlist.

The recommendation page already hydrated up to six visible titles after ranking.  V4.8 keeps
that exact I/O ceiling, but reports whether factual ranking inputs were added so the caller can
run one final ranking pass.  Providers only fill missing fields; existing catalog data is never
overwritten here.
"""

from dataclasses import dataclass
import time
from typing import Callable, Iterable

from .models import Movie, Recommendation
from .open_metadata import OpenMovieMetadataProvider
from .tmdb import TmdbProvider


CANDIDATE_METADATA_VERSION = "candidate-metadata-v4.9.1"
MAX_PREFLIGHT_TITLES = 6
PREFLIGHT_POOL_SIZE = 36
PREFLIGHT_VISIBLE_SIZE = 12
PREFLIGHT_BUDGET_SECONDS = 12.0
MAX_CONSECUTIVE_PROVIDER_FAILURES = 2

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

    def _romanian_upgrade_ids(self, recommendations: list[Recommendation]) -> set[int]:
        """Return TMDb overviews that can be safely upgraded without touching other sources."""
        if not self.token.strip() or not hasattr(self.db, "connect"):
            return set()
        ids = [int(rec.movie.id) for rec in recommendations if rec.movie.id is not None]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        try:
            with self.db.connect() as con:
                rows = con.execute(
                    f"""SELECT movie_id FROM metadata_provenance
                        WHERE field='overview' AND provider IN ('tmdb','tmdb-en')
                          AND movie_id IN ({placeholders})""",
                    ids,
                ).fetchall()
            return {int(row["movie_id"]) for row in rows}
        except Exception:
            return set()

    def _providers(self):
        tmdb = None
        if self.token.strip():
            factory = self.tmdb_factory or TmdbProvider
            try:
                try:
                    tmdb = factory(self.db, self.token.strip(), request_timeout=(2.0, 3.0))
                except TypeError:
                    tmdb = factory(self.db, self.token.strip())
            except Exception:
                tmdb = None
        factory = self.open_factory or OpenMovieMetadataProvider
        try:
            try:
                open_provider = factory(
                    self.db,
                    request_timeout=(2.0, 3.0),
                    summary_timeout=(2.0, 3.0),
                )
            except TypeError:
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
        budget_seconds: float = PREFLIGHT_BUDGET_SECONDS,
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
                "timed_out": False,
                "selected_ranks": [],
                "pool_size": len(recs),
                "before": before_coverage,
                "after": before_coverage,
                "io_limit": MAX_PREFLIGHT_TITLES,
            }
        candidates: list[tuple[float, int, Recommendation]] = []
        localization_ids = self._romanian_upgrade_ids(recs)
        for rank, rec in enumerate(recs, start=1):
            movie = rec.movie
            if not movie.id or not movie.imdb_id or int(movie.id) in attempted_ids:
                continue
            snapshot = metadata_snapshot(movie)
            localize = int(movie.id) in localization_ids
            if all(snapshot.values()) and not localize:
                continue
            missing_ranking = sum(not snapshot[field] for field in _RANKING_FIELDS)
            # Facts missing from the visible top 12 can directly change what the user sees.
            # Candidates immediately below the cut also matter because a factual rerank can
            # promote them. Poster-only gaps are useful, but never outrank missing taste facts.
            visible_bonus = 18.0 if rank <= PREFLIGHT_VISIBLE_SIZE else max(0.0, 12.0 - abs(rank - PREFLIGHT_VISIBLE_SIZE))
            impact = missing_ranking * 25.0 + visible_bonus + (2.0 if not snapshot["poster"] else 0.0)
            if localize:
                impact += 4.0
            candidates.append((impact, rank, rec))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        selected = candidates[:bounded_limit]
        targets = [item[2] for item in selected]
        selected_ranks = [item[1] for item in selected]

        tmdb, open_provider = self._providers()
        ranking_added: dict[int, list[str]] = {}
        visual_added = 0
        changed_titles = 0
        failed = 0
        providers_used: set[str] = set()
        started = time.monotonic()
        timed_out = False
        consecutive_provider_failures = 0

        for index, rec in enumerate(targets, start=1):
            if time.monotonic() - started >= max(0.1, float(budget_seconds)):
                timed_out = True
                break
            if progress:
                progress(f"Verific datele recomandărilor… {index}/{len(targets)}")
            movie = rec.movie
            attempted_ids.add(int(movie.id))
            before = metadata_snapshot(movie)
            before_content = (movie.overview, movie.poster_url, movie.runtime_min)
            # With no usable provider this title was attempted but could not be checked.
            # A missing optional TMDb token alone is not an error because Wikimedia is the
            # public fallback used in that configuration.
            had_error = tmdb is None and open_provider is None
            if tmdb is not None:
                try:
                    tmdb.enrich_by_imdb(movie)
                    providers_used.add("TMDb")
                    if getattr(tmdb, "last_status", "") == "error":
                        had_error = True
                except Exception:
                    had_error = True
            current = metadata_snapshot(movie)
            remaining = max(0.0, float(budget_seconds) - (time.monotonic() - started))
            if open_provider is not None and not all(current.values()) and remaining >= 2.0:
                try:
                    open_provider.enrich_by_imdb(movie)
                    providers_used.add("Wikidata/Wikipedia")
                    if getattr(open_provider, "last_status", "") == "error":
                        had_error = True
                except Exception:
                    had_error = True
            elif open_provider is not None and not all(current.values()) and remaining < 2.0:
                timed_out = True
            after = metadata_snapshot(movie)
            after_content = (movie.overview, movie.poster_url, movie.runtime_min)
            if had_error and before == after:
                # One unavailable public result must not block the recommendation page.
                failed += 1
                consecutive_provider_failures += 1
            else:
                consecutive_provider_failures = 0
            added = ranking_fields_added(before, after)
            if added:
                ranking_added[int(movie.id)] = added
            if not before["poster"] and after["poster"]:
                visual_added += 1
            if before != after or before_content != after_content:
                changed_titles += 1
            if consecutive_provider_failures >= MAX_CONSECUTIVE_PROVIDER_FAILURES:
                break

        attempted = len(attempted_ids.intersection({int(rec.movie.id) for rec in targets}))
        if attempted and failed == attempted:
            state = "failed"
        elif failed or timed_out or attempted < len(targets):
            state = "partial"
        else:
            state = "completed"

        return {
            "version": CANDIDATE_METADATA_VERSION,
            "state": state,
            "attempted": attempted,
            "changed_titles": changed_titles,
            "failed": failed,
            "ranking_fields_added": ranking_added,
            "ranking_change": bool(ranking_added),
            "visual_added": visual_added,
            "providers": sorted(providers_used),
            "timed_out": timed_out,
            "selected_ranks": selected_ranks,
            "pool_size": len(recs),
            "elapsed_ms": int(round((time.monotonic() - started) * 1000.0)),
            "before": before_coverage,
            "after": coverage_report(recs),
            "io_limit": MAX_PREFLIGHT_TITLES,
        }
