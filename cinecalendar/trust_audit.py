from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
import json


TRUST_STATUSES = ("trusted", "backfill", "red_flag", "bypassed", "unclassified")
OUTCOME_ACTIONS = (
    "chosen",
    "skip_today",
    "trailer_opened",
    "stremio_opened",
    "play_opened",  # legacy URL handoff; not verified playback
    "playback_confirmed",
    "watched",
)


def ensure_trust_audit_schema(con) -> None:
    """Create the 3.1 trust telemetry table without changing the core DB migration path."""
    con.execute(
        """CREATE TABLE IF NOT EXISTS recommendation_trust_audit(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               history_id INTEGER NOT NULL UNIQUE REFERENCES recommendation_history(id) ON DELETE CASCADE,
               run_id INTEGER REFERENCES recommendation_runs(id) ON DELETE SET NULL,
               movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
               context_date TEXT NOT NULL,
               slot TEXT NOT NULL,
               rank_position INTEGER NOT NULL,
               engine_version TEXT NOT NULL,
               trust_status TEXT NOT NULL,
               trust_score REAL,
               gate_score REAL,
               support_count INTEGER NOT NULL DEFAULT 0,
               support_labels TEXT NOT NULL DEFAULT '[]',
               red_flag INTEGER NOT NULL DEFAULT 0,
               red_reason TEXT,
               score_gap REAL,
               als_score REAL,
               public_bayes REAL,
               created_at TEXT NOT NULL
           )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_rec_trust_date_status "
        "ON recommendation_trust_audit(context_date,trust_status)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_rec_trust_movie_date "
        "ON recommendation_trust_audit(movie_id,context_date)"
    )


def record_trust_snapshot(
    con,
    *,
    history_id: int,
    run_id: int | None,
    movie_id: int,
    context_date: str,
    slot: str,
    rank_position: int,
    engine_version: str,
    payload: dict | None,
    created_at: str,
) -> None:
    payload = dict(payload or {})
    status = str(payload.get("status") or "unclassified")
    supports = [str(value) for value in (payload.get("supports") or []) if str(value).strip()]

    def number(name: str):
        value = payload.get(name)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    con.execute(
        """INSERT OR REPLACE INTO recommendation_trust_audit(
               history_id,run_id,movie_id,context_date,slot,rank_position,engine_version,
               trust_status,trust_score,gate_score,support_count,support_labels,red_flag,
               red_reason,score_gap,als_score,public_bayes,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(history_id),
            int(run_id) if run_id is not None else None,
            int(movie_id),
            str(context_date),
            str(slot),
            max(1, int(rank_position)),
            str(engine_version),
            status,
            number("trust"),
            number("gate_score"),
            len(supports),
            json.dumps(supports, ensure_ascii=False),
            1 if payload.get("red_flag") else 0,
            str(payload.get("red_reason") or ""),
            number("gap"),
            number("als"),
            number("public_bayes"),
            str(created_at),
        ),
    )


def _empty_status() -> dict:
    return {
        "recommendations": 0,
        "stremio_attempts": 0,
        "confirmed_starts": 0,
        "watched": 0,
        "skipped": 0,
        "avg_trust_score": None,
        "start_rate": None,
        "watch_rate": None,
        "skip_rate": None,
    }


def _ratio(num: int, den: int):
    return round(num / den, 4) if den > 0 else None


def build_trust_outcome_audit(db, days: int = 90) -> dict:
    """Correlate V16 trust labels with observed local watch outcomes.

    This is descriptive telemetry only. It does not tune thresholds automatically and does not
    send data outside the local SQLite database.
    """
    days = max(1, min(int(days or 90), 3650))
    start_day = (date.today() - timedelta(days=days - 1)).isoformat()

    with db.tx() as con:
        ensure_trust_audit_schema(con)

    with db.connect() as con:
        trust_rows = con.execute(
            """SELECT movie_id,context_date,trust_status,trust_score,engine_version
               FROM recommendation_trust_audit
               WHERE context_date>=?
               ORDER BY id ASC""",
            (start_day,),
        ).fetchall()
        marks = ",".join("?" for _ in OUTCOME_ACTIONS)
        action_rows = con.execute(
            f"""SELECT movie_id,context_date,recommended_at,action,id
                FROM recommendation_history
                WHERE action IN ({marks}) AND context_date>=?
                ORDER BY id ASC""",
            (*OUTCOME_ACTIONS, start_day),
        ).fetchall()

    actions_by_key: dict[tuple[int, str], list[str]] = defaultdict(list)
    for row in action_rows:
        day = str(row["context_date"] or "").strip()
        if not day:
            raw = str(row["recommended_at"] or "")
            day = raw[:10] if len(raw) >= 10 else "unknown"
        action = str(row["action"] or "").strip()
        if action == "play_opened":
            action = "stremio_opened"
        actions_by_key[(int(row["movie_id"]), day)].append(action)

    summaries = {status: _empty_status() for status in TRUST_STATUSES}
    trust_values: dict[str, list[float]] = defaultdict(list)
    engine_versions: Counter[str] = Counter()
    total_confirmed = 0

    for row in trust_rows:
        status = str(row["trust_status"] or "unclassified")
        if status not in summaries:
            summaries[status] = _empty_status()
        item = summaries[status]
        item["recommendations"] += 1
        engine_versions[str(row["engine_version"] or "unknown")] += 1
        if row["trust_score"] is not None:
            trust_values[status].append(float(row["trust_score"]))

        key = (int(row["movie_id"]), str(row["context_date"] or ""))
        actions = actions_by_key.get(key, [])
        has_stremio = "stremio_opened" in actions
        has_confirmed = "playback_confirmed" in actions or "watched" in actions
        has_watched = "watched" in actions
        final = actions[-1] if actions else ""
        if has_stremio:
            item["stremio_attempts"] += 1
        if has_confirmed:
            item["confirmed_starts"] += 1
            total_confirmed += 1
        if has_watched:
            item["watched"] += 1
        if final == "skip_today":
            item["skipped"] += 1

    for status, item in summaries.items():
        total = int(item["recommendations"])
        values = trust_values.get(status, [])
        item["avg_trust_score"] = round(sum(values) / len(values), 4) if values else None
        item["start_rate"] = _ratio(int(item["confirmed_starts"]), total)
        item["watch_rate"] = _ratio(int(item["watched"]), total)
        item["skip_rate"] = _ratio(int(item["skipped"]), total)

    total = len(trust_rows)
    return {
        "available": total > 0,
        "window_days": days,
        "start_date": start_day,
        "recommendations": total,
        "engine_versions": dict(engine_versions),
        "by_status": summaries,
        "confirmed_starts": total_confirmed,
        "enough_data_for_tuning": total >= 20 and total_confirmed >= 5,
        "note": (
            "Pragurile V16 nu sunt ajustate automat. Comparația trusted/backfill devine utilă "
            "abia după suficiente rezultate reale de vizionare."
        ),
    }
