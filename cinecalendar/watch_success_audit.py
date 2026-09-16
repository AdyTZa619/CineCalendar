from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .trust_audit import (
    OUTCOME_ACTIONS,
    build_trust_outcome_audit,
    ensure_trust_audit_schema,
)


def _ratio(num: int, den: int):
    return round(num / den, 4) if den > 0 else None


def build_watch_success_audit(db, days: int = 90) -> dict:
    """Summarize the local recommendation-to-watch funnel per concrete exposure.

    v3.3 treats every immutable recommendation root as a separate funnel. New action rows are linked
    through exposure_history_id. Legacy unlinked actions are recovered only when one unambiguous
    same-day exposure exists; otherwise they are reported rather than merged by movie + day.
    """
    days = max(1, min(int(days or 90), 3650))
    start_day = (date.today() - timedelta(days=days - 1)).isoformat()
    marks = ",".join("?" for _ in OUTCOME_ACTIONS)

    with db.connect() as con:
        ensure_trust_audit_schema(con)
        exposures = con.execute(
            """SELECT h.id,h.movie_id,h.context_date,h.recommended_at,h.slot,h.action
               FROM recommendation_history h
               WHERE h.context_date>=?
                 AND h.exposure_history_id IS NULL
                 AND h.slot NOT IN ('decision_action','watch_success_v3')
                 AND (
                       h.action IS NULL
                       OR EXISTS(
                           SELECT 1 FROM recommendation_trust_audit t
                           WHERE t.history_id=h.id
                       )
                     )
               ORDER BY h.id ASC""",
            (start_day,),
        ).fetchall()
        action_rows = con.execute(
            f"""SELECT id,movie_id,context_date,recommended_at,action,exposure_history_id
                FROM recommendation_history
                WHERE action IN ({marks}) AND context_date>=?
                ORDER BY id ASC""",
            (*OUTCOME_ACTIONS, start_day),
        ).fetchall()

    exposure_ids = {int(row["id"]) for row in exposures}
    by_movie_day: dict[tuple[int, str], list[int]] = defaultdict(list)
    for row in exposures:
        by_movie_day[(int(row["movie_id"]), str(row["context_date"] or ""))].append(int(row["id"]))

    actions_by_exposure: dict[int, list[str]] = defaultdict(list)
    legacy_funnels: dict[tuple[int, str], list[str]] = defaultdict(list)
    exact_linked_actions = 0
    legacy_linked_actions = 0
    ambiguous_unlinked_actions = 0
    orphan_unlinked_actions = 0

    for row in action_rows:
        action = str(row["action"] or "").strip()
        if action == "play_opened":
            action = "stremio_opened"

        parent = row["exposure_history_id"]
        if parent is not None:
            parent = int(parent)
            if parent in exposure_ids:
                actions_by_exposure[parent].append(action)
                exact_linked_actions += 1
            else:
                orphan_unlinked_actions += 1
            continue

        row_id = int(row["id"])
        if row_id in exposure_ids:
            # Compatibility with v3.2 roots mutated by chosen/skip.
            actions_by_exposure[row_id].append(action)
            exact_linked_actions += 1
            continue

        day = str(row["context_date"] or "").strip()
        if not day:
            raw = str(row["recommended_at"] or "")
            day = raw[:10] if len(raw) >= 10 else "unknown"
        candidates = by_movie_day.get((int(row["movie_id"]), day), [])
        if len(candidates) == 1:
            actions_by_exposure[candidates[0]].append(action)
            legacy_linked_actions += 1
        elif len(candidates) > 1:
            ambiguous_unlinked_actions += 1
        else:
            # Pre-v3.3 databases can contain only action rows, with no immutable root exposure at
            # all. Preserve those historical funnels by movie/day only when there is *no* candidate
            # root to confuse them with. When real roots exist, ambiguity is reported instead.
            legacy_funnels[(int(row["movie_id"]), day)].append(action)
            legacy_linked_actions += 1

    final_counts = {
        name: 0
        for name in (
            "chosen",
            "skip_today",
            "trailer_opened",
            "stremio_opened",
            "playback_confirmed",
            "watched",
            "none",
        )
    }
    stremio_attempts = 0
    playback_confirmations = 0
    confirmed_starts = 0
    watched = 0
    skipped = 0
    engaged_exposures = 0
    confirmed_after_stremio = 0
    watched_after_confirmed = 0

    def consume(actions: list[str]) -> None:
        nonlocal engaged_exposures, stremio_attempts, playback_confirmations, confirmed_starts
        nonlocal watched, skipped, confirmed_after_stremio, watched_after_confirmed
        if not actions:
            return
        engaged_exposures += 1
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

    for exposure in exposures:
        actions = actions_by_exposure.get(int(exposure["id"]), [])
        if not actions:
            final_counts["none"] += 1
            continue
        consume(actions)

    # Historical databases that never had exposure roots still remain auditable, but their legacy
    # movie/day grouping is kept explicitly separate from the exact v3.3 exposure funnels.
    for actions in legacy_funnels.values():
        consume(actions)

    exposure_count = len(exposures)
    legacy_funnel_count = len(legacy_funnels)
    auditable_funnel_count = exposure_count + legacy_funnel_count
    return {
        "window_days": days,
        "start_date": start_day,
        "exposures": exposure_count,
        "legacy_funnels": legacy_funnel_count,
        # Backwards-compatible key; now means engaged concrete exposures, never movie/day buckets.
        "funnels": engaged_exposures,
        "raw_action_rows": len(action_rows),
        "final_outcomes": final_counts,
        "stremio_attempts": stremio_attempts,
        "playback_confirmations": playback_confirmations,
        "confirmed_starts": confirmed_starts,
        "watched": watched,
        "skipped": skipped,
        "stremio_to_confirmed_rate": _ratio(confirmed_after_stremio, stremio_attempts),
        "confirmed_to_watched_rate": _ratio(watched_after_confirmed, playback_confirmations),
        "watch_rate_per_funnel": _ratio(watched, engaged_exposures),
        "watch_rate_per_exposure": _ratio(watched, exposure_count),
        "skip_rate_per_funnel": _ratio(skipped, engaged_exposures),
        "skip_rate_per_exposure": _ratio(skipped, exposure_count),
        "exact_linked_actions": exact_linked_actions,
        "legacy_linked_actions": legacy_linked_actions,
        "ambiguous_unlinked_actions": ambiguous_unlinked_actions,
        "orphan_unlinked_actions": orphan_unlinked_actions,
        "enough_data_for_tuning": auditable_funnel_count >= 20 and confirmed_starts >= 5,
        "trust_gate": build_trust_outcome_audit(db, days=days),
    }
