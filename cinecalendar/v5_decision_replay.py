from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from .availability_guard_v37 import availability_engine_class
from .db import Database
from .recommendation_backtest import (
    _build_engine,
    _sqlite_backup,
    _wait_for_als,
    visible_outcome_metrics,
)
from .recommender_v16 import FastRecommendationEngineV16
from .rolling_backtest_v37 import _remove_future, rolling_windows
from .temp_workspaces import backtest_storage_guard, managed_temp_workspace
from .v5_visible_trial import AlphaTrialRecommender, V5VisibleTrialEngine20


DECISION_REPLAY_VERSION = "v5-decision-replay-alpha1"


def decision_replay_guard(folds: list[dict]) -> dict:
    """A sparse/unknown future outcome cannot promote a visible ranker."""
    counts = {name: {"matched": 0, "liked_8_plus": 0, "disliked_4_minus": 0}
              for name in ("v16", "v5_20")}
    for fold in folds:
        for name in counts:
            matched = (fold.get(name) or {}).get("matched") or []
            counts[name]["matched"] += len(matched)
            counts[name]["liked_8_plus"] += sum(int(item["rating"]) >= 8 for item in matched)
            counts[name]["disliked_4_minus"] += sum(int(item["rating"]) <= 4 for item in matched)

    baseline, challenger = counts["v16"], counts["v5_20"]
    informative = bool(
        len(folds) >= 2
        and baseline["matched"] + challenger["matched"] >= 3
        and baseline["liked_8_plus"] + challenger["liked_8_plus"] > 0
    )
    passed = bool(
        informative
        and challenger["liked_8_plus"] > baseline["liked_8_plus"]
        and challenger["disliked_4_minus"] <= baseline["disliked_4_minus"]
    )
    return {
        "informative": informative,
        "passed": passed,
        "fold_count": len(folds),
        "v16": baseline,
        "v5_20": challenger,
        "reason": (
            "Prea puține ratinguri ulterioare cunoscute printre cele trei opțiuni afișate."
            if not informative else
            "V5 a găsit mai multe filme apreciate fără a crește expunerea la ratinguri 1–4."
            if passed else
            "V5 nu a demonstrat un câștig la cele trei opțiuni afișate."
        ),
    }


def run_visible_decision_replay(
    db_path: str | Path, *, desired_folds: int = 3, als_timeout: float = 150.0,
    progress=None,
) -> dict:
    """Replay the exact Alpha decision path on separate, past-only SQLite copies.

    The later ratings are used only for scoring the chosen three, never to build the
    historical profile. Current UI filters are replayed as configured; historical
    preference/filter settings cannot be reconstructed from the ratings alone.
    """
    source = Path(db_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    backtest_storage_guard(source)
    windows = rolling_windows(
        Database(source), minimum_train=180, minimum_holdout=60,
        desired_folds=max(2, int(desired_folds)), maximum_holdout=160,
    )
    if len(windows) < 2:
        raise RuntimeError("Nu există suficiente ferestre temporale pentru decizia vizibilă.")
    progress = progress or (lambda _message: None)
    folds = []
    for index, window in enumerate(windows, 1):
        progress(f"V5 Lab: Ce văd acum? {index}/{len(windows)} • {window.cutoff_date}")
        with managed_temp_workspace("cinecalendar-rolling37-") as tmp:
            temp_path = tmp / "cinecalendar.db"
            _sqlite_backup(source, temp_path)
            temp_db = Database(temp_path)
            _remove_future(temp_db, window)
            eval_date = date.fromisoformat(window.cutoff_date)
            v16 = _build_engine(availability_engine_class(FastRecommendationEngineV16), temp_db)
            v5 = _build_engine(availability_engine_class(V5VisibleTrialEngine20), temp_db)
            for engine in (v16, v5):
                _wait_for_als(engine.collaborative, als_timeout)
            trial = AlphaTrialRecommender(temp_db, v16, v5)
            # This mirrors Home's call, including its prior-week exclusions, runtime,
            # mood, and any day-specific genre filter. The trial computes both lists.
            decision_mode = str(temp_db.get_setting("decision_mode", "decide") or "decide")
            trial.decision_pick(eval_date, set(), decision_mode)
            pair = trial._round_cache["decision"]
            with closing(temp_db.connect()) as con:
                train_count = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
            fold = {
                "date": window.cutoff_date,
                "holdout_count": window.holdout_count,
                "training_rating_count": train_count,
                "decision_mode": decision_mode,
            }
            for name, key in (("v16", "v16"), ("v5_20", "v5")):
                chosen = [str(rec.movie.imdb_id or "") for rec in pair[key][:3]]
                fold[name] = visible_outcome_metrics(chosen, list(window.holdout), k=3)
                fold[name]["imdb_ids"] = chosen
            folds.append(fold)
    return {
        "version": DECISION_REPLAY_VERSION,
        "method": "AlphaTrialRecommender.decision_pick / current UI settings",
        "folds": folds,
        "guard": decision_replay_guard(folds),
    }
