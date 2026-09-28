from __future__ import annotations

from hashlib import sha256
import json


def rating_history_snapshot(db) -> dict:
    """Small, stable fingerprint of the ratings used by a V5 historical evaluation."""
    digest = sha256()
    count = 0
    with db.connect() as con:
        rows = con.execute(
            "SELECT movie_id,rating,date_rated,updated_at FROM ratings ORDER BY movie_id"
        )
        for row in rows:
            values = [row[key] for key in ("movie_id", "rating", "date_rated", "updated_at")]
            digest.update(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
            count += 1
    return {"count": count, "sha256": digest.hexdigest()}


def report_rating_freshness(db, report: dict) -> bool | None:
    """None means an older report has no fingerprint and cannot be checked."""
    expected = report.get("rating_snapshot") if isinstance(report, dict) else None
    if not isinstance(expected, dict) or "sha256" not in expected:
        return None
    return expected == rating_history_snapshot(db)
