from __future__ import annotations

from dataclasses import dataclass

from .util import utcnow_iso


SETTING_KEY = "imdb_rating_followups_v1"
MAX_PENDING = 25


@dataclass(frozen=True)
class ImportedImdbRating:
    movie_id: int
    imdb_id: str
    title: str
    rating: int


def pending_imdb_rating_followups(db) -> list[dict]:
    raw = db.get_setting(SETTING_KEY, [])
    if not isinstance(raw, list):
        return []
    clean: list[dict] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            movie_id = int(item.get("movie_id") or 0)
        except (TypeError, ValueError):
            continue
        imdb_id = str(item.get("imdb_id") or "").strip()
        if movie_id <= 0 or not imdb_id or movie_id in seen:
            continue
        seen.add(movie_id)
        clean.append({
            "movie_id": movie_id,
            "imdb_id": imdb_id,
            "title": str(item.get("title") or "Film").strip() or "Film",
            "queued_at": str(item.get("queued_at") or ""),
        })
    return clean[-MAX_PENDING:]


def queue_imdb_rating_followup(db, movie) -> bool:
    """Remember a watched IMDb-linked film until its real IMDb rating is imported."""
    return queue_imdb_rating_followup_values(db, movie.id, movie.imdb_id, movie.title)


def queue_imdb_rating_followup_values(db, movie_id, imdb_id, title) -> bool:
    try:
        movie_id = int(movie_id or 0)
    except (TypeError, ValueError):
        return False
    imdb_id = str(imdb_id or "").strip()
    if movie_id <= 0 or not imdb_id:
        return False
    pending = [
        item for item in pending_imdb_rating_followups(db)
        if int(item["movie_id"]) != movie_id
    ]
    pending.append({
        "movie_id": movie_id,
        "imdb_id": imdb_id,
        "title": str(title or "Film").strip() or "Film",
        "queued_at": utcnow_iso(),
    })
    db.set_setting(SETTING_KEY, pending[-MAX_PENDING:])
    return True


def cancel_imdb_rating_followup(db, movie_id: int) -> bool:
    try:
        movie_id = int(movie_id)
    except (TypeError, ValueError):
        return False
    pending = pending_imdb_rating_followups(db)
    remaining = [item for item in pending if int(item["movie_id"]) != movie_id]
    if len(remaining) == len(pending):
        return False
    db.set_setting(SETTING_KEY, remaining)
    return True


def resolve_imdb_rating_followups(db) -> tuple[list[ImportedImdbRating], list[dict]]:
    """Remove and return follow-ups whose rating has arrived through IMDb sync."""
    pending = pending_imdb_rating_followups(db)
    if not pending:
        return [], []
    ids = [int(item["movie_id"]) for item in pending]
    marks = ",".join("?" for _ in ids)
    with db.connect() as con:
        rows = con.execute(
            f"SELECT movie_id,rating FROM ratings WHERE movie_id IN ({marks})",
            ids,
        ).fetchall()
    ratings = {int(row["movie_id"]): int(row["rating"]) for row in rows}
    resolved: list[ImportedImdbRating] = []
    remaining: list[dict] = []
    for item in pending:
        movie_id = int(item["movie_id"])
        rating = ratings.get(movie_id)
        if rating is None:
            remaining.append(item)
            continue
        resolved.append(ImportedImdbRating(
            movie_id=movie_id,
            imdb_id=str(item["imdb_id"]),
            title=str(item["title"]),
            rating=rating,
        ))
    if len(remaining) != len(pending):
        db.set_setting(SETTING_KEY, remaining)
    return resolved, remaining
