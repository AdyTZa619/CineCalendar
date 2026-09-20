from __future__ import annotations

from datetime import date, timedelta

from .util import json_dumps, json_loads, utcnow_iso


HISTORY_SNAPSHOT_VERSION = "history-snapshot-v4.3.0"


def record_explanation_snapshot(con, history_id: int, score, *, created_at: str | None = None) -> None:
    """Persist the exact explanation rendered for one recommendation exposure."""
    factors = dict(getattr(score, "score_factors", {}) or {})
    contributions = list(getattr(score, "contributions", []) or [])
    con.execute(
        """INSERT INTO recommendation_explanations(
               history_id,personal_reason,why_not,score_factors_json,contributions_json,created_at
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(history_id) DO UPDATE SET
               personal_reason=excluded.personal_reason,
               why_not=excluded.why_not,
               score_factors_json=excluded.score_factors_json,
               contributions_json=excluded.contributions_json,
               created_at=excluded.created_at""",
        (
            int(history_id),
            str(getattr(score, "personal_reason", "") or ""),
            str(getattr(score, "why_not", "") or ""),
            json_dumps(factors),
            json_dumps(contributions),
            created_at or utcnow_iso(),
        ),
    )


def history_engine_versions(db) -> list[str]:
    with db.connect() as con:
        rows = con.execute(
            """SELECT DISTINCT engine_version
               FROM recommendation_trust_audit
               WHERE engine_version IS NOT NULL AND trim(engine_version)<>''
               ORDER BY engine_version DESC"""
        ).fetchall()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


def recommendation_history_rows(
    db,
    *,
    days: int | None = None,
    status: str = "",
    engine_version: str = "",
    limit: int = 3000,
) -> list[dict]:
    where = ["h.action IS NULL"]
    params: list[object] = []
    if days is not None:
        cutoff = (date.today() - timedelta(days=max(0, int(days)))).isoformat()
        where.append("h.context_date>=?")
        params.append(cutoff)
    if engine_version:
        where.append("COALESCE(a.engine_version,'')=?")
        params.append(str(engine_version))

    status_filter = "WHERE row_status=?" if status else ""
    if status:
        params.append(str(status))
    params.append(max(1, int(limit)))

    sql = f"""
        WITH base AS (
            SELECT
                h.id AS history_id,
                h.movie_id,
                h.recommended_at,
                h.context_date,
                h.slot,
                h.final_score,
                h.ignored,
                h.predicted_rating,
                h.confidence,
                COALESCE(NULLIF(m.original_title,''),m.title) AS display_title,
                m.imdb_id,
                a.rank_position,
                a.engine_version,
                a.trust_status,
                a.support_labels,
                o.chosen_at,
                o.playback_at,
                o.watched_at,
                o.actual_rating,
                o.rating_date,
                o.absolute_error,
                e.personal_reason,
                e.why_not,
                e.score_factors_json,
                e.contributions_json,
                (
                    SELECT ev.action
                    FROM recommendation_history ev
                    WHERE ev.exposure_history_id=h.id
                      AND ev.action IS NOT NULL
                    ORDER BY ev.id DESC
                    LIMIT 1
                ) AS latest_action
            FROM recommendation_history h
            JOIN movies m ON m.id=h.movie_id
            LEFT JOIN recommendation_trust_audit a ON a.history_id=h.id
            LEFT JOIN recommendation_outcomes o ON o.exposure_history_id=h.id
            LEFT JOIN recommendation_explanations e ON e.history_id=h.id
            WHERE {' AND '.join(where)}
        ),
        classified AS (
            SELECT
                base.*,
                CASE
                    WHEN actual_rating IS NOT NULL THEN 'rated'
                    WHEN watched_at IS NOT NULL AND watched_at<>'' THEN 'watched'
                    WHEN playback_at IS NOT NULL AND playback_at<>'' THEN 'started'
                    WHEN chosen_at IS NOT NULL AND chosen_at<>'' THEN 'chosen'
                    WHEN COALESCE(latest_action,'')='skip_today' THEN 'skipped'
                    WHEN COALESCE(ignored,0)<>0 THEN 'ignored'
                    ELSE 'shown'
                END AS row_status
            FROM base
        )
        SELECT *
        FROM classified
        {status_filter}
        ORDER BY history_id DESC
        LIMIT ?
    """
    with db.connect() as con:
        rows = con.execute(sql, tuple(params)).fetchall()

    out = []
    for row in rows:
        out.append({
            "history_id": int(row["history_id"]),
            "movie_id": int(row["movie_id"]),
            "recommended_at": str(row["recommended_at"] or ""),
            "context_date": str(row["context_date"] or ""),
            "slot": str(row["slot"] or ""),
            "final_score": float(row["final_score"]) if row["final_score"] is not None else None,
            "predicted_rating": float(row["predicted_rating"]) if row["predicted_rating"] is not None else None,
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "title": str(row["display_title"] or ""),
            "imdb_id": str(row["imdb_id"] or ""),
            "rank": int(row["rank_position"]) if row["rank_position"] is not None else None,
            "engine_version": str(row["engine_version"] or ""),
            "trust_status": str(row["trust_status"] or ""),
            "supports": json_loads(row["support_labels"], []) or [],
            "status": str(row["row_status"] or "shown"),
            "actual_rating": int(row["actual_rating"]) if row["actual_rating"] is not None else None,
            "rating_date": str(row["rating_date"] or ""),
            "absolute_error": float(row["absolute_error"]) if row["absolute_error"] is not None else None,
            "personal_reason": str(row["personal_reason"] or ""),
            "why_not": str(row["why_not"] or ""),
            "score_factors": json_loads(row["score_factors_json"], {}) or {},
            "contributions": json_loads(row["contributions_json"], []) or [],
            "snapshot_available": row["personal_reason"] is not None,
        })
    return out
