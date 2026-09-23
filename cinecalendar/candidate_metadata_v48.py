from __future__ import annotations

"""Bounded metadata preflight for the visible recommendation shortlist.

The 12 recommendations the user can act on are completed before nearby off-screen candidates.
The report tells the caller whether factual ranking inputs were added so it can run one final
ranking pass. Providers only fill missing fields; existing catalog data is never overwritten.
"""

from dataclasses import dataclass
import time
from typing import Callable, Iterable

from .models import Movie, Recommendation
from .open_metadata import OpenMovieMetadataProvider
from .tmdb import TmdbProvider


CANDIDATE_METADATA_VERSION = "candidate-metadata-v4.9.4"
MAX_PREFLIGHT_TITLES = 12
PREFLIGHT_POOL_SIZE = 36
PREFLIGHT_VISIBLE_SIZE = 12
PREFLIGHT_BUDGET_SECONDS = 20.0
MAX_CONSECUTIVE_PROVIDER_FAILURES = 2

_RANKING_FIELDS = (
    "genres",
    "directors",
    "countries",
    "semantic_text",
    "runtime",
)

_FIELD_LABELS = {
    "genres": "genuri",
    "directors": "regizor",
    "countries": "țară",
    "semantic_text": "descriere",
    "runtime": "durată",
    "poster": "poster",
}


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


def missing_metadata_labels(movie: Movie) -> list[str]:
    snapshot = metadata_snapshot(movie)
    return [_FIELD_LABELS[field] for field in (*_RANKING_FIELDS, "poster") if not snapshot[field]]


def reset_metadata_cache(db, movie_ids: Iterable[int]) -> int:
    """Forget cached provider answers only for explicitly retried incomplete movies."""
    ids = sorted({int(movie_id) for movie_id in movie_ids if movie_id})
    if not ids or not hasattr(db, "connect"):
        return 0
    marks = ",".join("?" for _ in ids)
    removed = 0
    with db.tx() as con:
        rows = con.execute(
            f"SELECT imdb_id,tmdb_id FROM movies WHERE id IN ({marks})",
            ids,
        ).fetchall()
        for row in rows:
            imdb_id = str(row["imdb_id"] or "").strip()
            tmdb_id = row["tmdb_id"]
            if imdb_id:
                removed += con.execute(
                    "DELETE FROM metadata_cache WHERE provider='tmdb' AND cache_key LIKE ?",
                    (f"/find/{imdb_id}?%",),
                ).rowcount
                removed += con.execute(
                    "DELETE FROM metadata_cache WHERE provider='wikimedia' AND cache_key=?",
                    (f"movie:v2:{imdb_id}",),
                ).rowcount
            if tmdb_id:
                removed += con.execute(
                    "DELETE FROM metadata_cache WHERE provider='tmdb' AND cache_key LIKE ?",
                    (f"/movie/{int(tmdb_id)}?%",),
                ).rowcount
    return int(removed)


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
                "title_results": {},
                "selected_ranks": [],
                "pool_size": len(recs),
                "before": before_coverage,
                "after": before_coverage,
                "io_limit": MAX_PREFLIGHT_TITLES,
            }
        candidates: list[tuple[int, float, int, Recommendation]] = []
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
            # Complete cards that are actually shown before spending a request on a nearby
            # candidate outside the visible list.
            visible_priority = 0 if rank <= PREFLIGHT_VISIBLE_SIZE else 1
            candidates.append((visible_priority, impact, rank, rec))
        candidates.sort(key=lambda item: (item[0], -item[1], item[2]))
        selected = candidates[:bounded_limit]
        targets = [item[3] for item in selected]
        selected_ranks = [item[2] for item in selected]

        tmdb, open_provider = self._providers()
        ranking_added: dict[int, list[str]] = {}
        visual_added = 0
        changed_titles = 0
        failed = 0
        providers_used: set[str] = set()
        started = time.monotonic()
        timed_out = False
        consecutive_provider_failures = 0
        title_results: dict[int, dict] = {}

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
            tmdb_status = "not_configured" if tmdb is None else "pending"
            open_status = "unavailable" if open_provider is None else "pending"
            title_timed_out = False
            if tmdb is not None:
                try:
                    tmdb.enrich_by_imdb(movie)
                    providers_used.add("TMDb")
                    tmdb_status = str(getattr(tmdb, "last_status", "success") or "success")
                    if tmdb_status == "error":
                        had_error = True
                except Exception:
                    tmdb_status = "error"
                    had_error = True
            current = metadata_snapshot(movie)
            remaining = max(0.0, float(budget_seconds) - (time.monotonic() - started))
            if open_provider is not None and not all(current.values()) and remaining >= 2.0:
                try:
                    open_provider.enrich_by_imdb(movie)
                    providers_used.add("Wikidata/Wikipedia")
                    open_status = str(getattr(open_provider, "last_status", "success") or "success")
                    if open_status == "error":
                        had_error = True
                except Exception:
                    open_status = "error"
                    had_error = True
            elif open_provider is not None and not all(current.values()) and remaining < 2.0:
                timed_out = True
                title_timed_out = True
                open_status = "timeout"
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
            missing = [_FIELD_LABELS[field] for field in (*_RANKING_FIELDS, "poster") if not after[field]]
            if not missing:
                result_status = "complete"
                reason = "Metadatele urmărite sunt complete."
            elif before != after or before_content != after_content:
                result_status = "partial"
                reason = "Completat parțial; lipsesc: " + ", ".join(missing) + "."
            elif title_timed_out:
                result_status = "timeout"
                reason = "Verificarea a atins limita de timp; lipsesc: " + ", ".join(missing) + "."
            elif tmdb_status == "not_found" and open_status == "empty":
                result_status = "not_found"
                reason = "Filmul nu a fost găsit în sursele de metadate."
            elif "error" in {tmdb_status, open_status}:
                result_status = "error"
                reason = "O sursă nu a răspuns; lipsesc: " + ", ".join(missing) + "."
            else:
                result_status = "unavailable"
                reason = "Sursele nu oferă momentan: " + ", ".join(missing) + "."
            title_results[int(movie.id)] = {
                "title": str(movie.title or "Film"),
                "status": result_status,
                "reason": reason,
                "missing": missing,
                "tmdb": tmdb_status,
                "fallback": open_status,
            }
            if consecutive_provider_failures >= MAX_CONSECUTIVE_PROVIDER_FAILURES:
                break

        completed_target_ids = set(title_results)
        for rec in targets:
            movie = rec.movie
            if not movie.id or int(movie.id) in completed_target_ids:
                continue
            missing = missing_metadata_labels(movie)
            title_results[int(movie.id)] = {
                "title": str(movie.title or "Film"),
                "status": "timeout" if timed_out else "deferred",
                "reason": (
                    "Verificarea a atins limita de timp; lipsesc: " + ", ".join(missing) + "."
                    if timed_out else
                    "Verificarea a fost amânată după erori consecutive ale surselor."
                ),
                "missing": missing,
                "tmdb": "not_attempted",
                "fallback": "not_attempted",
            }

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
            "title_results": title_results,
            "selected_ranks": selected_ranks,
            "pool_size": len(recs),
            "elapsed_ms": int(round((time.monotonic() - started) * 1000.0)),
            "before": before_coverage,
            "after": coverage_report(recs),
            "io_limit": MAX_PREFLIGHT_TITLES,
        }
