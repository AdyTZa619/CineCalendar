from __future__ import annotations

"""Evaluation helpers for full-catalog retrieval.

4.14 keeps the challenger out of production. It makes live shadow evidence honest about
exposure bias and adds a temporal replay that evaluates a conservative fixed-budget blend on
historical ratings without leaking future feedback into training.
"""

from dataclasses import asdict
from datetime import date
from pathlib import Path
import math
import random

from .db import Database
from .recommendation_backtest import (
    _build_engine,
    _sqlite_backup,
    _wait_for_als,
    candidate_recall_metrics,
    ranking_quality_metrics,
)
from .rolling_backtest_v37 import TemporalWindowV37, _remove_future, rolling_windows
from .temp_workspaces import backtest_storage_guard, managed_temp_workspace


FULL_CATALOG_EVALUATION_VERSION = "full-catalog-evaluation-v4.14.0"
LIVE_EVALUATION_WINDOW_DAYS = 120
MIN_CHALLENGER_RATINGS_FOR_REVIEW = 20
MIN_PAIRED_RUNS_FOR_REVIEW = 8
REPLAY_INJECTION_SHARE = 0.15
REPLAY_DEFAULT_LIMIT = 600
REPLAY_BOOTSTRAP_SAMPLES = 2000


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round_or_none(value: float | None, digits: int = 6):
    return round(float(value), digits) if value is not None else None


def bootstrap_mean_interval(
    values: list[float] | tuple[float, ...],
    *,
    confidence: float = 0.80,
    samples: int = REPLAY_BOOTSTRAP_SAMPLES,
    seed: int = 4140,
) -> dict:
    data = [float(value) for value in values]
    if not data:
        return {"count": 0, "mean": None, "low": None, "high": None, "confidence": confidence}
    if len(data) == 1:
        value = data[0]
        return {
            "count": 1,
            "mean": round(value, 6),
            "low": round(value, 6),
            "high": round(value, 6),
            "confidence": confidence,
        }
    rng = random.Random(int(seed))
    n = len(data)
    means = []
    for _ in range(max(200, int(samples))):
        means.append(sum(data[rng.randrange(n)] for _idx in range(n)) / n)
    means.sort()
    alpha = max(0.0, min(0.49, (1.0 - float(confidence)) / 2.0))
    lo_idx = min(len(means) - 1, max(0, int(math.floor(alpha * (len(means) - 1)))))
    hi_idx = min(len(means) - 1, max(0, int(math.ceil((1.0 - alpha) * (len(means) - 1)))))
    return {
        "count": n,
        "mean": round(sum(data) / n, 6),
        "low": round(means[lo_idx], 6),
        "high": round(means[hi_idx], 6),
        "confidence": confidence,
    }


def _live_rows(
    db: Database,
    window_days: int = LIVE_EVALUATION_WINDOW_DAYS,
    challenger_version: str | None = None,
):
    version_clause = " AND run.challenger_version=?" if challenger_version else ""
    params: list[object] = [f"+{max(1, int(window_days))} days"]
    if challenger_version:
        params.append(str(challenger_version))
    with db.connect() as con:
        rows = con.execute(
            """SELECT run.id AS run_id,run.context_date,i.source,i.movie_id,i.rank_position,
                      r.rating,substr(COALESCE(r.date_rated,r.updated_at,''),1,10) AS rating_day
               FROM retrieval_shadow_runs run
               JOIN retrieval_shadow_items i ON i.run_id=run.id
               JOIN ratings r ON r.movie_id=i.movie_id
               WHERE substr(COALESCE(r.date_rated,r.updated_at,''),1,10)>=run.context_date
                 AND date(substr(COALESCE(r.date_rated,r.updated_at,''),1,10))
                     <=date(run.context_date, ?)"""
            + version_clause
            + " ORDER BY run.id,i.source,i.rank_position",
            tuple(params),
        ).fetchall()
    return rows


def live_shadow_metrics(
    db: Database,
    window_days: int = LIVE_EVALUATION_WINDOW_DAYS,
    *,
    challenger_version: str | None = None,
) -> dict:
    """Return observational live metrics without pretending the hidden challenger had equal exposure.

    A movie is counted once per source globally. Paired deltas are calculated only for runs where
    each side later received at least one rating, and titles present on both sides of the same run
    are removed from that run-level comparison.
    """
    rows = list(_live_rows(db, window_days, challenger_version))
    per_source: dict[str, dict[int, int]] = {"baseline": {}, "challenger": {}}
    per_run: dict[int, dict[str, dict[int, int]]] = {}
    for row in rows:
        source = str(row["source"])
        movie_id = int(row["movie_id"])
        rating = int(row["rating"])
        per_source.setdefault(source, {}).setdefault(movie_id, rating)
        run = per_run.setdefault(int(row["run_id"]), {"baseline": {}, "challenger": {}})
        run.setdefault(source, {}).setdefault(movie_id, rating)

    def source_summary(source: str) -> dict:
        ratings = list(per_source.get(source, {}).values())
        return {
            "rated": len(ratings),
            "average_rating": _round_or_none(_mean([float(x) for x in ratings]), 4),
            "liked": sum(value >= 8 for value in ratings),
            "liked_rate": _round_or_none(
                (sum(value >= 8 for value in ratings) / len(ratings)) if ratings else None,
                6,
            ),
        }

    rating_deltas: list[float] = []
    liked_deltas: list[float] = []
    paired_details: list[dict] = []
    for run_id, sources in sorted(per_run.items()):
        base = dict(sources.get("baseline") or {})
        challenger = dict(sources.get("challenger") or {})
        overlap = set(base).intersection(challenger)
        for movie_id in overlap:
            base.pop(movie_id, None)
            challenger.pop(movie_id, None)
        if not base or not challenger:
            continue
        base_values = list(base.values())
        challenger_values = list(challenger.values())
        base_mean = sum(base_values) / len(base_values)
        challenger_mean = sum(challenger_values) / len(challenger_values)
        base_liked = sum(value >= 8 for value in base_values) / len(base_values)
        challenger_liked = sum(value >= 8 for value in challenger_values) / len(challenger_values)
        rating_deltas.append(challenger_mean - base_mean)
        liked_deltas.append(challenger_liked - base_liked)
        paired_details.append(
            {
                "run_id": run_id,
                "baseline_rated": len(base_values),
                "challenger_rated": len(challenger_values),
                "rating_delta": round(challenger_mean - base_mean, 6),
                "liked_rate_delta": round(challenger_liked - base_liked, 6),
                "overlap_excluded": len(overlap),
            }
        )

    baseline = source_summary("baseline")
    challenger = source_summary("challenger")
    paired = {
        "runs": len(paired_details),
        "positive_rating_runs": sum(value > 0 for value in rating_deltas),
        "rating_delta_interval": bootstrap_mean_interval(rating_deltas, seed=4141),
        "liked_rate_delta_interval": bootstrap_mean_interval(liked_deltas, seed=4142),
        "details": paired_details[-25:],
    }
    enough_observational = bool(
        challenger["rated"] >= MIN_CHALLENGER_RATINGS_FOR_REVIEW
        and paired["runs"] >= MIN_PAIRED_RUNS_FOR_REVIEW
    )
    return {
        "version": FULL_CATALOG_EVALUATION_VERSION,
        "window_days": max(1, int(window_days)),
        "challenger_version": str(challenger_version or ""),
        "baseline": baseline,
        "challenger": challenger,
        "paired": paired,
        "eligible_for_offline_review": enough_observational,
        "minimum_challenger_ratings": MIN_CHALLENGER_RATINGS_FOR_REVIEW,
        "minimum_paired_runs": MIN_PAIRED_RUNS_FOR_REVIEW,
        "promotion_allowed_from_live_shadow": False,
        "limitation": "hidden_challenger_has_unequal_exposure",
    }


def _movie_ids_to_imdb(db: Database, movie_ids: list[int]) -> list[str]:
    if not movie_ids:
        return []
    out: list[str] = []
    with db.connect() as con:
        for start in range(0, len(movie_ids), 700):
            chunk = [int(value) for value in movie_ids[start:start + 700]]
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT id,imdb_id FROM movies WHERE id IN ({marks})", tuple(chunk)
            ).fetchall()
            mapping = {int(row["id"]): str(row["imdb_id"] or "") for row in rows}
            out.extend(mapping.get(movie_id, "") for movie_id in chunk)
    return [value for value in out if value]


def fixed_budget_mix(
    baseline: list[int],
    challenger: list[int],
    *,
    limit: int,
    share: float = REPLAY_INJECTION_SHARE,
) -> list[int]:
    """Inject a bounded challenger share while preserving one fixed candidate budget."""
    limit = max(1, int(limit))
    challenger_slots = max(
        1,
        min(limit - 1 if limit > 1 else 1, int(round(limit * float(share)))),
    )
    baseline_slots = max(0, limit - challenger_slots)
    base = []
    seen: set[int] = set()
    for movie_id in baseline:
        movie_id = int(movie_id)
        if movie_id > 0 and movie_id not in seen:
            seen.add(movie_id)
            base.append(movie_id)
        if len(base) >= baseline_slots:
            break
    challenge = []
    for movie_id in challenger:
        movie_id = int(movie_id)
        if movie_id > 0 and movie_id not in seen:
            seen.add(movie_id)
            challenge.append(movie_id)
        if len(challenge) >= challenger_slots:
            break

    # Spread challenger slots instead of appending them all at the tail, so rank-aware metrics
    # measure the actual conservative blend rather than an artificially baseline-first list.
    result: list[int] = []
    b = c = 0
    stride = max(1, int(round((1.0 - float(share)) / max(0.01, float(share)))))
    while len(result) < limit and (b < len(base) or c < len(challenge)):
        for _ in range(stride):
            if b < len(base) and len(result) < limit:
                result.append(base[b])
                b += 1
        if c < len(challenge) and len(result) < limit:
            result.append(challenge[c])
            c += 1
        if b >= len(base) and c >= len(challenge):
            break
    for source in (base[b:], challenge[c:], baseline, challenger):
        for movie_id in source:
            movie_id = int(movie_id)
            if movie_id not in result and len(result) < limit:
                result.append(movie_id)
    return result[:limit]


def _metric(report: dict, section: str, key: str) -> float:
    try:
        return float((report.get(section) or {}).get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _evaluate_ids(db: Database, movie_ids: list[int], holdout, *, limit: int) -> dict:
    imdb_ids = _movie_ids_to_imdb(db, list(movie_ids)[: max(1, int(limit))])
    cutoffs = tuple(sorted({min(50, limit), min(100, limit), min(250, limit), limit}))
    cutoffs = tuple(value for value in cutoffs if value > 0)
    return {
        "ranked_count": len(imdb_ids),
        "candidate_recall": candidate_recall_metrics(imdb_ids, list(holdout), cutoffs=cutoffs),
        "ranking_quality": ranking_quality_metrics(imdb_ids, list(holdout), cutoffs=cutoffs),
    }


def evaluate_replay_window_db(
    test_db: Database,
    window: TemporalWindowV37,
    *,
    engine_cls,
    candidate_limit: int = REPLAY_DEFAULT_LIMIT,
    als_timeout: float = 180.0,
    injection_share: float = REPLAY_INJECTION_SHARE,
) -> dict:
    try:
        eval_date = date.fromisoformat(window.cutoff_date) if window.cutoff_date else date.today()
    except ValueError:
        eval_date = date.today()
    engine = _build_engine(engine_cls, test_db)
    _wait_for_als(engine.collaborative, als_timeout)
    engine.adaptive.status()

    limit = max(100, int(candidate_limit))
    baseline_ids = list(engine._balanced_candidate_ids(eval_date, limit))[:limit]
    from .full_catalog_shadow_v414 import FullCatalogCandidateGeneratorV414

    generator = FullCatalogCandidateGeneratorV414(test_db, engine.collaborative)
    challenger_items = generator.candidates(limit)
    challenger_ids = [int(item.movie_id) for item in challenger_items]
    blended_ids = fixed_budget_mix(
        baseline_ids,
        challenger_ids,
        limit=limit,
        share=injection_share,
    )

    baseline = _evaluate_ids(test_db, baseline_ids, window.holdout, limit=limit)
    challenger = _evaluate_ids(test_db, challenger_ids, window.holdout, limit=limit)
    blended = _evaluate_ids(test_db, blended_ids, window.holdout, limit=limit)
    key8 = f"recall_8_plus_at_{limit}"
    key9 = f"recall_9_plus_at_{limit}"
    bad = f"dislike_recall_at_{limit}"
    ndcg = f"ndcg_at_{limit}"
    deltas = {
        "recall_8_plus": round(
            _metric(blended, "candidate_recall", key8)
            - _metric(baseline, "candidate_recall", key8),
            8,
        ),
        "recall_9_plus": round(
            _metric(blended, "candidate_recall", key9)
            - _metric(baseline, "candidate_recall", key9),
            8,
        ),
        "dislike_recall": round(
            _metric(blended, "candidate_recall", bad)
            - _metric(baseline, "candidate_recall", bad),
            8,
        ),
        "ndcg": round(
            _metric(blended, "ranking_quality", ndcg)
            - _metric(baseline, "ranking_quality", ndcg),
            8,
        ),
    }
    return {
        "version": FULL_CATALOG_EVALUATION_VERSION,
        "window": asdict(window),
        "cutoff_date": window.cutoff_date,
        "candidate_limit": limit,
        "injection_share": float(injection_share),
        "baseline": baseline,
        "challenger": challenger,
        "blended": blended,
        "deltas": deltas,
        "generator": generator.status(),
    }


def run_full_catalog_replay(
    db_path: str | Path,
    *,
    engine_cls,
    candidate_limit: int = REPLAY_DEFAULT_LIMIT,
    desired_folds: int = 3,
    als_timeout: float = 180.0,
    injection_share: float = REPLAY_INJECTION_SHARE,
) -> dict:
    """Run leakage-safe historical replay. It reports evidence only and never changes production."""
    source_path = Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    backtest_storage_guard(source_path)
    source_db = Database(source_path)
    windows = rolling_windows(source_db, desired_folds=max(2, int(desired_folds)))
    folds: list[dict] = []
    for window in windows:
        with managed_temp_workspace("cinecalendar-fullcatalog414-") as tmp:
            temp_path = tmp / "cinecalendar.db"
            _sqlite_backup(source_path, temp_path)
            test_db = Database(temp_path)
            _remove_future(test_db, window)
            folds.append(
                evaluate_replay_window_db(
                    test_db,
                    window,
                    engine_cls=engine_cls,
                    candidate_limit=candidate_limit,
                    als_timeout=als_timeout,
                    injection_share=injection_share,
                )
            )

    recall8 = [
        float((fold.get("deltas") or {}).get("recall_8_plus", 0.0) or 0.0)
        for fold in folds
    ]
    recall9 = [
        float((fold.get("deltas") or {}).get("recall_9_plus", 0.0) or 0.0)
        for fold in folds
    ]
    dislike = [
        float((fold.get("deltas") or {}).get("dislike_recall", 0.0) or 0.0)
        for fold in folds
    ]
    ndcg = [
        float((fold.get("deltas") or {}).get("ndcg", 0.0) or 0.0)
        for fold in folds
    ]
    enough = len(folds) >= 2
    aggregate = {
        "fold_count": len(folds),
        "enough_windows": enough,
        "recall_8_plus_delta": bootstrap_mean_interval(recall8, seed=4143),
        "recall_9_plus_delta": bootstrap_mean_interval(recall9, seed=4144),
        "dislike_recall_delta": bootstrap_mean_interval(dislike, seed=4145),
        "ndcg_delta": bootstrap_mean_interval(ndcg, seed=4146),
        "positive_recall8_folds": sum(value > 0 for value in recall8),
        "positive_recall9_folds": sum(value > 0 for value in recall9),
        "production_unchanged": True,
        "automatic_promotion": False,
    }
    return {
        "version": FULL_CATALOG_EVALUATION_VERSION,
        "candidate_limit": max(100, int(candidate_limit)),
        "injection_share": float(injection_share),
        "folds": folds,
        "aggregate": aggregate,
    }
