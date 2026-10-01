from __future__ import annotations

from .recommendation_outcomes_v42 import reconcile_recommendation_outcomes


RATING_AUDIT_VERSION = "rating-outcome-audit-v1"


def latest_rating_outcome_audit(db, limit: int = 20) -> dict:
    reconcile_recommendation_outcomes(db)
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.title,m.imdb_id,o.context_date,o.rating_date,o.rank_position,
                      o.predicted_rating,o.actual_rating,o.absolute_error,o.engine_version
               FROM recommendation_outcomes o
               JOIN movies m ON m.id=o.movie_id
               WHERE o.actual_rating IS NOT NULL
               ORDER BY COALESCE(o.rating_date,o.context_date) DESC,o.exposure_history_id DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    items = []
    signed = []
    absolute = []
    for row in rows:
        predicted = float(row["predicted_rating"]) if row["predicted_rating"] is not None else None
        actual = int(row["actual_rating"])
        if predicted is not None:
            signed.append(predicted - actual)
            absolute.append(abs(predicted - actual))
        items.append({
            "title": str(row["title"] or ""),
            "imdb_id": str(row["imdb_id"] or ""),
            "context_date": str(row["context_date"] or ""),
            "rating_date": str(row["rating_date"] or ""),
            "rank_position": int(row["rank_position"]) if row["rank_position"] is not None else None,
            "predicted_rating": round(predicted, 2) if predicted is not None else None,
            "actual_rating": actual,
            "absolute_error": round(float(row["absolute_error"]), 2) if row["absolute_error"] is not None else None,
            "engine_version": str(row["engine_version"] or ""),
        })
    return {
        "version": RATING_AUDIT_VERSION,
        "count": len(items),
        "mae": round(sum(absolute) / len(absolute), 3) if absolute else None,
        "bias": round(sum(signed) / len(signed), 3) if signed else None,
        "items": items,
    }
