from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Recommendation
from .profile import get_profile
from .recommendation import row_to_movie
from .util import clamp, json_loads


@dataclass(frozen=True)
class SmartWatchlistResult:
    recommendations: tuple[Recommendation, ...]
    total: int
    eligible: int
    available: int
    scored: int
    future_hidden: int
    filtered: int


def watchlist_entries(db) -> list[dict]:
    """Return the whole explicit Watchlist in stable, newest-first order."""
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.*,w.status AS watchlist_status,w.added_at AS watchlist_added_at,
                      w.updated_at AS watchlist_updated_at,
                      CASE WHEN r.movie_id IS NULL THEN 0 ELSE 1 END AS is_rated
               FROM watchlist w
               JOIN movies m ON m.id=w.movie_id
               LEFT JOIN ratings r ON r.movie_id=m.id
               WHERE w.status='want_to_watch'
               ORDER BY w.updated_at DESC,w.added_at DESC,m.id DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def _eligible_entries(db) -> tuple[int, list[dict]]:
    all_rows = watchlist_entries(db)
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.*,w.status AS watchlist_status,w.added_at AS watchlist_added_at,
                      w.updated_at AS watchlist_updated_at
               FROM watchlist w
               JOIN movies m ON m.id=w.movie_id
               LEFT JOIN ratings r ON r.movie_id=m.id
               WHERE w.status='want_to_watch'
                 AND r.movie_id IS NULL
                 AND lower(COALESCE(m.title_type,'movie')) IN
                     ('movie','short','tvmovie','video','tv movie')
                 AND NOT EXISTS(
                     SELECT 1 FROM feedback f
                     WHERE f.movie_id=m.id
                       AND f.kind IN ('not_interested','seen','never_similar')
                 )
               ORDER BY w.updated_at DESC,w.added_at DESC,m.id DESC"""
        ).fetchall()
    return len(all_rows), [dict(row) for row in rows]


def _known_future(row: dict, when: date) -> bool:
    year = row.get("year")
    try:
        if year is not None and int(year) > when.year:
            return True
    except (TypeError, ValueError):
        pass
    release = str(row.get("release_date") or "")[:10]
    return bool(len(release) >= 10 and release > when.isoformat())


def pinned_watchlist_ids(db) -> set[int]:
    raw = db.get_setting("watchlist_pinned_movie_ids", []) or []
    out: set[int] = set()
    for value in raw:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


def set_watchlist_pinned(db, movie_id: int, pinned: bool) -> bool:
    ids = pinned_watchlist_ids(db)
    mid = int(movie_id)
    if pinned:
        ids.add(mid)
    else:
        ids.discard(mid)
    db.set_setting("watchlist_pinned_movie_ids", sorted(ids))
    return mid in ids


def remove_from_watchlist(db, movie_id: int) -> bool:
    """Remove only the explicit Watchlist intent; do not create negative taste feedback."""
    mid = int(movie_id)
    with db.tx() as con:
        cur = con.execute("DELETE FROM watchlist WHERE movie_id=?", (mid,))
        removed = int(cur.rowcount or 0) > 0
    if removed:
        set_watchlist_pinned(db, mid, False)
    return removed


def rank_watchlist(
    recommender,
    when: date | None = None,
    count: int = 5,
    mode: str = "decide",
    *,
    runtime_bucket: str = "all",
    content_type: str = "all",
) -> SmartWatchlistResult:
    """Rank explicit Watchlist items through the current production scoring stack.

    This is deliberately not a second recommendation model. It reuses the active engine's
    content score, MovieLens ALS blend, adaptive reranker, Watch Success/Startability layer,
    V16 trust gate and any later validated wrappers. Only candidate discovery is constrained
    to titles the user explicitly put in Watchlist.
    """
    when = when or date.today()
    requested = max(1, int(count))
    total, rows = _eligible_entries(recommender.db)
    eligible = len(rows)

    available_rows = [row for row in rows if not _known_future(row, when)]
    future_hidden = eligible - len(available_rows)

    def matches_filters(row: dict) -> bool:
        runtime = row.get("runtime_min")
        try:
            minutes = int(runtime) if runtime is not None else None
        except (TypeError, ValueError):
            minutes = None
        if runtime_bucket == "short" and (minutes is None or minutes > 90):
            return False
        if runtime_bucket == "medium" and (minutes is None or minutes < 91 or minutes > 120):
            return False
        if runtime_bucket == "long" and (minutes is None or minutes <= 120):
            return False

        typ = str(row.get("title_type") or "").lower()
        genres = {str(x).casefold() for x in (json_loads(row.get("genres_json"), []) or [])}
        if content_type == "short" and typ != "short":
            return False
        if content_type == "documentary" and "documentary" not in genres:
            return False
        if content_type == "movie" and (typ == "short" or "documentary" in genres):
            return False
        return True

    available_rows = [row for row in available_rows if matches_filters(row)]
    if not available_rows:
        return SmartWatchlistResult((), total, eligible, 0, 0, future_hidden, total - eligible)

    profile = get_profile(recommender.db)
    rated_count = int(profile.get("rated_count", 0) or 0)
    minimum = int(getattr(recommender, "MIN_PERSONAL_RATINGS", 1) or 1)
    if rated_count < minimum:
        raise RuntimeError(
            "Nu sunt suficiente ratinguri personale pentru a ordona inteligent Watchlist-ul."
        )

    collaborative = getattr(recommender, "collaborative", None)
    if collaborative is not None and not collaborative.is_ready():
        wait = getattr(recommender, "_wait_briefly_for_first_model", None)
        if callable(wait):
            wait(25.0)

    imdb_ids = [str(row.get("imdb_id") or "") for row in available_rows if row.get("imdb_id")]
    collaborative_scores: dict[str, float] = {}
    mapped_ratings = 0
    if collaborative is not None:
        try:
            collaborative_scores, _raw, mapped_ratings = collaborative.score_candidates(imdb_ids)
        except Exception:
            collaborative_scores, mapped_ratings = {}, 0
    collaborative_active = bool(collaborative_scores) and int(mapped_ratings) >= 20

    context = recommender._run_context()
    pinned = pinned_watchlist_ids(recommender.db)
    candidates: list[Recommendation] = []
    for row in available_rows:
        movie = row_to_movie(row)

        quality_gate = getattr(recommender, "_catalog_quality_is_trustworthy", None)
        if callable(quality_gate) and not quality_gate(movie):
            continue

        score = recommender._score_one(movie, when, profile, context, False, mode)
        if score is None:
            continue
        if float(score.confidence or 0.0) >= 0.55 and float(score.predicted_rating or 0.0) < 5.8:
            continue

        iid = str(movie.imdb_id or "")
        if collaborative_active and iid in collaborative_scores:
            als_score = float(collaborative_scores[iid])
            trustworthy = getattr(recommender, "_mapped_candidate_is_trustworthy", None)
            if callable(trustworthy) and not trustworthy(
                als_score,
                float(score.predicted_rating),
                float(score.confidence),
            ):
                continue
            old_final = float(score.final)
            score.final, als_weight, content_weight = recommender._hybrid_blend(
                als_score, old_final
            )
            reason = recommender._collaborative_reason(mapped_ratings, als_score)
            score.contributions.insert(
                0,
                (
                    "ALS colaborativ MovieLens",
                    als_weight * als_score * 100.0,
                    reason,
                ),
            )
            score.contributions.append(
                (
                    "Motor personal de conținut",
                    content_weight * old_final * 100.0,
                    "Genuri, teme, regizori, calitate, noutate și context; "
                    "semnal independent de verificare.",
                )
            )
        brain = getattr(recommender, "personalization_v41", None)
        is_pinned = bool(movie.id is not None and int(movie.id) in pinned)
        if brain is not None and callable(getattr(brain, "dynamic_watchlist_shift", None)):
            shift, reason = brain.dynamic_watchlist_shift(
                row,
                score,
                pinned=is_pinned,
                when=when,
            )
            score.final = clamp(float(score.final) + float(shift))
            if abs(float(shift)) >= 0.001:
                score.contributions.insert(
                    0,
                    (
                        "Prioritate Watchlist dinamică",
                        float(shift) * 100.0,
                        reason or "Prioritatea se recalculează după noile ratinguri și starea curentă a Watchlist-ului.",
                    ),
                )
        elif is_pinned:
            score.final = clamp(float(score.final) + 0.08)
            score.contributions.insert(
                0,
                (
                    "Prioritate Watchlist",
                    8.0,
                    "Ai marcat filmul «Vreau să-l văd curând»; primește prioritate fără a ocoli filtrele de calitate.",
                ),
            )
        candidates.append(Recommendation(movie, score))

    candidates.sort(
        key=lambda rec: (
            float(rec.score.final),
            float(rec.score.predicted_rating),
            float(rec.score.confidence),
        ),
        reverse=True,
    )

    if not candidates:
        return SmartWatchlistResult(
            (), total, eligible, len(available_rows), 0, future_hidden, total - eligible
        )

    expanded = max(
        int(getattr(recommender, "ADAPTIVE_POOL_MIN", 60) or 60),
        requested * 12,
    )
    base = recommender._select_candidates(candidates, expanded, mode)
    selected = list(recommender._adaptive_rerank(list(base), requested))
    annotate = getattr(recommender, "_annotate_final_als", None)
    if callable(annotate):
        annotate(selected)

    return SmartWatchlistResult(
        tuple(selected),
        total,
        eligible,
        len(available_rows),
        len(candidates),
        future_hidden,
        total - eligible,
    )
