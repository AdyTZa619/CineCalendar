from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Iterable, Mapping

from .util import json_loads, utcnow_iso


SETTING_KEY = "imdb_rating_followups_v1"
ACTIVE_STATUSES = ("waiting", "rating_not_found", "profile_unavailable", "identity_mismatch")
AUTO_RETRY_STATUSES = ("waiting", "rating_not_found", "profile_unavailable")
IMDB_RATING_SOURCES = ("imdb", "imdb_csv", "imdb_public_sync")
BACKOFF_MINUTES = (10, 30, 60, 180, 360, 720, 1440)


@dataclass(frozen=True)
class ImportedImdbRating:
    movie_id: int
    imdb_id: str
    title: str
    rating: int


@dataclass(frozen=True)
class ImdbFollowupAudit:
    active: int
    overdue: int
    stuck: int
    identity_mismatches: int
    watched_without_rating: int
    recovered: int


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _valid_imdb_id(value: object) -> str:
    raw = str(value or "").strip()
    return raw if re.fullmatch(r"tt\d+", raw) else ""


def _next_after_attempt(attempt_count: int, *, now: datetime | None = None) -> str:
    current = now or _now()
    index = min(max(0, int(attempt_count) - 1), len(BACKOFF_MINUTES) - 1)
    return _iso(current + timedelta(minutes=BACKOFF_MINUTES[index]))


def _row_dict(row) -> dict:
    return {
        "movie_id": int(row["movie_id"]),
        "imdb_id": str(row["imdb_id"] or ""),
        "title": str(row["title"] or "Film"),
        "status": str(row["status"] or "waiting"),
        "attempt_count": int(row["attempt_count"] or 0),
        "queued_at": str(row["queued_at"] or ""),
        "last_checked_at": str(row["last_checked_at"] or ""),
        "next_check_at": str(row["next_check_at"] or ""),
        "last_error": str(row["last_error"] or ""),
        "matched_imdb_id": str(row["matched_imdb_id"] or ""),
        "resolved_rating": int(row["resolved_rating"]) if row["resolved_rating"] is not None else None,
        "resolved_at": str(row["resolved_at"] or ""),
    }


def pending_imdb_rating_followups(db) -> list[dict]:
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    with db.connect() as con:
        rows = con.execute(
            f"""SELECT * FROM imdb_rating_followups
                WHERE status IN ({marks})
                ORDER BY queued_at,movie_id""",
            ACTIVE_STATUSES,
        ).fetchall()
    return [_row_dict(row) for row in rows]


def recent_imdb_rating_followups(db, limit: int = 10) -> list[dict]:
    with db.connect() as con:
        rows = con.execute(
            """SELECT * FROM imdb_rating_followups
               ORDER BY CASE WHEN status='found' THEN 1 ELSE 0 END,
                        COALESCE(last_checked_at,queued_at) DESC,movie_id DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def queue_imdb_rating_followup(db, movie) -> bool:
    """Remember a watched IMDb-linked film until its exact IMDb rating is confirmed."""
    return queue_imdb_rating_followup_values(db, movie.id, movie.imdb_id, movie.title)


def queue_imdb_rating_followup_values(db, movie_id, imdb_id, title) -> bool:
    try:
        movie_id = int(movie_id or 0)
    except (TypeError, ValueError):
        return False
    imdb_id = _valid_imdb_id(imdb_id)
    if movie_id <= 0 or not imdb_id:
        return False
    now = _now()
    queued_at = _iso(now)
    next_check = _iso(now + timedelta(minutes=2))
    clean_title = str(title or "Film").strip() or "Film"
    with db.tx() as con:
        identity = con.execute("SELECT imdb_id FROM movies WHERE id=?", (movie_id,)).fetchone()
        if identity is None or str(identity["imdb_id"] or "") != imdb_id:
            return False
        con.execute(
            """INSERT INTO imdb_rating_followups(
                 movie_id,imdb_id,title,status,attempt_count,queued_at,last_checked_at,
                 next_check_at,last_error,matched_imdb_id,resolved_rating,resolved_at,updated_at
               ) VALUES(?,?,?,'waiting',0,?,NULL,?,'',NULL,NULL,NULL,?)
               ON CONFLICT(movie_id) DO UPDATE SET
                 imdb_id=excluded.imdb_id,title=excluded.title,status='waiting',attempt_count=0,
                 queued_at=excluded.queued_at,last_checked_at=NULL,next_check_at=excluded.next_check_at,
                 last_error='',matched_imdb_id=NULL,resolved_rating=NULL,resolved_at=NULL,
                 updated_at=excluded.updated_at""",
            (movie_id, imdb_id, clean_title, queued_at, next_check, queued_at),
        )
    return True


def cancel_imdb_rating_followup(db, movie_id: int) -> bool:
    try:
        movie_id = int(movie_id)
    except (TypeError, ValueError):
        return False
    with db.tx() as con:
        removed = con.execute(
            "DELETE FROM imdb_rating_followups WHERE movie_id=? AND status!='found'", (movie_id,)
        ).rowcount
    return bool(removed)


def due_imdb_rating_followups(db) -> list[dict]:
    now = _iso(_now())
    marks = ",".join("?" for _ in AUTO_RETRY_STATUSES)
    with db.connect() as con:
        rows = con.execute(
            f"""SELECT * FROM imdb_rating_followups
                WHERE status IN ({marks})
                  AND (next_check_at IS NULL OR next_check_at<=?)
                ORDER BY COALESCE(next_check_at,queued_at),movie_id""",
            (*AUTO_RETRY_STATUSES, now),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def begin_imdb_followup_check(db, *, force: bool = False) -> set[int]:
    """Claim due follow-ups and record exactly one network attempt for each."""
    current = _now()
    now = _iso(current)
    eligible_statuses = ACTIVE_STATUSES if force else AUTO_RETRY_STATUSES
    marks = ",".join("?" for _ in eligible_statuses)
    with db.tx() as con:
        sql = f"SELECT movie_id,attempt_count FROM imdb_rating_followups WHERE status IN ({marks})"
        params: list[object] = list(eligible_statuses)
        if not force:
            sql += " AND (next_check_at IS NULL OR next_check_at<=?)"
            params.append(now)
        rows = con.execute(sql, tuple(params)).fetchall()
        ids: set[int] = set()
        for row in rows:
            movie_id = int(row["movie_id"])
            attempts = int(row["attempt_count"] or 0) + 1
            ids.add(movie_id)
            con.execute(
                """UPDATE imdb_rating_followups
                   SET status='waiting',attempt_count=?,last_checked_at=?,next_check_at=?,
                       last_error='',updated_at=? WHERE movie_id=?""",
                (attempts, now, _next_after_attempt(attempts, now=current), now, movie_id),
            )
    return ids


def record_imdb_followup_failure(db, movie_ids: Iterable[int], error: object) -> int:
    ids = sorted({int(value) for value in movie_ids if value})
    if not ids:
        return 0
    detail = str(error or "IMDb indisponibil").strip().replace("\n", " ")[:240]
    marks = ",".join("?" for _ in ids)
    now = utcnow_iso()
    with db.tx() as con:
        changed = con.execute(
            f"""UPDATE imdb_rating_followups
                SET status='profile_unavailable',last_error=?,updated_at=?
                WHERE movie_id IN ({marks}) AND status!='found'""",
            (detail, now, *ids),
        ).rowcount
    return int(changed)


def _profile_match(con, queued_imdb: str, ratings: Mapping[str, int]) -> tuple[str, int] | None:
    if queued_imdb in ratings:
        return queued_imdb, int(ratings[queued_imdb])
    row = con.execute("SELECT value_json FROM settings WHERE key='imdb_public_id_aliases'").fetchone()
    aliases = json_loads(row[0], {}) if row else {}
    if not isinstance(aliases, dict):
        aliases = {}
    for remote_imdb, rating in ratings.items():
        if str(aliases.get(remote_imdb) or remote_imdb) == queued_imdb:
            return str(remote_imdb), int(rating)
    return None


def resolve_imdb_rating_followups(
    db,
    *,
    profile_ratings: Mapping[str, int] | None = None,
    checked_movie_ids: Iterable[int] | None = None,
) -> tuple[list[ImportedImdbRating], list[dict]]:
    """Resolve only ratings whose local movie and remote IMDb identity agree."""
    pending = pending_imdb_rating_followups(db)
    if not pending:
        return [], []
    checked = {int(value) for value in (checked_movie_ids or []) if value}
    profile: dict[str, int] = {}
    for key, value in (profile_ratings or {}).items():
        imdb_id = _valid_imdb_id(key)
        try:
            rating = int(value)
        except (TypeError, ValueError):
            continue
        if imdb_id and 1 <= rating <= 10:
            profile[imdb_id] = rating
    now = utcnow_iso()
    resolved: list[ImportedImdbRating] = []
    with db.tx() as con:
        for item in pending:
            movie_id = int(item["movie_id"])
            queued_imdb = str(item["imdb_id"])
            row = con.execute(
                """SELECT m.imdb_id,r.rating,r.source
                   FROM movies m LEFT JOIN ratings r ON r.movie_id=m.id
                   WHERE m.id=?""",
                (movie_id,),
            ).fetchone()
            current_imdb = str(row["imdb_id"] or "") if row else ""
            if not row or current_imdb != queued_imdb:
                con.execute(
                    """UPDATE imdb_rating_followups
                       SET status='identity_mismatch',last_error=?,next_check_at=NULL,updated_at=?
                       WHERE movie_id=?""",
                    ("IMDb ID-ul filmului nu mai corespunde identității puse în coadă.", now, movie_id),
                )
                continue

            local_rating = int(row["rating"]) if row["rating"] is not None else None
            local_source = str(row["source"] or "")
            match = _profile_match(con, queued_imdb, profile) if profile_ratings is not None else None
            confirmed = False
            matched_imdb = queued_imdb
            if profile_ratings is None:
                confirmed = local_rating is not None and local_source in IMDB_RATING_SOURCES
            elif match is not None:
                matched_imdb, remote_rating = match
                confirmed = local_rating == remote_rating and local_source in IMDB_RATING_SOURCES

            if confirmed and local_rating is not None:
                con.execute(
                    """UPDATE imdb_rating_followups
                       SET status='found',last_error='',matched_imdb_id=?,resolved_rating=?,
                           resolved_at=?,next_check_at=NULL,updated_at=? WHERE movie_id=?""",
                    (matched_imdb, local_rating, now, now, movie_id),
                )
                resolved.append(ImportedImdbRating(movie_id, queued_imdb, str(item["title"]), local_rating))
            elif movie_id in checked:
                error = "Ratingul nu apare încă în profilul public IMDb."
                if match is not None and local_rating != int(match[1]):
                    error = "Ratingul IMDb a fost găsit, dar legarea locală nu a fost confirmată."
                con.execute(
                    """UPDATE imdb_rating_followups
                       SET status='rating_not_found',last_error=?,updated_at=? WHERE movie_id=?""",
                    (error, now, movie_id),
                )
    return resolved, pending_imdb_rating_followups(db)


def recover_missing_imdb_followups(db) -> int:
    """Rebuild missing queue entries for watched IMDb titles that still have no rating."""
    with db.connect() as con:
        rows = con.execute(
            """SELECT DISTINCT m.id,m.imdb_id,m.title
               FROM movies m
               WHERE m.imdb_id GLOB 'tt[0-9]*'
                 AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.movie_id=m.id)
                 AND NOT EXISTS (SELECT 1 FROM imdb_rating_followups f WHERE f.movie_id=m.id)
                 AND (
                   EXISTS (SELECT 1 FROM feedback x WHERE x.movie_id=m.id AND x.kind='seen')
                   OR EXISTS (SELECT 1 FROM recommendation_history h WHERE h.movie_id=m.id AND h.action='watched')
                   OR EXISTS (SELECT 1 FROM watchlist w WHERE w.movie_id=m.id AND w.status='watched')
                 )"""
        ).fetchall()
    recovered = 0
    for row in rows:
        recovered += int(queue_imdb_rating_followup_values(
            db, int(row["id"]), str(row["imdb_id"]), str(row["title"]),
        ))
    return recovered


def audit_imdb_rating_followups(db, *, recover: bool = True) -> ImdbFollowupAudit:
    recovered = recover_missing_imdb_followups(db) if recover else 0
    now = _iso(_now())
    with db.connect() as con:
        row = con.execute(
            """SELECT
                 SUM(CASE WHEN f.status!='found' THEN 1 ELSE 0 END) AS active,
                 SUM(CASE WHEN f.status!='found' AND f.next_check_at<=? THEN 1 ELSE 0 END) AS overdue,
                 SUM(CASE WHEN f.status!='found' AND f.attempt_count>=5 THEN 1 ELSE 0 END) AS stuck,
                 SUM(CASE WHEN f.status='identity_mismatch' OR m.imdb_id!=f.imdb_id THEN 1 ELSE 0 END) AS mismatch
               FROM imdb_rating_followups f JOIN movies m ON m.id=f.movie_id""",
            (now,),
        ).fetchone()
        missing = con.execute(
            """SELECT COUNT(DISTINCT m.id) FROM movies m
               WHERE m.imdb_id GLOB 'tt[0-9]*'
                 AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.movie_id=m.id)
                 AND (
                   EXISTS (SELECT 1 FROM feedback x WHERE x.movie_id=m.id AND x.kind='seen')
                   OR EXISTS (SELECT 1 FROM recommendation_history h WHERE h.movie_id=m.id AND h.action='watched')
                   OR EXISTS (SELECT 1 FROM watchlist w WHERE w.movie_id=m.id AND w.status='watched')
                 )"""
        ).fetchone()[0]
    return ImdbFollowupAudit(
        active=int(row["active"] or 0), overdue=int(row["overdue"] or 0),
        stuck=int(row["stuck"] or 0), identity_mismatches=int(row["mismatch"] or 0),
        watched_without_rating=int(missing or 0), recovered=int(recovered),
    )
