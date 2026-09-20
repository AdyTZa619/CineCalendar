from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from .util import utcnow_iso


OUTCOME_VERSION = "recommendation-outcomes-v4.2.0"


@dataclass(frozen=True)
class RecommendationPerformance:
    decision_exposures: int
    chosen: int
    playback_confirmed: int
    watched: int
    rated_outcomes: int
    choice_rate: float
    start_rate: float
    watched_rate: float
    liked_rate: float
    mae: float | None
    bias: float | None
    within_one: float | None
    top1_liked_rate: float | None
    top3_liked_rate: float | None
    recent: tuple[dict, ...]


def reconcile_recommendation_outcomes(db) -> int:
    """Link recommendation exposures to later choice/watch events and the eventual IMDb rating.

    Only exposures with an explicit choice, confirmed playback, or watched event become outcomes.
    A later rating is attached only when its date/update is not older than the recommendation day,
    avoiding accidental linkage to legacy ratings.
    """
    now = utcnow_iso()
    with db.connect() as con:
        rows = con.execute(
            """SELECT
                   root.id AS exposure_history_id,
                   root.movie_id,
                   root.context_date,
                   root.slot,
                   root.final_score,
                   root.predicted_rating,
                   root.confidence,
                   audit.rank_position,
                   audit.engine_version,
                   MIN(CASE WHEN ev.action='chosen' THEN ev.recommended_at END) AS chosen_at,
                   MIN(CASE WHEN ev.action='playback_confirmed' THEN ev.recommended_at END) AS playback_at,
                   MIN(CASE WHEN ev.action='watched' THEN ev.recommended_at END) AS watched_at
               FROM recommendation_history root
               LEFT JOIN recommendation_history ev
                 ON ev.exposure_history_id=root.id
                AND ev.action IN ('chosen','playback_confirmed','watched')
               LEFT JOIN recommendation_trust_audit audit
                 ON audit.history_id=root.id
               WHERE root.action IS NULL
               GROUP BY root.id
               HAVING chosen_at IS NOT NULL
                   OR playback_at IS NOT NULL
                   OR watched_at IS NOT NULL
               ORDER BY root.id ASC"""
        ).fetchall()

        rating_rows = con.execute(
            """SELECT id,movie_id,rating,
                      COALESCE(NULLIF(date_rated,''),updated_at,imported_at,'') AS rating_date
               FROM ratings"""
        ).fetchall()
    ratings = {int(row["movie_id"]): row for row in rating_rows}

    changed = 0
    with db.tx() as con:
        for row in rows:
            movie_id = int(row["movie_id"])
            rating = ratings.get(movie_id)
            rating_id = actual_rating = None
            rating_date = None
            absolute_error = None
            resolved_at = None

            if rating is not None:
                candidate_date = str(rating["rating_date"] or "")
                context_date = str(row["context_date"] or "")
                if not candidate_date or not context_date or candidate_date[:10] >= context_date[:10]:
                    rating_id = int(rating["id"])
                    actual_rating = int(rating["rating"])
                    rating_date = candidate_date
                    predicted = row["predicted_rating"]
                    if predicted is not None:
                        absolute_error = abs(float(predicted) - float(actual_rating))
                    resolved_at = now

            before = con.execute(
                "SELECT * FROM recommendation_outcomes WHERE exposure_history_id=?",
                (int(row["exposure_history_id"]),),
            ).fetchone()

            payload = (
                int(row["exposure_history_id"]),
                movie_id,
                int(row["rank_position"]) if row["rank_position"] is not None else None,
                str(row["context_date"] or ""),
                str(row["slot"] or ""),
                str(row["chosen_at"] or "") or None,
                str(row["playback_at"] or "") or None,
                str(row["watched_at"] or "") or None,
                rating_id,
                actual_rating,
                rating_date,
                float(row["predicted_rating"]) if row["predicted_rating"] is not None else None,
                float(row["confidence"]) if row["confidence"] is not None else None,
                float(row["final_score"]) if row["final_score"] is not None else None,
                str(row["engine_version"] or "") or None,
                absolute_error,
                resolved_at,
                now,
            )
            con.execute(
                """INSERT INTO recommendation_outcomes(
                       exposure_history_id,movie_id,rank_position,context_date,slot,
                       chosen_at,playback_at,watched_at,rating_id,actual_rating,rating_date,
                       predicted_rating,confidence,final_score,engine_version,absolute_error,
                       resolved_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(exposure_history_id) DO UPDATE SET
                       movie_id=excluded.movie_id,
                       rank_position=excluded.rank_position,
                       context_date=excluded.context_date,
                       slot=excluded.slot,
                       chosen_at=excluded.chosen_at,
                       playback_at=excluded.playback_at,
                       watched_at=excluded.watched_at,
                       rating_id=excluded.rating_id,
                       actual_rating=excluded.actual_rating,
                       rating_date=excluded.rating_date,
                       predicted_rating=COALESCE(excluded.predicted_rating,recommendation_outcomes.predicted_rating),
                       confidence=COALESCE(excluded.confidence,recommendation_outcomes.confidence),
                       final_score=COALESCE(excluded.final_score,recommendation_outcomes.final_score),
                       engine_version=COALESCE(excluded.engine_version,recommendation_outcomes.engine_version),
                       absolute_error=excluded.absolute_error,
                       resolved_at=excluded.resolved_at,
                       updated_at=excluded.updated_at""",
                payload,
            )
            after = con.execute(
                "SELECT * FROM recommendation_outcomes WHERE exposure_history_id=?",
                (int(row["exposure_history_id"]),),
            ).fetchone()
            if before is None or tuple(before) != tuple(after):
                changed += 1
    return changed


def recommendation_performance(db, *, recent_limit: int = 12) -> RecommendationPerformance:
    reconcile_recommendation_outcomes(db)
    with db.connect() as con:
        exposure_row = con.execute(
            """SELECT COUNT(*)
               FROM recommendation_history
               WHERE action IS NULL
                 AND slot IN ('decision','decision-refill','watchlist_next','decision-v41')"""
        ).fetchone()
        rows = con.execute(
            """SELECT o.*,COALESCE(NULLIF(m.original_title,''),m.title) AS display_title
               FROM recommendation_outcomes o
               JOIN movies m ON m.id=o.movie_id
               ORDER BY COALESCE(o.rating_date,o.watched_at,o.playback_at,o.chosen_at,o.context_date) DESC,
                        o.exposure_history_id DESC"""
        ).fetchall()

    decision_exposures = int(exposure_row[0] or 0)
    chosen = sum(bool(row["chosen_at"]) for row in rows)
    started = sum(bool(row["playback_at"]) for row in rows)
    watched = sum(bool(row["watched_at"]) for row in rows)
    rated = [row for row in rows if row["actual_rating"] is not None]
    calibrated = [row for row in rated if row["predicted_rating"] is not None]

    errors = [abs(float(row["predicted_rating"]) - float(row["actual_rating"])) for row in calibrated]
    signed = [float(row["predicted_rating"]) - float(row["actual_rating"]) for row in calibrated]
    liked = [row for row in rated if int(row["actual_rating"]) >= 8]
    top1 = [row for row in rated if int(row["rank_position"] or 0) == 1]
    top3 = [row for row in rated if 1 <= int(row["rank_position"] or 0) <= 3]

    def rate(num: int, den: int) -> float:
        return (num / den) if den else 0.0

    recent = []
    for row in rows[:max(1, int(recent_limit))]:
        predicted = float(row["predicted_rating"]) if row["predicted_rating"] is not None else None
        actual = int(row["actual_rating"]) if row["actual_rating"] is not None else None
        status = "ales"
        if row["playback_at"]:
            status = "pornit"
        if row["watched_at"]:
            status = "văzut"
        if actual is not None:
            status = f"notat {actual}/10"
        recent.append(
            {
                "title": str(row["display_title"] or ""),
                "context_date": str(row["context_date"] or "")[:10],
                "rank": int(row["rank_position"]) if row["rank_position"] is not None else None,
                "status": status,
                "predicted": predicted,
                "actual": actual,
                "error": (abs(predicted - actual) if predicted is not None and actual is not None else None),
            }
        )

    return RecommendationPerformance(
        decision_exposures=decision_exposures,
        chosen=chosen,
        playback_confirmed=started,
        watched=watched,
        rated_outcomes=len(rated),
        choice_rate=rate(chosen, decision_exposures),
        start_rate=rate(started, chosen),
        watched_rate=rate(watched, chosen),
        liked_rate=rate(len(liked), len(rated)),
        mae=mean(errors) if errors else None,
        bias=mean(signed) if signed else None,
        within_one=rate(sum(error <= 1.0 for error in errors), len(errors)) if errors else None,
        top1_liked_rate=rate(sum(int(row["actual_rating"]) >= 8 for row in top1), len(top1)) if top1 else None,
        top3_liked_rate=rate(sum(int(row["actual_rating"]) >= 8 for row in top3), len(top3)) if top3 else None,
        recent=tuple(recent),
    )
