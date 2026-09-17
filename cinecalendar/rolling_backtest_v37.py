from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import math
import tempfile

from .availability_guard_v37 import availability_engine_class
from .db import Database
from .recommendation_backtest import (
    HoldoutRating,
    _build_engine,
    _sqlite_backup,
    _wait_for_als,
    candidate_recall_metrics,
    quality_verdict,
    ranking_quality_metrics,
    visible_outcome_metrics,
)


BACKTEST_VERSION = "rolling-temporal-v3.7.0"


@dataclass(frozen=True)
class TemporalWindowV37:
    index: int
    training_count: int
    holdout: tuple[HoldoutRating, ...]
    future_rating_ids: tuple[int, ...]
    cutoff_date: str

    @property
    def holdout_count(self) -> int:
        return len(self.holdout)


def _ordered_rows(db: Database):
    with db.connect() as con:
        return con.execute(
            """SELECT r.id AS rating_id,m.id AS movie_id,m.imdb_id,r.rating,
                      COALESCE(r.date_rated,r.updated_at,'') AS date_key
               FROM ratings r JOIN movies m ON m.id=r.movie_id
               WHERE m.imdb_id IS NOT NULL AND m.imdb_id<>''
               ORDER BY COALESCE(r.date_rated,r.updated_at,'') ASC,r.id ASC"""
        ).fetchall()


def rolling_windows(
    db: Database,
    *,
    minimum_train: int = 80,
    minimum_holdout: int = 30,
    desired_folds: int = 3,
    maximum_holdout: int = 160,
) -> list[TemporalWindowV37]:
    """Build recent, non-overlapping temporal windows with no future-rating leakage.

    Each fold trains only on ratings strictly before its holdout slice. Ratings belonging to the
    fold itself *and every later rating* are removed from the copied training DB. This makes the
    folds genuinely different historical snapshots instead of overlapping views of the same newest
    ratings.
    """
    rows = list(_ordered_rows(db))
    total = len(rows)
    available = max(0, total - int(minimum_train))
    fold_count = min(max(0, int(desired_folds)), available // max(1, int(minimum_holdout)))
    if fold_count < 2:
        return []

    holdout_size = min(
        max(1, int(maximum_holdout)),
        max(int(minimum_holdout), available // fold_count),
    )
    first_start = total - fold_count * holdout_size
    if first_start < int(minimum_train):
        first_start = int(minimum_train)
        usable = total - first_start
        holdout_size = usable // fold_count
    if holdout_size < int(minimum_holdout):
        return []

    windows: list[TemporalWindowV37] = []
    for fold in range(fold_count):
        start = first_start + fold * holdout_size
        end = start + holdout_size
        if fold == fold_count - 1:
            end = min(total, end)
        slice_rows = rows[start:end]
        if len(slice_rows) < int(minimum_holdout):
            continue
        holdout = tuple(
            HoldoutRating(
                movie_id=int(row["movie_id"]),
                imdb_id=str(row["imdb_id"]),
                rating=int(row["rating"]),
                date_rated=str(row["date_key"] or ""),
            )
            for row in slice_rows
        )
        future_ids = tuple(int(row["rating_id"]) for row in rows[start:])
        cutoff = min(
            (str(row["date_key"] or "")[:10] for row in slice_rows if str(row["date_key"] or "")),
            default="",
        )
        windows.append(
            TemporalWindowV37(
                index=fold + 1,
                training_count=start,
                holdout=holdout,
                future_rating_ids=future_ids,
                cutoff_date=cutoff,
            )
        )
    return windows


def _remove_future(db: Database, window: TemporalWindowV37) -> None:
    ids = list(window.future_rating_ids)
    with db.tx() as con:
        for start in range(0, len(ids), 700):
            chunk = ids[start:start + 700]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            con.execute(f"DELETE FROM ratings WHERE id IN ({marks})", tuple(chunk))
        if window.cutoff_date:
            cutoff = window.cutoff_date
            con.execute("DELETE FROM feedback WHERE substr(created_at,1,10)>=?", (cutoff,))
            con.execute("DELETE FROM recommendation_history WHERE context_date>=?", (cutoff,))
            con.execute("DELETE FROM watchlist WHERE substr(added_at,1,10)>=?", (cutoff,))


def _candidate_imdb_ids(engine, eval_date: date, test_db: Database, candidate_limit: int) -> list[str]:
    rowids = engine._balanced_candidate_ids(eval_date, max(100, int(candidate_limit)))
    out: list[str] = []
    with test_db.connect() as con:
        for start in range(0, len(rowids), 700):
            chunk = rowids[start:start + 700]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT id,imdb_id FROM movies WHERE id IN ({marks})",
                tuple(chunk),
            ).fetchall()
            by_id = {int(row["id"]): str(row["imdb_id"] or "") for row in rows}
            out.extend(by_id.get(int(mid), "") for mid in chunk)
    return [iid for iid in out if iid]


def run_window_backtest(
    db_path: str | Path,
    window: TemporalWindowV37,
    *,
    engine_cls,
    candidate_limit: int = 2200,
    final_limit: int = 100,
    als_timeout: float = 180.0,
) -> dict:
    source_path = Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    with tempfile.TemporaryDirectory(prefix="cinecalendar-rolling37-") as tmp:
        temp_path = Path(tmp) / "cinecalendar.db"
        _sqlite_backup(source_path, temp_path)
        test_db = Database(temp_path)
        _remove_future(test_db, window)
        try:
            eval_date = date.fromisoformat(window.cutoff_date) if window.cutoff_date else date.today()
        except ValueError:
            eval_date = date.today()

        # Historical evaluation must not give either side access to films known to release after
        # the simulated day. Apply the same guard used by 3.7 production to baseline and challenger.
        evaluated_cls = availability_engine_class(engine_cls)
        engine = _build_engine(evaluated_cls, test_db)
        _wait_for_als(engine.collaborative, als_timeout)
        candidate_imdb = _candidate_imdb_ids(engine, eval_date, test_db, candidate_limit)
        engine.adaptive.status()

        final = engine.recommend(
            when=eval_date,
            count=max(10, int(final_limit)),
            record=False,
            candidate_limit=max(100, int(candidate_limit)),
        )
        final_imdb = [str(rec.movie.imdb_id or "") for rec in final if rec.movie.imdb_id]
        top3 = engine.recommend(
            when=eval_date,
            count=3,
            record=False,
            candidate_limit=max(100, int(candidate_limit)),
        )
        top3_imdb = [str(rec.movie.imdb_id or "") for rec in top3 if rec.movie.imdb_id]

        holdout = list(window.holdout)
        return {
            "backtest_version": BACKTEST_VERSION,
            "engine": str(getattr(engine_cls, "__name__", "engine")),
            "evaluated_engine": str(getattr(evaluated_cls, "__name__", "engine")),
            "availability_guard": True,
            "window_index": int(window.index),
            "cutoff_date": window.cutoff_date,
            "training_rating_count": int(window.training_count),
            "holdout_count": int(window.holdout_count),
            "candidate_limit": int(candidate_limit),
            "final_limit": int(final_limit),
            "candidate_recall": candidate_recall_metrics(candidate_imdb, holdout),
            "candidate_quality": ranking_quality_metrics(
                candidate_imdb,
                holdout,
                cutoffs=tuple(k for k in (50, 100, 250, 500, 1000) if k <= max(100, int(candidate_limit))),
            ),
            "final_ranking": candidate_recall_metrics(
                final_imdb,
                holdout,
                cutoffs=tuple(k for k in (3, 10, 25, 50, 100) if k <= max(10, int(final_limit))),
            ),
            "final_quality": ranking_quality_metrics(
                final_imdb,
                holdout,
                cutoffs=tuple(k for k in (3, 10, 25, 50, 100) if k <= max(10, int(final_limit))),
            ),
            "top3_quality": ranking_quality_metrics(top3_imdb, holdout, cutoffs=(3,)),
            "top3_outcomes": visible_outcome_metrics(top3_imdb, holdout, k=3),
            "top3_gate": engine.quality_gate_status(),
            "candidate_generation": engine.candidate_generation_status(),
            "adaptive": engine.adaptive.status(),
        }


def strict_rolling_verdict(folds: list[dict]) -> dict:
    verdicts = [dict(fold.get("verdict") or {}) for fold in folds]
    if len(verdicts) < 2:
        return {
            "approved": False,
            "reason": "insufficient_nonoverlapping_windows",
            "fold_count": len(verdicts),
        }

    deltas = [float(v.get("composite_delta", 0.0) or 0.0) for v in verdicts]
    ndcg = [float(v.get("ndcg25_delta", 0.0) or 0.0) for v in verdicts]
    c8 = [float(v.get("candidate_8_plus_delta", 0.0) or 0.0) for v in verdicts]
    c9 = [float(v.get("candidate_9_plus_delta", 0.0) or 0.0) for v in verdicts]
    bad = [float(v.get("final_dislike_delta", 0.0) or 0.0) for v in verdicts]

    mean_delta = sum(deltas) / len(deltas)
    positive = sum(value > 0.0 for value in deltas)
    required_positive = max(2, math.ceil(len(deltas) * 2.0 / 3.0))
    guards = {
        "no_bad_fold": min(deltas) >= -0.005,
        "candidate_8_plus_stable": min(c8) >= -0.010,
        "candidate_9_plus_stable": min(c9) >= -0.015,
        "ndcg25_stable": min(ndcg) >= -0.005,
        "dislike_exposure_stable": max(bad) <= 0.010,
        "positive_majority": positive >= required_positive,
    }
    meaningful = any(
        delta >= 0.012 or nd >= 0.010 or eight >= 0.010 or nine >= 0.010
        for delta, nd, eight, nine in zip(deltas, ndcg, c8, c9)
    )
    approved = bool(all(guards.values()) and meaningful and mean_delta >= 0.008)
    return {
        "approved": approved,
        "fold_count": len(verdicts),
        "positive_folds": positive,
        "required_positive_folds": required_positive,
        "mean_composite_delta": round(mean_delta, 8),
        "worst_composite_delta": round(min(deltas), 8),
        "mean_ndcg25_delta": round(sum(ndcg) / len(ndcg), 8),
        "mean_candidate_8_plus_delta": round(sum(c8) / len(c8), 8),
        "mean_candidate_9_plus_delta": round(sum(c9) / len(c9), 8),
        "worst_dislike_delta": round(max(bad), 8),
        "minimum_mean_gain": 0.008,
        "meaningful_improvement": meaningful,
        "guardrails": guards,
        "selection_score": round(
            mean_delta + 0.20 * (sum(ndcg) / len(ndcg)) - 0.25 * max(0.0, max(bad)),
            8,
        ),
    }


def compare_on_windows(
    db_path: str | Path,
    windows: list[TemporalWindowV37],
    *,
    baseline_cls,
    challenger_cls,
    candidate_limit: int = 2200,
    final_limit: int = 100,
    als_timeout: float = 180.0,
    baseline_reports: list[dict] | None = None,
) -> dict:
    baselines = list(baseline_reports or [])
    if not baselines:
        baselines = [
            run_window_backtest(
                db_path,
                window,
                engine_cls=baseline_cls,
                candidate_limit=candidate_limit,
                final_limit=final_limit,
                als_timeout=als_timeout,
            )
            for window in windows
        ]
    if len(baselines) != len(windows):
        raise ValueError("baseline report/window count mismatch")

    folds: list[dict] = []
    for window, baseline in zip(windows, baselines):
        challenger = run_window_backtest(
            db_path,
            window,
            engine_cls=challenger_cls,
            candidate_limit=candidate_limit,
            final_limit=final_limit,
            als_timeout=als_timeout,
        )
        verdict = quality_verdict(baseline, challenger, min_gain=0.0)
        folds.append(
            {
                "window_index": window.index,
                "cutoff_date": window.cutoff_date,
                "training_count": window.training_count,
                "holdout_count": window.holdout_count,
                "baseline": baseline,
                "challenger": challenger,
                "verdict": verdict,
            }
        )
    return {
        "backtest_version": BACKTEST_VERSION,
        "baseline_engine": str(getattr(baseline_cls, "__name__", "baseline")),
        "challenger_engine": str(getattr(challenger_cls, "__name__", "challenger")),
        "folds": folds,
        "aggregate": strict_rolling_verdict(folds),
    }
