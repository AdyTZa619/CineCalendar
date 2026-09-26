from __future__ import annotations

"""Date-aligned replay for CineCalendar 5.

The legacy rolling benchmark intentionally uses large non-overlapping future blocks. That is useful
for detecting leakage and broad regressions, but it is a poor absolute test for a calendar-aware
product: a recommendation generated on 8 January should not be expected to rank a Christmas film
the user will watch eleven months later.

V5 therefore adds a second, stricter view: replay selected historical rating days. Every rating
from that day and every future rating is hidden, the engine is evaluated using the actual date, and
only the films rated on that day are outcomes. The old rolling benchmark remains a guardrail; this
event replay complements it rather than replacing it.
"""

from dataclasses import dataclass

from .db import Database
from .recommendation_backtest import HoldoutRating
from .rolling_backtest_v37 import TemporalWindowV37


V5_EVENT_REPLAY_VERSION = "v5-event-replay-alpha1"


@dataclass(frozen=True)
class V5EventReplaySelection:
    available_days: int
    selected_days: tuple[str, ...]
    windows: tuple[TemporalWindowV37, ...]

    def as_dict(self) -> dict:
        return {
            "version": V5_EVENT_REPLAY_VERSION,
            "available_days": self.available_days,
            "selected_days": list(self.selected_days),
            "window_count": len(self.windows),
        }


def _rating_rows(db: Database):
    with db.connect() as con:
        return con.execute(
            """SELECT r.id AS rating_id,m.id AS movie_id,m.imdb_id,r.rating,
                      substr(COALESCE(r.date_rated,r.updated_at,''),1,10) AS rating_day
               FROM ratings r JOIN movies m ON m.id=r.movie_id
               WHERE m.imdb_id IS NOT NULL AND m.imdb_id<>''
                 AND substr(COALESCE(r.date_rated,r.updated_at,''),1,10)<>''
               ORDER BY COALESCE(r.date_rated,r.updated_at,'') ASC,r.id ASC"""
        ).fetchall()


def _evenly_spaced(values: list[str], count: int) -> list[str]:
    if not values or count <= 0:
        return []
    if len(values) <= count:
        return list(values)
    if count == 1:
        return [values[-1]]
    positions = {
        round(index * (len(values) - 1) / float(count - 1))
        for index in range(count)
    }
    return [values[index] for index in sorted(positions)]


def event_replay_windows(
    db: Database,
    *,
    desired_days: int = 12,
    minimum_train: int = 80,
    informative_only: bool = True,
    start_date: str = "",
) -> V5EventReplaySelection:
    """Create deterministic date-aligned replay points without looking at model output.

    informative_only keeps days containing at least one 8+ or <=4 rating. This focuses expensive
    replay on decisions that teach the positive/negative boundary while selection remains entirely
    independent of recommendation performance.
    """
    rows = list(_rating_rows(db))
    grouped: dict[str, list] = {}
    for row in rows:
        day = str(row["rating_day"] or "")
        if not day or (start_date and day < str(start_date)):
            continue
        grouped.setdefault(day, []).append(row)

    eligible_days: list[str] = []
    rows_before = 0
    count_by_day: dict[str, int] = {}
    for day in sorted(grouped):
        day_rows = grouped[day]
        count_by_day[day] = rows_before
        informative = any(int(row["rating"]) >= 8 or int(row["rating"]) <= 4 for row in day_rows)
        if rows_before >= int(minimum_train) and (informative or not informative_only):
            eligible_days.append(day)
        rows_before += len(day_rows)

    selected = _evenly_spaced(eligible_days, max(1, int(desired_days)))
    windows: list[TemporalWindowV37] = []
    for index, day in enumerate(selected, 1):
        day_rows = grouped[day]
        holdout = tuple(
            HoldoutRating(
                movie_id=int(row["movie_id"]),
                imdb_id=str(row["imdb_id"]),
                rating=int(row["rating"]),
                date_rated=day,
            )
            for row in day_rows
        )
        future_ids = tuple(
            int(row["rating_id"])
            for row in rows
            if str(row["rating_day"] or "") >= day
        )
        windows.append(
            TemporalWindowV37(
                index=index,
                training_count=int(count_by_day[day]),
                holdout=holdout,
                future_rating_ids=future_ids,
                cutoff_date=day,
            )
        )
    return V5EventReplaySelection(
        available_days=len(eligible_days),
        selected_days=tuple(selected),
        windows=tuple(windows),
    )


def _hit_count(metrics: dict, *, population_key: str, recall_key: str) -> int:
    population = int(metrics.get(population_key, 0) or 0)
    recall = float(metrics.get(recall_key, 0.0) or 0.0)
    return int(round(population * recall))


def aggregate_event_reports(reports: list[dict]) -> dict:
    """Aggregate day-level reports by actual target counts instead of averaging tiny-day rates."""
    out = {
        "version": V5_EVENT_REPLAY_VERSION,
        "days": len(reports),
        "liked_8_plus": 0,
        "loved_9_plus": 0,
        "disliked_4_minus": 0,
        "candidate_8_plus_hits": 0,
        "candidate_9_plus_hits": 0,
        "candidate_dislike_hits": 0,
        "top10_8_plus_hits": 0,
        "top10_9_plus_hits": 0,
        "top10_dislike_hits": 0,
    }
    for report in reports:
        candidate = dict(report.get("candidate_recall") or {})
        final = dict(report.get("final_ranking") or {})
        liked = int(candidate.get("liked_8_plus", 0) or 0)
        loved = int(candidate.get("loved_9_plus", 0) or 0)
        bad = int(candidate.get("disliked_4_minus", 0) or 0)
        out["liked_8_plus"] += liked
        out["loved_9_plus"] += loved
        out["disliked_4_minus"] += bad

        candidate_cutoffs = [
            int(key.rsplit("_", 1)[-1])
            for key in candidate
            if key.startswith("recall_8_plus_at_") and key.rsplit("_", 1)[-1].isdigit()
        ]
        candidate_k = max(candidate_cutoffs) if candidate_cutoffs else 0
        if candidate_k:
            out["candidate_8_plus_hits"] += _hit_count(
                candidate, population_key="liked_8_plus",
                recall_key=f"recall_8_plus_at_{candidate_k}",
            )
            out["candidate_9_plus_hits"] += _hit_count(
                candidate, population_key="loved_9_plus",
                recall_key=f"recall_9_plus_at_{candidate_k}",
            )
            out["candidate_dislike_hits"] += _hit_count(
                candidate, population_key="disliked_4_minus",
                recall_key=f"dislike_recall_at_{candidate_k}",
            )
        if "recall_8_plus_at_10" in final:
            out["top10_8_plus_hits"] += _hit_count(
                final, population_key="liked_8_plus", recall_key="recall_8_plus_at_10"
            )
            out["top10_9_plus_hits"] += _hit_count(
                final, population_key="loved_9_plus", recall_key="recall_9_plus_at_10"
            )
            out["top10_dislike_hits"] += _hit_count(
                final, population_key="disliked_4_minus", recall_key="dislike_recall_at_10"
            )

    out["candidate_8_plus_recall"] = round(
        out["candidate_8_plus_hits"] / out["liked_8_plus"], 6
    ) if out["liked_8_plus"] else None
    out["candidate_9_plus_recall"] = round(
        out["candidate_9_plus_hits"] / out["loved_9_plus"], 6
    ) if out["loved_9_plus"] else None
    out["candidate_dislike_rate"] = round(
        out["candidate_dislike_hits"] / out["disliked_4_minus"], 6
    ) if out["disliked_4_minus"] else None
    out["top10_8_plus_recall"] = round(
        out["top10_8_plus_hits"] / out["liked_8_plus"], 6
    ) if out["liked_8_plus"] else None
    out["top10_9_plus_recall"] = round(
        out["top10_9_plus_hits"] / out["loved_9_plus"], 6
    ) if out["loved_9_plus"] else None
    out["top10_dislike_rate"] = round(
        out["top10_dislike_hits"] / out["disliked_4_minus"], 6
    ) if out["disliked_4_minus"] else None
    return out
