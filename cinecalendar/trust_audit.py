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
_EVENT_SLOTS = ("decision_action", "watch_success_v3")


def ensure_trust_audit_schema(con) -> None:
    """Fail fast if the canonical v5 schema is not available."""
    table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_trust_audit'"
    ).fetchone()
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(recommendation_history)").fetchall()}
    if table is None or "exposure_history_id" not in columns:
        raise RuntimeError("Schema CineCalendar incompletă: migrarea SQLite v5 nu este aplicată.")


def validate_exposure_history_id(
    con,
    exposure_history_id: int | None,
    *,
    movie_id: int | None = None,
    context_date: str | None = None,
):
    """Return the immutable root exposure row only when the supplied id is valid.

    Event rows are never accepted as exposure roots. Optional movie/date checks prevent stale UI
    state from attaching an action to an unrelated recommendation.
    """
    if exposure_history_id is None:
        return None
    try:
        exposure_history_id = int(exposure_history_id)
    except (TypeError, ValueError):
        return None
    marks = ",".join("?" for _ in _EVENT_SLOTS)
    row = con.execute(
        f"""SELECT * FROM recommendation_history
            WHERE id=?
              AND exposure_history_id IS NULL
              AND slot NOT IN ({marks})
            LIMIT 1""",
        (exposure_history_id, *_EVENT_SLOTS),
    ).fetchone()
    if row is None:
        return None
    if movie_id is not None and int(row["movie_id"]) != int(movie_id):
        return None
    if context_date is not None and str(row["context_date"] or "") != str(context_date):
        return None
    return row


def _candidate_exposure_ids(con, movie_id: int, context_date: str) -> list[int]:
    marks = ",".join("?" for _ in _EVENT_SLOTS)
    rows = con.execute(
        f"""SELECT h.id
            FROM recommendation_history h
            WHERE h.movie_id=? AND h.context_date=?
              AND h.exposure_history_id IS NULL
              AND h.slot NOT IN ({marks})
              AND (
                    h.action IS NULL
                    OR EXISTS(
                        SELECT 1 FROM recommendation_trust_audit t
                        WHERE t.history_id=h.id
                    )
                  )
            ORDER BY h.id ASC""",
        (int(movie_id), str(context_date), *_EVENT_SLOTS),
    ).fetchall()
    return [int(row[0]) for row in rows]


def resolve_exposure_history_id(con, movie_id: int, context_date: str) -> int | None:
    """Resolve only an unambiguous exposure.

    v3.3 intentionally refuses the old "latest movie + day" guess. UI paths pass the exposure id
    explicitly; this resolver exists for legacy compatibility and returns None whenever multiple
    valid roots exist.
    """
    ids = _candidate_exposure_ids(con, int(movie_id), str(context_date))
    return ids[0] if len(ids) == 1 else None


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
    """Correlate each V16 exposure with observed outcomes without guessing ambiguous links."""
    days = max(1, min(int(days or 90), 3650))
    start_day = (date.today() - timedelta(days=days - 1)).isoformat()

    with db.connect() as con:
        ensure_trust_audit_schema(con)
        trust_rows = con.execute(
            """SELECT history_id,movie_id,context_date,trust_status,trust_score,engine_version
               FROM recommendation_trust_audit
               WHERE context_date>=?
               ORDER BY id ASC""",
            (start_day,),
        ).fetchall()
        marks = ",".join("?" for _ in OUTCOME_ACTIONS)
        action_rows = con.execute(
            f"""SELECT id,movie_id,context_date,recommended_at,action,exposure_history_id
                FROM recommendation_history
                WHERE action IN ({marks}) AND context_date>=?
                ORDER BY id ASC""",
            (*OUTCOME_ACTIONS, start_day),
        ).fetchall()

    trust_ids = {int(row["history_id"]) for row in trust_rows}
    trust_by_movie_day: dict[tuple[int, str], list[int]] = defaultdict(list)
    for row in trust_rows:
        trust_by_movie_day[(int(row["movie_id"]), str(row["context_date"] or ""))].append(
            int(row["history_id"])
        )

    actions_by_exposure: dict[int, list[str]] = defaultdict(list)
    legacy_linked_actions = 0
    ambiguous_unlinked_actions = 0
    orphan_unlinked_actions = 0
    exact_linked_actions = 0

    for row in action_rows:
        action = str(row["action"] or "").strip()
        if action == "play_opened":
            action = "stremio_opened"

        exposure_id = row["exposure_history_id"]
        if exposure_id is not None:
            root = int(exposure_id)
            if root in trust_ids:
                actions_by_exposure[root].append(action)
                exact_linked_actions += 1
            else:
                orphan_unlinked_actions += 1
            continue

        row_id = int(row["id"])
        if row_id in trust_ids:
            # Compatibility with 3.2 rows where choose/skip mutated the exposure itself.
            actions_by_exposure[row_id].append(action)
            exact_linked_actions += 1
            continue

        day = str(row["context_date"] or "").strip()
        if not day:
            raw = str(row["recommended_at"] or "")
            day = raw[:10] if len(raw) >= 10 else "unknown"
        candidates = trust_by_movie_day.get((int(row["movie_id"]), day), [])
        if len(candidates) == 1:
            actions_by_exposure[candidates[0]].append(action)
            legacy_linked_actions += 1
        elif len(candidates) > 1:
            ambiguous_unlinked_actions += 1
        else:
            orphan_unlinked_actions += 1

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

        actions = actions_by_exposure.get(int(row["history_id"]), [])
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
        "exact_linked_actions": exact_linked_actions,
        "legacy_linked_actions": legacy_linked_actions,
        "ambiguous_unlinked_actions": ambiguous_unlinked_actions,
        "orphan_unlinked_actions": orphan_unlinked_actions,
        "enough_data_for_tuning": total >= 20 and total_confirmed >= 5,
        "note": (
            "Pragurile V16 nu sunt ajustate automat. Comparația trusted/backfill devine utilă "
            "abia după suficiente rezultate reale de vizionare și legături exacte."
        ),
    }
