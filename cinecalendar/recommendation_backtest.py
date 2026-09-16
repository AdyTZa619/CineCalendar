from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import json
import sqlite3
import tempfile
import time

from .collaborative_als import CollaborativeALSProvider
from .db import Database
from .recommender_v15 import FastRecommendationEngineV15


@dataclass(frozen=True)
class HoldoutRating:
    movie_id: int
    imdb_id: str
    rating: int
    date_rated: str


def temporal_holdout(
    db: Database,
    *,
    fraction: float = 0.20,
    min_holdout: int = 60,
    max_holdout: int = 500,
) -> list[HoldoutRating]:
    """Return the newest part of the rating history, preserving chronology.

    Rows without a usable IMDb id are ignored because retrieval recall cannot be measured for them.
    The holdout is deterministic and never mutates the source database.
    """
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.id AS movie_id,m.imdb_id,r.rating,
                      COALESCE(r.date_rated,r.updated_at,'') AS date_key
               FROM ratings r JOIN movies m ON m.id=r.movie_id
               WHERE m.imdb_id IS NOT NULL AND m.imdb_id<>''
               ORDER BY COALESCE(r.date_rated,r.updated_at,'') ASC, r.id ASC"""
        ).fetchall()
    if len(rows) < 80:
        return []
    wanted = max(int(min_holdout), int(round(len(rows) * max(0.05, min(0.45, float(fraction))))))
    wanted = min(int(max_holdout), wanted, max(1, len(rows) - 40))
    chosen = rows[-wanted:]
    return [
        HoldoutRating(
            movie_id=int(row["movie_id"]),
            imdb_id=str(row["imdb_id"]),
            rating=int(row["rating"]),
            date_rated=str(row["date_key"] or ""),
        )
        for row in chosen
    ]


def _at_k(ranked: list[str], wanted: set[str], k: int) -> float:
    if not wanted:
        return 0.0
    found = len(wanted.intersection(ranked[: max(0, int(k))]))
    return found / float(len(wanted))


def candidate_recall_metrics(
    ranked_imdb_ids: list[str],
    holdout: list[HoldoutRating],
    *,
    cutoffs: tuple[int, ...] = (50, 100, 250, 500, 1000),
) -> dict:
    """Measure whether retrieval can surface films the user later rated highly.

    This deliberately separates retrieval from final ranking.  If a hidden 9/10 film is absent from
    the candidate pool, no downstream model can recover it.
    """
    ranked = [str(x) for x in ranked_imdb_ids if str(x)]
    rating_by_id = {row.imdb_id: int(row.rating) for row in holdout}
    liked = {iid for iid, rating in rating_by_id.items() if rating >= 8}
    loved = {iid for iid, rating in rating_by_id.items() if rating >= 9}
    disliked = {iid for iid, rating in rating_by_id.items() if rating <= 4}

    out = {
        "holdout": len(holdout),
        "liked_8_plus": len(liked),
        "loved_9_plus": len(loved),
        "disliked_4_minus": len(disliked),
        "ranked_count": len(ranked),
    }
    for cutoff in cutoffs:
        k = int(cutoff)
        out[f"recall_8_plus_at_{k}"] = round(_at_k(ranked, liked, k), 6)
        out[f"recall_9_plus_at_{k}"] = round(_at_k(ranked, loved, k), 6)
        out[f"dislike_recall_at_{k}"] = round(_at_k(ranked, disliked, k), 6)

    matched = [rating_by_id[iid] for iid in ranked if iid in rating_by_id]
    out["matched_holdout"] = len(matched)
    out["matched_mean_rating"] = round(sum(matched) / len(matched), 4) if matched else None
    out["matched_8_plus_share"] = (
        round(sum(1 for value in matched if value >= 8) / len(matched), 6) if matched else None
    )
    out["matched_4_minus_share"] = (
        round(sum(1 for value in matched if value <= 4) / len(matched), 6) if matched else None
    )
    return out


def _sqlite_backup(source: Path, target: Path) -> None:
    source_con = sqlite3.connect(source)
    target_con = sqlite3.connect(target)
    try:
        source_con.backup(target_con)
    finally:
        target_con.close()
        source_con.close()


def _remove_future_signals(db: Database, holdout: list[HoldoutRating]) -> str:
    if not holdout:
        return ""
    holdout_ids = [row.movie_id for row in holdout]
    cutoff = min((row.date_rated[:10] for row in holdout if row.date_rated), default="")
    with db.tx() as con:
        for start in range(0, len(holdout_ids), 700):
            chunk = holdout_ids[start:start + 700]
            marks = ",".join("?" for _ in chunk)
            con.execute(f"DELETE FROM ratings WHERE movie_id IN ({marks})", tuple(chunk))
        if cutoff:
            con.execute("DELETE FROM feedback WHERE substr(created_at,1,10)>=?", (cutoff,))
            con.execute("DELETE FROM recommendation_history WHERE context_date>=?", (cutoff,))
            con.execute("DELETE FROM watchlist WHERE substr(added_at,1,10)>=?", (cutoff,))
    return cutoff


def _wait_for_als(provider: CollaborativeALSProvider, timeout: float) -> None:
    provider.start_background()
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if provider.is_ready():
            return
        status = provider.status()
        if status.get("state") == "error":
            raise RuntimeError(str(status.get("error") or "ALS failed to load"))
        time.sleep(0.20)
    raise TimeoutError("ALS did not become ready before the backtest timeout")


def run_local_backtest(
    db_path: str | Path,
    *,
    fraction: float = 0.20,
    candidate_limit: int = 1800,
    final_limit: int = 100,
    als_timeout: float = 180.0,
) -> dict:
    """Backtest current retrieval/ranking against hidden recent ratings on a DB copy.

    The live CineCalendar database is never modified.  The latest fraction of ratings is removed
    from a temporary SQLite backup, future feedback/watch actions are pruned, and the current engine
    is asked to retrieve/rank those titles as if they were unseen.
    """
    source_path = Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    source_db = Database(source_path)
    holdout = temporal_holdout(source_db, fraction=fraction)
    if not holdout:
        raise RuntimeError("Not enough timestamped IMDb ratings for a temporal holdout")

    with tempfile.TemporaryDirectory(prefix="cinecalendar-backtest-") as tmp:
        temp_path = Path(tmp) / "cinecalendar.db"
        _sqlite_backup(source_path, temp_path)
        test_db = Database(temp_path)
        cutoff = _remove_future_signals(test_db, holdout)

        engine = FastRecommendationEngineV15(test_db)
        _wait_for_als(engine.collaborative, als_timeout)

        # Candidate recall: inspect the exact pre-hydration pool used by the production engine.
        candidate_rowids = engine._balanced_candidate_ids(date.today(), max(100, int(candidate_limit)))
        candidate_imdb: list[str] = []
        with test_db.connect() as con:
            for start in range(0, len(candidate_rowids), 700):
                chunk = candidate_rowids[start:start + 700]
                if not chunk:
                    continue
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"SELECT id,imdb_id FROM movies WHERE id IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                by_id = {int(row["id"]): str(row["imdb_id"] or "") for row in rows}
                candidate_imdb.extend(by_id.get(int(mid), "") for mid in chunk)
        candidate_imdb = [iid for iid in candidate_imdb if iid]

        # Force the adaptive model to finish for an audit run; unlike Home, this is explicitly an
        # offline diagnostic and should measure the mature current engine rather than first-paint fallback.
        engine.adaptive.status()
        final = engine.recommend(
            when=date.today(),
            count=max(3, int(final_limit)),
            record=False,
            candidate_limit=max(100, int(candidate_limit)),
        )
        final_imdb = [str(rec.movie.imdb_id or "") for rec in final if rec.movie.imdb_id]

        report = {
            "cutoff_date": cutoff,
            "source_rating_count": len(holdout) + int(
                source_db.connect().execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
            ) - len(holdout),
            "holdout_fraction": float(fraction),
            "candidate_limit": int(candidate_limit),
            "final_limit": int(final_limit),
            "candidate_recall": candidate_recall_metrics(candidate_imdb, holdout),
            "final_ranking": candidate_recall_metrics(
                final_imdb,
                holdout,
                cutoffs=tuple(k for k in (3, 10, 25, 50, 100) if k <= max(3, int(final_limit))),
            ),
            "candidate_generation": engine.candidate_generation_status(),
            "personal_retrieval": engine.personal_candidates.status(),
            "adaptive": engine.adaptive.status(),
        }
        return report


def report_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
