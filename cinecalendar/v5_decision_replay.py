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
from .v5_lab import DiscoveryRecommendationEngine
from .rolling_backtest_v37 import _remove_future, rolling_windows
from .temp_workspaces import backtest_storage_guard, managed_temp_workspace
from .v5_visible_trial import AlphaTrialRecommender, V5VisibleTrialEngine20


DECISION_REPLAY_VERSION = "v5-decision-replay-alpha2-three-engine"


def decision_replay_guard(
    folds: list[dict],
    challenger_key: str = "v5_20",
    challenger_label: str = "Adaptiv",
) -> dict:
    """A sparse/unknown future outcome cannot promote a visible engine."""
    keys = ("v16", str(challenger_key))
    counts = {
        name: {"matched": 0, "liked_8_plus": 0, "disliked_4_minus": 0}
        for name in keys
    }
    for fold in folds:
        for name in keys:
            matched = (fold.get(name) or {}).get("matched") or []
            counts[name]["matched"] += len(matched)
            counts[name]["liked_8_plus"] += sum(int(item["rating"]) >= 8 for item in matched)
            counts[name]["disliked_4_minus"] += sum(int(item["rating"]) <= 4 for item in matched)

    baseline, challenger = counts["v16"], counts[str(challenger_key)]
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
        str(challenger_key): challenger,
        "challenger": str(challenger_key),
        "challenger_label": str(challenger_label),
        "reason": (
            "Prea puține ratinguri ulterioare cunoscute printre cele trei opțiuni afișate."
            if not informative else
            f"{challenger_label} a găsit mai multe filme apreciate fără a crește expunerea la ratinguri 1–4."
            if passed else
            f"{challenger_label} nu a demonstrat încă un câștig la cele trei opțiuni afișate."
        ),
    }


def run_visible_decision_replay(
    db_path: str | Path, *, desired_folds: int = 3, als_timeout: float = 150.0,
    progress=None, baseline_cls=FastRecommendationEngineV16,
    discovery_cls=DiscoveryRecommendationEngine,
    adaptive_cls=V5VisibleTrialEngine20,
) -> dict:
    """Replay the exact Alpha decision path on separate, past-only SQLite copies.

    The later ratings are used only for scoring the chosen three, never to build the
    historical profile. This is the first decision of each historical day, before
    any same-day feedback. Current UI filters are replayed as configured; historical
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
        progress(f"Comparare motor: Ce văd acum? {index}/{len(windows)} • {window.cutoff_date}")
        with managed_temp_workspace("cinecalendar-rolling37-") as tmp:
            temp_path = tmp / "cinecalendar.db"
            _sqlite_backup(source, temp_path)
            temp_db = Database(temp_path)
            _remove_future(temp_db, window)
            eval_date = date.fromisoformat(window.cutoff_date)
            v16 = _build_engine(availability_engine_class(baseline_cls), temp_db)
            discovery = _build_engine(availability_engine_class(discovery_cls), temp_db)
            adaptive = _build_engine(availability_engine_class(adaptive_cls), temp_db)
            for engine in (v16, discovery, adaptive):
                _wait_for_als(engine.collaborative, als_timeout)
            trial = AlphaTrialRecommender(temp_db, v16, adaptive)
            # Replay the Home call at the start of that date. Same-day feedback and
            # session skips have not happened yet. Stabil and Adaptiv are frozen by the
            # same trial proxy; Descoperire receives the exact same recent exclusions.
            decision_mode = str(temp_db.get_setting("decision_mode", "decide") or "decide")
            recent_exclusions = trial._recent_decision_exclusions(eval_date)
            trial.preview_decision_pick(eval_date, set(), decision_mode, contextual_feedback=())
            pair = trial._round_cache["decision"]
            d_primary, d_backups = discovery.decision_pick(
                eval_date,
                set(recent_exclusions),
                decision_mode,
                contextual_feedback=(),
            )
            discovery_recs = ([d_primary] if d_primary else []) + list(d_backups or [])
            with closing(temp_db.connect()) as con:
                train_count = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
            fold = {
                "date": window.cutoff_date,
                "holdout_count": window.holdout_count,
                "training_rating_count": train_count,
                "decision_mode": decision_mode,
            }
            for name, recs in (
                ("v16", pair["v16"]),
                ("discovery", discovery_recs),
                ("v5_20", pair["v5"]),
            ):
                chosen = [str(rec.movie.imdb_id or "") for rec in list(recs)[:3]]
                fold[name] = visible_outcome_metrics(chosen, list(window.holdout), k=3)
                fold[name]["imdb_ids"] = chosen
            folds.append(fold)
    discovery_guard = decision_replay_guard(folds, "discovery", "Descoperire")
    adaptive_guard = decision_replay_guard(folds, "v5_20", "Adaptiv")
    return {
        "version": DECISION_REPLAY_VERSION,
        "method": "first decision of day / same recent exclusions / current UI settings",
        "baseline_engine": getattr(baseline_cls, "__name__", str(baseline_cls)),
        "discovery_engine": getattr(discovery_cls, "__name__", str(discovery_cls)),
        "adaptive_engine": getattr(adaptive_cls, "__name__", str(adaptive_cls)),
        "folds": folds,
        "guards": {
            "discovery": discovery_guard,
            "adaptive": adaptive_guard,
        },
        "guard": adaptive_guard,
    }
