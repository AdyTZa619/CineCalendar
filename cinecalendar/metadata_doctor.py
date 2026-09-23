from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlparse

from .models import Movie
from .open_metadata import OpenMovieMetadataProvider
from .tmdb import TmdbProvider
from .util import json_dumps, json_loads, utcnow_iso


METADATA_DOCTOR_VERSION = "metadata-doctor-v4.11.0"
RETRY_MINUTES = (15, 60, 360, 1440, 4320, 10080)
ACTIVE_STATUSES = ("pending", "retry", "running")
TRACKED_FIELDS = ("genres", "directors", "countries", "overview", "runtime", "poster")


@dataclass(frozen=True)
class MetadataDoctorReport:
    queued: int
    due: int
    retrying: int
    completed: int
    blocked: int
    open_issues: int
    broken_posters: int


@dataclass(frozen=True)
class MetadataProcessResult:
    attempted: int
    completed: int
    improved: int
    retrying: int
    failed: int


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _missing_values(movie: Movie) -> list[str]:
    values = {
        "genres": bool(movie.genres),
        "directors": bool(movie.directors),
        "countries": bool(movie.countries),
        "overview": bool(movie.overview or movie.keywords),
        "runtime": bool(movie.runtime_min),
        "poster": bool(movie.poster_url),
    }
    return [field for field in TRACKED_FIELDS if not values[field]]


def _movie_from_row(row) -> Movie:
    return Movie(
        id=int(row["id"]), imdb_id=str(row["imdb_id"] or ""),
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
        release_date=row["release_date"], poster_url=row["poster_url"],
        source=str(row["source"] or ""), semantic=json_loads(row["semantic_json"], {}) or {},
    )


def queue_metadata_movie(db, movie_id: int, *, priority: int = 0, reason: str = "catalog") -> bool:
    try:
        movie_id = int(movie_id)
    except (TypeError, ValueError):
        return False
    with db.connect() as con:
        row = con.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    if row is None or not str(row["imdb_id"] or "").strip():
        return False
    missing = _missing_values(_movie_from_row(row))
    now = utcnow_iso()
    if not missing:
        with db.tx() as con:
            con.execute(
                """UPDATE metadata_jobs SET status='complete',missing_json='[]',completed_at=?,
                   next_check_at=NULL,last_error='',updated_at=? WHERE movie_id=?""",
                (now, now, movie_id),
            )
        return False
    with db.tx() as con:
        con.execute(
            """INSERT INTO metadata_jobs(
                 movie_id,priority,reason,status,attempt_count,missing_json,queued_at,
                 next_check_at,last_error,updated_at
               ) VALUES(?,?,?,'pending',0,?,?,?,'',?)
               ON CONFLICT(movie_id) DO UPDATE SET
                 priority=MAX(metadata_jobs.priority,excluded.priority),
                 reason=excluded.reason,
                 status=CASE WHEN metadata_jobs.status='complete' THEN 'pending'
                             ELSE metadata_jobs.status END,
                 missing_json=excluded.missing_json,
                 next_check_at=CASE WHEN metadata_jobs.status='complete'
                                    THEN excluded.next_check_at ELSE metadata_jobs.next_check_at END,
                 completed_at=NULL,updated_at=excluded.updated_at""",
            (movie_id, int(priority), str(reason or "catalog")[:60], json_dumps(missing), now, now, now),
        )
    return True


def queue_recommendation_metadata(db, recommendations: Iterable, *, visible: int = 12) -> int:
    queued = 0
    for rank, rec in enumerate(recommendations, 1):
        movie = getattr(rec, "movie", None)
        movie_id = getattr(movie, "id", None)
        if not movie_id:
            continue
        priority = 1000 - rank * 10 if rank <= int(visible) else 500 - rank
        queued += int(queue_metadata_movie(db, int(movie_id), priority=priority, reason=f"recommendation:{rank}"))
    return queued


def refresh_metadata_job(db, movie_id: int, *, error: str = "") -> bool:
    """Reconcile a queue row after foreground preflight changed the movie."""
    with db.connect() as con:
        row = con.execute("SELECT * FROM movies WHERE id=?", (int(movie_id),)).fetchone()
    if row is None:
        return False
    missing = _missing_values(_movie_from_row(row)); now = utcnow_iso()
    retry_at = _iso(_now() + timedelta(minutes=RETRY_MINUTES[0]))
    with db.tx() as con:
        if not missing:
            con.execute(
                """UPDATE metadata_jobs SET status='complete',attempt_count=attempt_count+1,
                   missing_json='[]',last_error='',
                   next_check_at=NULL,completed_at=?,updated_at=? WHERE movie_id=?""",
                (now, now, int(movie_id)),
            )
            con.execute(
                "UPDATE metadata_issues SET resolved_at=? WHERE movie_id=? AND resolved_at IS NULL",
                (now, int(movie_id)),
            )
        else:
            con.execute(
                """UPDATE metadata_jobs SET status='retry',attempt_count=attempt_count+1,
                   missing_json=?,last_error=?,next_check_at=?,updated_at=? WHERE movie_id=?""",
                (json_dumps(missing), str(error or "")[:300], retry_at, now, int(movie_id)),
            )
    return not missing


def seed_metadata_queue(db, limit: int = 250) -> int:
    """Queue useful titles only: rated, watchlisted, recently recommended, then sparse candidates."""
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.id,
                      CASE WHEN r.movie_id IS NOT NULL THEN 900
                           WHEN w.movie_id IS NOT NULL THEN 800
                           WHEN h.movie_id IS NOT NULL THEN 700 ELSE 100 END AS priority,
                      CASE WHEN r.movie_id IS NOT NULL THEN 'rated'
                           WHEN w.movie_id IS NOT NULL THEN 'watchlist'
                           WHEN h.movie_id IS NOT NULL THEN 'recommended' ELSE 'catalog' END AS reason
               FROM movies m
               LEFT JOIN ratings r ON r.movie_id=m.id
               LEFT JOIN watchlist w ON w.movie_id=m.id
               LEFT JOIN (SELECT movie_id,MAX(id) AS last_id FROM recommendation_history GROUP BY movie_id) h
                 ON h.movie_id=m.id
               WHERE m.imdb_id GLOB 'tt[0-9]*'
                 AND (TRIM(COALESCE(m.genres_json,'')) IN ('','[]')
                   OR TRIM(COALESCE(m.directors_json,'')) IN ('','[]')
                   OR TRIM(COALESCE(m.countries_json,'')) IN ('','[]')
                   OR TRIM(COALESCE(m.overview,''))=''
                   OR m.runtime_min IS NULL
                   OR TRIM(COALESCE(m.poster_url,''))='')
               ORDER BY priority DESC,COALESCE(h.last_id,0) DESC,m.id DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    return sum(queue_metadata_movie(db, row["id"], priority=row["priority"], reason=row["reason"]) for row in rows)


def _next_retry(attempt: int, now: datetime) -> str:
    index = min(max(0, int(attempt) - 1), len(RETRY_MINUTES) - 1)
    return _iso(now + timedelta(minutes=RETRY_MINUTES[index]))


def process_metadata_queue(
    db, token: str = "", *, limit: int = 6, force: bool = False,
    progress=None, tmdb_factory=None, open_factory=None,
) -> MetadataProcessResult:
    now_dt = _now(); now = _iso(now_dt)
    with db.connect() as con:
        condition = "" if force else "AND (j.next_check_at IS NULL OR j.next_check_at<=?)"
        params = [*ACTIVE_STATUSES]
        if not force:
            params.append(now)
        params.append(max(1, int(limit)))
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = con.execute(
            f"""SELECT m.*,j.attempt_count FROM metadata_jobs j JOIN movies m ON m.id=j.movie_id
                WHERE j.status IN ({marks}) {condition}
                ORDER BY j.priority DESC,COALESCE(j.next_check_at,j.queued_at),j.movie_id LIMIT ?""",
            tuple(params),
        ).fetchall()
    if not rows:
        return MetadataProcessResult(0, 0, 0, 0, 0)

    tmdb = None
    if str(token or "").strip():
        factory = tmdb_factory or TmdbProvider
        tmdb = factory(db, str(token).strip())
    factory = open_factory or OpenMovieMetadataProvider
    try:
        opened = factory(db)
    except Exception:
        opened = None
    completed = improved = retrying = failed = 0
    for index, row in enumerate(rows, 1):
        movie = _movie_from_row(row)
        before = _missing_values(movie)
        attempts = int(row["attempt_count"] or 0) + 1
        if progress:
            progress(f"Metadata Doctor: {index}/{len(rows)} • {movie.title}")
        with db.tx() as con:
            con.execute(
                """UPDATE metadata_jobs SET status='running',attempt_count=?,last_checked_at=?,
                   last_error='',updated_at=? WHERE movie_id=?""",
                (attempts, now, now, movie.id),
            )
        errors: list[str] = []
        for provider in (tmdb, opened):
            if provider is None or not _missing_values(movie):
                continue
            try:
                provider.enrich_by_imdb(movie)
            except Exception as exc:
                errors.append(str(exc).replace("\n", " ")[:160])
        after = _missing_values(movie)
        if len(after) < len(before):
            improved += 1
        checked = utcnow_iso()
        with db.tx() as con:
            if not after:
                completed += 1
                con.execute(
                    """UPDATE metadata_jobs SET status='complete',missing_json='[]',last_error='',
                       next_check_at=NULL,completed_at=?,updated_at=? WHERE movie_id=?""",
                    (checked, checked, movie.id),
                )
                con.execute(
                    "UPDATE metadata_issues SET resolved_at=? WHERE movie_id=? AND resolved_at IS NULL",
                    (checked, movie.id),
                )
            else:
                retrying += 1
                if errors:
                    failed += 1
                con.execute(
                    """UPDATE metadata_jobs SET status='retry',missing_json=?,last_error=?,
                       next_check_at=?,updated_at=? WHERE movie_id=?""",
                    (json_dumps(after), "; ".join(errors)[:300], _next_retry(attempts, now_dt), checked, movie.id),
                )
    return MetadataProcessResult(len(rows), completed, improved, retrying, failed)


def report_broken_poster(db, movie_id: int, url: str, error: str = "") -> bool:
    now = utcnow_iso(); clean_url = str(url or "").strip()
    with db.tx() as con:
        row = con.execute("SELECT poster_url FROM movies WHERE id=?", (int(movie_id),)).fetchone()
        if row is None or str(row["poster_url"] or "").strip() != clean_url:
            return False
        con.execute("UPDATE movies SET poster_url=NULL,updated_at=? WHERE id=?", (now, int(movie_id)))
        con.execute(
            """INSERT INTO metadata_issues(movie_id,field,issue_type,provider,detail,detected_at,resolved_at)
               VALUES(?,'poster','broken_url','',?,?,NULL)
               ON CONFLICT(movie_id,field,issue_type) DO UPDATE SET
                 detail=excluded.detail,detected_at=excluded.detected_at,resolved_at=NULL""",
            (int(movie_id), (clean_url + " • " + str(error or ""))[:500], now),
        )
    queue_metadata_movie(db, int(movie_id), priority=1200, reason="broken_poster")
    return True


def audit_metadata_doctor(db) -> MetadataDoctorReport:
    now = utcnow_iso()
    invalid_ids: list[int] = []
    with db.tx() as con:
        invalid = con.execute(
            """SELECT id,poster_url FROM movies WHERE poster_url IS NOT NULL AND TRIM(poster_url)!=''"""
        ).fetchall()
        for row in invalid:
            url = str(row["poster_url"] or "").strip()
            if urlparse(url).scheme.lower() not in {"http", "https"}:
                con.execute(
                    """INSERT INTO metadata_issues(movie_id,field,issue_type,provider,detail,detected_at,resolved_at)
                       VALUES(?,'poster','invalid_url','',?,?,NULL)
                       ON CONFLICT(movie_id,field,issue_type) DO UPDATE SET
                         detail=excluded.detail,detected_at=excluded.detected_at,resolved_at=NULL""",
                    (int(row["id"]), url[:500], now),
                )
                con.execute("UPDATE movies SET poster_url=NULL,updated_at=? WHERE id=?", (now, int(row["id"])))
                invalid_ids.append(int(row["id"]))
        stats = con.execute(
            """SELECT
                 SUM(CASE WHEN status IN ('pending','running') THEN 1 ELSE 0 END) queued,
                 SUM(CASE WHEN status IN ('pending','retry','running') AND (next_check_at IS NULL OR next_check_at<=?) THEN 1 ELSE 0 END) due,
                 SUM(CASE WHEN status='retry' THEN 1 ELSE 0 END) retrying,
                 SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) completed,
                 SUM(CASE WHEN status='retry' AND attempt_count>=5 THEN 1 ELSE 0 END) blocked
               FROM metadata_jobs""",
            (now,),
        ).fetchone()
        issue = con.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN field='poster' THEN 1 ELSE 0 END) posters
               FROM metadata_issues WHERE resolved_at IS NULL"""
        ).fetchone()
    for movie_id in invalid_ids:
        queue_metadata_movie(db, movie_id, priority=1200, reason="invalid_poster")
    return MetadataDoctorReport(
        queued=int(stats["queued"] or 0), due=int(stats["due"] or 0),
        retrying=int(stats["retrying"] or 0), completed=int(stats["completed"] or 0),
        blocked=int(stats["blocked"] or 0), open_issues=int(issue["total"] or 0),
        broken_posters=int(issue["posters"] or 0),
    )


def recent_metadata_jobs(db, limit: int = 25) -> list[dict]:
    with db.connect() as con:
        rows = con.execute(
            """SELECT j.*,m.title,m.imdb_id FROM metadata_jobs j JOIN movies m ON m.id=j.movie_id
               ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'retry' THEN 2 ELSE 3 END,
                        j.priority DESC,j.updated_at DESC LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]
