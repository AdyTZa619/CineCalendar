from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import requests

from .imdb_sync import backfill_public_rating_metadata, sync_public_ratings
from .models import Movie
from .open_metadata import OpenMovieMetadataProvider
from .profile import build_profile
from .tmdb import enrich_library
from .util import json_loads


@dataclass(frozen=True)
class LibraryHealth:
    total: int
    complete: int
    incomplete: int
    missing_genres: int
    missing_directors: int
    missing_runtime: int
    missing_imdb_rating: int
    missing_poster: int
    missing_countries: int

    @property
    def completion_percent(self) -> float:
        return (100.0 * self.complete / self.total) if self.total else 100.0


@dataclass(frozen=True)
class LibraryRepairResult:
    before: LibraryHealth
    after: LibraryHealth
    new_ratings: int = 0
    changed_ratings: int = 0
    duplicates_repaired: int = 0
    imdb_enriched: int = 0
    tmdb_enriched: int = 0
    wikimedia_enriched: int = 0
    wikimedia_failed: int = 0

    @property
    def fixed_titles(self) -> int:
        return max(0, self.before.incomplete - self.after.incomplete)


def rated_library_health(db) -> LibraryHealth:
    with db.connect() as con:
        row = con.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN TRIM(COALESCE(m.genres_json,'')) IN ('','[]') THEN 1 ELSE 0 END) AS missing_genres,
              SUM(CASE WHEN TRIM(COALESCE(m.directors_json,'')) IN ('','[]') THEN 1 ELSE 0 END) AS missing_directors,
              SUM(CASE WHEN m.runtime_min IS NULL THEN 1 ELSE 0 END) AS missing_runtime,
              SUM(CASE WHEN m.imdb_rating IS NULL THEN 1 ELSE 0 END) AS missing_imdb_rating,
              SUM(CASE WHEN m.poster_url IS NULL OR TRIM(m.poster_url)='' THEN 1 ELSE 0 END) AS missing_poster,
              SUM(CASE WHEN TRIM(COALESCE(m.countries_json,'')) IN ('','[]') THEN 1 ELSE 0 END) AS missing_countries,
              SUM(CASE WHEN
                    TRIM(COALESCE(m.genres_json,'')) NOT IN ('','[]')
                AND TRIM(COALESCE(m.directors_json,'')) NOT IN ('','[]')
                AND m.runtime_min IS NOT NULL
                AND m.imdb_rating IS NOT NULL
                THEN 1 ELSE 0 END) AS complete
            FROM ratings r
            JOIN movies m ON m.id=r.movie_id
            """
        ).fetchone()
    total = int(row["total"] or 0)
    complete = int(row["complete"] or 0)
    return LibraryHealth(
        total=total,
        complete=complete,
        incomplete=max(0, total - complete),
        missing_genres=int(row["missing_genres"] or 0),
        missing_directors=int(row["missing_directors"] or 0),
        missing_runtime=int(row["missing_runtime"] or 0),
        missing_imdb_rating=int(row["missing_imdb_rating"] or 0),
        missing_poster=int(row["missing_poster"] or 0),
        missing_countries=int(row["missing_countries"] or 0),
    )


def _missing_rows(db, limit: int):
    with db.connect() as con:
        return con.execute(
            """
            SELECT m.*
            FROM ratings r
            JOIN movies m ON m.id=r.movie_id
            WHERE m.imdb_id IS NOT NULL AND TRIM(m.imdb_id)!=''
              AND (
                   TRIM(COALESCE(m.genres_json,'')) IN ('','[]')
                OR TRIM(COALESCE(m.directors_json,'')) IN ('','[]')
                OR m.runtime_min IS NULL
                OR m.imdb_rating IS NULL
                OR m.poster_url IS NULL OR TRIM(m.poster_url)=''
                OR TRIM(COALESCE(m.countries_json,'')) IN ('','[]')
              )
            ORDER BY COALESCE(r.date_rated,'') DESC,r.id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()


def _movie_from_row(row) -> Movie:
    return Movie(
        id=int(row["id"]),
        imdb_id=str(row["imdb_id"] or ""),
        title=str(row["title"] or ""),
        original_title=str(row["original_title"] or row["title"] or ""),
        year=int(row["year"]) if row["year"] is not None else None,
        title_type=str(row["title_type"] or "Movie"),
        runtime_min=int(row["runtime_min"]) if row["runtime_min"] is not None else None,
        genres=json_loads(row["genres_json"], []) or [],
        directors=json_loads(row["directors_json"], []) or [],
        countries=json_loads(row["countries_json"], []) or [],
        overview=str(row["overview"] or ""),
        keywords=json_loads(row["keywords_json"], []) or [],
        imdb_rating=float(row["imdb_rating"]) if row["imdb_rating"] is not None else None,
        num_votes=int(row["num_votes"]) if row["num_votes"] is not None else None,
        release_date=row["release_date"],
        poster_url=row["poster_url"],
        source=str(row["source"] or ""),
        semantic=json_loads(row["semantic_json"], {}) or {},
    )


def repair_rated_library(
    db,
    *,
    profile_url: str = "",
    baseline_date: str | None = "2026-09-05",
    limit: int = 5000,
    progress: Callable[[str], None] | None = None,
) -> LibraryRepairResult:
    progress = progress or (lambda _message: None)
    before = rated_library_health(db)
    new_ratings = changed_ratings = duplicates = 0

    if profile_url.strip():
        progress("Sincronizez ratingurile și repar identitățile duplicate…")
        sync = sync_public_ratings(
            db,
            profile_url.strip(),
            baseline_date=baseline_date,
        )
        new_ratings = len(sync.new_ratings)
        changed_ratings = len(sync.changed_ratings)
        duplicates = len(sync.reconciled_duplicates)

    progress("Completez metadatele din IMDb…")
    imdb_enriched = backfill_public_rating_metadata(db, limit=limit)

    tmdb_enriched = 0
    token = str(db.get_setting("tmdb_token", "") or "").strip()
    if token:
        progress("Completez câmpurile rămase prin TMDb…")
        try:
            tmdb_result = enrich_library(
                db,
                token,
                limit=limit,
                progress=progress,
                rated_only=True,
            )
            tmdb_enriched = int(tmdb_result.get("enriched", 0) or 0)
        except (requests.RequestException, RuntimeError, ValueError):
            tmdb_enriched = 0

    progress("Aplic fallback Wikidata/Wikipedia pentru câmpurile rămase…")
    provider = OpenMovieMetadataProvider(db)
    wikimedia_enriched = 0
    wikimedia_failed = 0
    rows = _missing_rows(db, limit)
    for index, row in enumerate(rows, 1):
        movie = _movie_from_row(row)
        before_fields = (
            tuple(movie.genres),
            tuple(movie.directors),
            tuple(movie.countries),
            movie.runtime_min,
            movie.poster_url,
        )
        try:
            provider.enrich_by_imdb(movie)
            after_fields = (
                tuple(movie.genres),
                tuple(movie.directors),
                tuple(movie.countries),
                movie.runtime_min,
                movie.poster_url,
            )
            if after_fields != before_fields:
                wikimedia_enriched += 1
        except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError):
            wikimedia_failed += 1
        if index == 1 or index % 25 == 0 or index == len(rows):
            progress(
                f"Fallback deschis: {index}/{len(rows)} • completate {wikimedia_enriched} • erori {wikimedia_failed}"
            )

    progress("Recalculez statisticile și profilul de gust…")
    build_profile(db)
    after = rated_library_health(db)
    return LibraryRepairResult(
        before=before,
        after=after,
        new_ratings=new_ratings,
        changed_ratings=changed_ratings,
        duplicates_repaired=duplicates,
        imdb_enriched=int(imdb_enriched or 0),
        tmdb_enriched=tmdb_enriched,
        wikimedia_enriched=wikimedia_enriched,
        wikimedia_failed=wikimedia_failed,
    )
