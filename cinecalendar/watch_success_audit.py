from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta


AUDIT_ACTIONS = (
    "chosen",
    "skip_today",
    "trailer_opened",
    "stremio_opened",
    "play_opened",  # legacy 2.7.0 URL handoff
    "playback_confirmed",
    "watched",
)


def build_watch_success_audit(db, days: int = 90) -> dict:
    """Summarize the real local recommendation-to-watch funnel without inventing probabilities.

    The audit deliberately uses observed actions only. It is intended for diagnostics and future
    model tuning: we can see whether recommendations are merely opened, actually started, watched,
    or skipped. No data leaves the local SQLite database.
    """
    days = max(1, min(int(days or 90), 3650))
    start_day = (date.today() - timedelta(days=days - 1)).isoformat()
    marks = ",".join("?" for _ in AUDIT_ACTIONS)
    with db.connect() as con:
        rows = con.execute(
            f"""SELECT movie_id,context_date,recommended_at,action,id
                FROM recommendation_history
                WHERE action IN ({marks}) AND context_date>=?
                ORDER BY id ASC""",
            (*AUDIT_ACTIONS, start_day),
        ).fetchall()

    funnels: dict[tuple[int, str], list[str]] = defaultdict(list)
    for row in rows:
        day = str(row["context_date"] or "").strip()
        if not day:
            raw = str(row["recommended_at"] or "")
            day = raw[:10] if len(raw) >= 10 else "unknown"
        action = str(row["action"] or "").strip()
        if action == "play_opened":
            # 2.7.0 used this label for URL dispatch, not verified playback.
            action = "stremio_opened"
        funnels[(int(row["movie_id"]), day)].append(action)

    final_counts = {name: 0 for name in (
        "chosen",
        "skip_today",
        "trailer_opened",
        "stremio_opened",
        "playback_confirmed",
        "watched",
    )}
    stremio_attempts = 0
    playback_confirmations = 0
    confirmed_starts = 0
    watched = 0
    skipped = 0
    confirmed_after_stremio = 0
    watched_after_confirmed = 0

    for actions in funnels.values():
        if not actions:
            continue
        final = actions[-1]
        if final in final_counts:
            final_counts[final] += 1
        has_stremio = "stremio_opened" in actions
        has_playback_confirmation = "playback_confirmed" in actions
        has_watched = "watched" in actions
        has_confirmed_start = has_playback_confirmation or has_watched
        if has_stremio:
            stremio_attempts += 1
        if has_playback_confirmation:
            playback_confirmations += 1
        if has_confirmed_start:
            confirmed_starts += 1
        if has_watched:
            watched += 1
        if final == "skip_today":
            skipped += 1
        if has_stremio and has_confirmed_start:
            confirmed_after_stremio += 1
        if has_playback_confirmation and has_watched:
            watched_after_confirmed += 1

    def ratio(num: int, den: int):
        return round(num / den, 4) if den > 0 else None

    return {
        "window_days": days,
        "start_date": start_day,
        "funnels": len(funnels),
        "raw_action_rows": len(rows),
        "final_outcomes": final_counts,
        "stremio_attempts": stremio_attempts,
        "playback_confirmations": playback_confirmations,
        "confirmed_starts": confirmed_starts,
        "watched": watched,
        "skipped": skipped,
        "stremio_to_confirmed_rate": ratio(confirmed_after_stremio, stremio_attempts),
        "confirmed_to_watched_rate": ratio(watched_after_confirmed, playback_confirmations),
        "watch_rate_per_funnel": ratio(watched, len(funnels)),
        "skip_rate_per_funnel": ratio(skipped, len(funnels)),
        "enough_data_for_tuning": len(funnels) >= 20 and confirmed_starts >= 5,
    }
