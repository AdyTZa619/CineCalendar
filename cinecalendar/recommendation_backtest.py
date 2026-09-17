from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import json
import math
import sqlite3
import tempfile
import time

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from .calendar_engine_v2 import RichCalendarEngine
from .collaborative_als import CollaborativeALSProvider
from .db import Database
from .recommender_v16 import FastRecommendationEngineV16
from .watch_success_v33 import WatchSuccessIntentLearnerV33


@dataclass(frozen=True)
class HoldoutRating:
    movie_id: int
    imdb_id: str
    rating: int
    date_rated: str


def temporal_holdout(
    db: Database,
    *,
    fraction: float = 0.20,
    min_holdout: int = 60,
    max_holdout: int = 500,
) -> list[HoldoutRating]:
    """Return the newest part of the rating history, preserving chronology."""
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.id AS movie_id,m.imdb_id,r.rating,
                      COALESCE(r.date_rated,r.updated_at,'') AS date_key
               FROM ratings r JOIN movies m ON m.id=r.movie_id
               WHERE m.imdb_id IS NOT NULL AND m.imdb_id<>''
               ORDER BY COALESCE(r.date_rated,r.updated_at,'') ASC, r.id ASC"""
        ).fetchall()
    if len(rows) < 80:
        return []
    wanted = max(int(min_holdout), int(round(len(rows) * max(0.05, min(0.45, float(fraction))))))
    wanted = min(int(max_holdout), wanted, max(1, len(rows) - 40))
    chosen = rows[-wanted:]
    return [
        HoldoutRating(
            movie_id=int(row["movie_id"]),
            imdb_id=str(row["imdb_id"]),
            rating=int(row["rating"]),
            date_rated=str(row["date_key"] or ""),
        )
        for row in chosen
    ]


def _at_k(ranked: list[str], wanted: set[str], k: int) -> float:
    if not wanted:
        return 0.0
    found = len(wanted.intersection(ranked[: max(0, int(k))]))
    return found / float(len(wanted))


def candidate_recall_metrics(
    ranked_imdb_ids: list[str],
    holdout: list[HoldoutRating],
    *,
    cutoffs: tuple[int, ...] = (50, 100, 250, 500, 1000),
) -> dict:
    """Measure whether retrieval can surface films the user later rated highly."""
    ranked = [str(x) for x in ranked_imdb_ids if str(x)]
    rating_by_id = {row.imdb_id: int(row.rating) for row in holdout}
    liked = {iid for iid, rating in rating_by_id.items() if rating >= 8}
    loved = {iid for iid, rating in rating_by_id.items() if rating >= 9}
    disliked = {iid for iid, rating in rating_by_id.items() if rating <= 4}

    out = {
        "holdout": len(holdout),
        "liked_8_plus": len(liked),
        "loved_9_plus": len(loved),
        "disliked_4_minus": len(disliked),
        "ranked_count": len(ranked),
    }
    for cutoff in cutoffs:
        k = int(cutoff)
        out[f"recall_8_plus_at_{k}"] = round(_at_k(ranked, liked, k), 6)
        out[f"recall_9_plus_at_{k}"] = round(_at_k(ranked, loved, k), 6)
        out[f"dislike_recall_at_{k}"] = round(_at_k(ranked, disliked, k), 6)

    matched = [rating_by_id[iid] for iid in ranked if iid in rating_by_id]
    out["matched_holdout"] = len(matched)
    out["matched_mean_rating"] = round(sum(matched) / len(matched), 4) if matched else None
    out["matched_8_plus_share"] = (
        round(sum(1 for value in matched if value >= 8) / len(matched), 6) if matched else None
    )
    out["matched_4_minus_share"] = (
        round(sum(1 for value in matched if value <= 4) / len(matched), 6) if matched else None
    )
    return out


def _gain(rating: int) -> float:
    return max(0.0, float(rating) - 5.0)


def ranking_quality_metrics(
    ranked_imdb_ids: list[str],
    holdout: list[HoldoutRating],
    *,
    cutoffs: tuple[int, ...] = (3, 10, 25, 50, 100),
) -> dict:
    """Rank-aware metrics for hidden future ratings: NDCG, recall, MRR and dislike exposure."""
    ranked = [str(x) for x in ranked_imdb_ids if str(x)]
    ratings = {row.imdb_id: int(row.rating) for row in holdout}
    liked = {iid for iid, value in ratings.items() if value >= 8}
    loved = {iid for iid, value in ratings.items() if value >= 9}
    disliked = {iid for iid, value in ratings.items() if value <= 4}
    ideal_gains = sorted((_gain(value) for value in ratings.values()), reverse=True)

    out: dict[str, object] = {
        "ranked_count": len(ranked),
        "holdout": len(holdout),
        "liked_8_plus": len(liked),
        "loved_9_plus": len(loved),
        "disliked_4_minus": len(disliked),
    }
    first_liked = next((idx for idx, iid in enumerate(ranked, start=1) if iid in liked), None)
    first_loved = next((idx for idx, iid in enumerate(ranked, start=1) if iid in loved), None)
    out["mrr_8_plus"] = round(1.0 / first_liked, 6) if first_liked else 0.0
    out["mrr_9_plus"] = round(1.0 / first_loved, 6) if first_loved else 0.0

    for raw_k in cutoffs:
        k = max(1, int(raw_k))
        top = ranked[:k]
        dcg = 0.0
        for rank, iid in enumerate(top, start=1):
            rel = _gain(ratings.get(iid, 0))
            if rel > 0:
                dcg += (2.0 ** rel - 1.0) / math.log2(rank + 1.0)
        ideal = 0.0
        for rank, rel in enumerate(ideal_gains[:k], start=1):
            if rel > 0:
                ideal += (2.0 ** rel - 1.0) / math.log2(rank + 1.0)
        out[f"ndcg_at_{k}"] = round(dcg / ideal, 6) if ideal > 0 else 0.0
        out[f"recall_8_plus_at_{k}"] = round(_at_k(ranked, liked, k), 6)
        out[f"recall_9_plus_at_{k}"] = round(_at_k(ranked, loved, k), 6)
        out[f"dislike_recall_at_{k}"] = round(_at_k(ranked, disliked, k), 6)
        out[f"hit_8_plus_at_{k}"] = bool(liked.intersection(top))
        out[f"hit_9_plus_at_{k}"] = bool(loved.intersection(top))
    return out


def visible_outcome_metrics(ranked_imdb_ids: list[str], holdout: list[HoldoutRating], *, k: int = 3) -> dict:
    ranked = [str(x) for x in ranked_imdb_ids if str(x)][: max(1, int(k))]
    rating_by_id = {row.imdb_id: int(row.rating) for row in holdout}
    matched = [(iid, rating_by_id[iid]) for iid in ranked if iid in rating_by_id]
    values = [rating for _iid, rating in matched]
    return {
        "k": max(1, int(k)),
        "returned": len(ranked),
        "matched_hidden_future": len(values),
        "coverage": round(len(values) / len(ranked), 6) if ranked else 0.0,
        "mean_actual_rating": round(sum(values) / len(values), 4) if values else None,
        "rated_8_plus_share": round(sum(v >= 8 for v in values) / len(values), 6) if values else None,
        "rated_9_plus_share": round(sum(v >= 9 for v in values) / len(values), 6) if values else None,
        "disaster_4_minus_rate": round(sum(v <= 4 for v in values) / len(values), 6) if values else None,
        "matched": [{"imdb_id": iid, "rating": rating} for iid, rating in matched],
    }


def _sqlite_backup(source: Path, target: Path) -> None:
    source_con = sqlite3.connect(source)
    target_con = sqlite3.connect(target)
    try:
        source_con.backup(target_con)
    finally:
        target_con.close()
        source_con.close()


def _remove_future_signals(db: Database, holdout: list[HoldoutRating]) -> str:
    if not holdout:
        return ""
    holdout_ids = [row.movie_id for row in holdout]
    cutoff = min((row.date_rated[:10] for row in holdout if row.date_rated), default="")
    with db.tx() as con:
        for start in range(0, len(holdout_ids), 700):
            chunk = holdout_ids[start:start + 700]
            marks = ",".join("?" for _ in chunk)
            con.execute(f"DELETE FROM ratings WHERE movie_id IN ({marks})", tuple(chunk))
        if cutoff:
            con.execute("DELETE FROM feedback WHERE substr(created_at,1,10)>=?", (cutoff,))
            con.execute("DELETE FROM recommendation_history WHERE context_date>=?", (cutoff,))
            con.execute("DELETE FROM watchlist WHERE substr(added_at,1,10)>=?", (cutoff,))
    return cutoff


def _wait_for_als(provider: CollaborativeALSProvider, timeout: float) -> None:
    provider.start_background()
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if provider.is_ready():
            return
        status = provider.status()
        if status.get("state") == "error":
            raise RuntimeError(str(status.get("error") or "ALS failed to load"))
        time.sleep(0.20)
    raise TimeoutError("ALS did not become ready before the backtest timeout")


def _engine_name(engine_cls) -> str:
    return str(getattr(engine_cls, "__name__", "engine"))


def _build_engine(engine_cls, db: Database):
    engine = engine_cls(db, RichCalendarEngine())
    engine.adaptive = AdaptivePreferenceLearnerV2(db)
    engine.watch_intent = WatchSuccessIntentLearnerV33(db)
    return engine


def run_local_backtest(
    db_path: str | Path,
    *,
    fraction: float = 0.20,
    candidate_limit: int = 1800,
    final_limit: int = 100,
    als_timeout: float = 180.0,
    engine_cls=FastRecommendationEngineV16,
) -> dict:
    """Backtest one engine against hidden recent ratings on a transactionally copied DB."""
    source_path = Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    source_db = Database(source_path)
    with source_db.connect() as con:
        source_rating_count = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
    holdout = temporal_holdout(source_db, fraction=fraction)
    if not holdout:
        raise RuntimeError("Not enough timestamped IMDb ratings for a temporal holdout")

    with tempfile.TemporaryDirectory(prefix="cinecalendar-backtest-") as tmp:
        temp_path = Path(tmp) / "cinecalendar.db"
        _sqlite_backup(source_path, temp_path)
        test_db = Database(temp_path)
        cutoff = _remove_future_signals(test_db, holdout)
        try:
            eval_date = date.fromisoformat(cutoff) if cutoff else date.today()
        except ValueError:
            eval_date = date.today()

        engine = _build_engine(engine_cls, test_db)
        _wait_for_als(engine.collaborative, als_timeout)

        candidate_rowids = engine._balanced_candidate_ids(eval_date, max(100, int(candidate_limit)))
        candidate_imdb: list[str] = []
        with test_db.connect() as con:
            for start in range(0, len(candidate_rowids), 700):
                chunk = candidate_rowids[start:start + 700]
                if not chunk:
                    continue
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"SELECT id,imdb_id FROM movies WHERE id IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                by_id = {int(row["id"]): str(row["imdb_id"] or "") for row in rows}
                candidate_imdb.extend(by_id.get(int(mid), "") for mid in chunk)
        candidate_imdb = [iid for iid in candidate_imdb if iid]

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
        gate_status = engine.quality_gate_status()

        return {
            "engine": _engine_name(engine_cls),
            "cutoff_date": cutoff,
            "source_rating_count": source_rating_count,
            "training_rating_count": source_rating_count - len(holdout),
            "holdout_fraction": float(fraction),
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
            "top3": candidate_recall_metrics(top3_imdb, holdout, cutoffs=(3,)),
            "top3_quality": ranking_quality_metrics(top3_imdb, holdout, cutoffs=(3,)),
            "top3_outcomes": visible_outcome_metrics(top3_imdb, holdout, k=3),
            "top3_gate": gate_status,
            "top3_roles": engine.role_status() if hasattr(engine, "role_status") else {"enabled": False, "roles": []},
            "candidate_generation": engine.candidate_generation_status(),
            "personal_retrieval": engine.personal_candidates.status(),
            "adaptive": engine.adaptive.status(),
        }


def _metric(payload: dict, path: tuple[str, ...], default: float = 0.0) -> float:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return float(default)
        current = current[key]
    if current is None:
        return float(default)
    try:
        return float(current)
    except (TypeError, ValueError):
        return float(default)


def quality_composite(report: dict) -> float:
    c8 = _metric(report, ("candidate_recall", "recall_8_plus_at_500"))
    c9 = _metric(report, ("candidate_recall", "recall_9_plus_at_500"))
    ndcg25 = _metric(report, ("final_quality", "ndcg_at_25"))
    r8_25 = _metric(report, ("final_quality", "recall_8_plus_at_25"))
    r9_25 = _metric(report, ("final_quality", "recall_9_plus_at_25"))
    mrr8 = _metric(report, ("final_quality", "mrr_8_plus"))
    bad25 = _metric(report, ("final_quality", "dislike_recall_at_25"))
    return round(
        0.25 * c8
        + 0.15 * c9
        + 0.22 * ndcg25
        + 0.14 * r8_25
        + 0.10 * r9_25
        + 0.09 * mrr8
        - 0.05 * bad25,
        8,
    )


def quality_verdict(baseline: dict, challenger: dict, *, min_gain: float = 0.01) -> dict:
    base_score = quality_composite(baseline)
    challenger_score = quality_composite(challenger)
    delta = challenger_score - base_score

    cand8_delta = _metric(challenger, ("candidate_recall", "recall_8_plus_at_500")) - _metric(
        baseline, ("candidate_recall", "recall_8_plus_at_500")
    )
    cand9_delta = _metric(challenger, ("candidate_recall", "recall_9_plus_at_500")) - _metric(
        baseline, ("candidate_recall", "recall_9_plus_at_500")
    )
    bad100_delta = _metric(challenger, ("final_quality", "dislike_recall_at_100")) - _metric(
        baseline, ("final_quality", "dislike_recall_at_100")
    )
    ndcg25_delta = _metric(challenger, ("final_quality", "ndcg_at_25")) - _metric(
        baseline, ("final_quality", "ndcg_at_25")
    )

    guardrails = {
        "candidate_8_plus_no_material_regression": cand8_delta >= -0.03,
        "candidate_9_plus_no_material_regression": cand9_delta >= -0.04,
        "dislike_exposure_no_material_regression": bad100_delta <= 0.03,
    }
    approved = all(guardrails.values()) and delta >= float(min_gain)
    return {
        "approved": bool(approved),
        "baseline_engine": str(baseline.get("engine") or "baseline"),
        "challenger_engine": str(challenger.get("engine") or "challenger"),
        "baseline_composite": base_score,
        "challenger_composite": challenger_score,
        "composite_delta": round(delta, 8),
        "candidate_8_plus_delta": round(cand8_delta, 8),
        "candidate_9_plus_delta": round(cand9_delta, 8),
        "final_dislike_delta": round(bad100_delta, 8),
        "ndcg25_delta": round(ndcg25_delta, 8),
        "minimum_required_gain": float(min_gain),
        "guardrails": guardrails,
    }


def compare_quality_engines(
    db_path: str | Path,
    *,
    fractions: tuple[float, ...] = (0.20,),
    candidate_limit: int = 1800,
    final_limit: int = 100,
    als_timeout: float = 180.0,
    baseline_cls=FastRecommendationEngineV16,
    challenger_cls=None,
) -> dict:
    if challenger_cls is None:
        from .recommender_v17 import FastRecommendationEngineV17
        challenger_cls = FastRecommendationEngineV17

    folds = []
    for fraction in fractions:
        baseline = run_local_backtest(
            db_path,
            fraction=float(fraction),
            candidate_limit=candidate_limit,
            final_limit=final_limit,
            als_timeout=als_timeout,
            engine_cls=baseline_cls,
        )
        challenger = run_local_backtest(
            db_path,
            fraction=float(fraction),
            candidate_limit=candidate_limit,
            final_limit=final_limit,
            als_timeout=als_timeout,
            engine_cls=challenger_cls,
        )
        verdict = quality_verdict(baseline, challenger)
        folds.append({"fraction": float(fraction), "baseline": baseline, "challenger": challenger, "verdict": verdict})

    deltas = [float(fold["verdict"]["composite_delta"]) for fold in folds]
    guardrails_ok = all(all(fold["verdict"]["guardrails"].values()) for fold in folds)
    positive = sum(1 for value in deltas if value > 0)
    mean_delta = sum(deltas) / len(deltas) if deltas else -1.0
    required_mean = 0.01 if len(folds) <= 1 else 0.008
    approved = bool(
        folds
        and guardrails_ok
        and mean_delta >= required_mean
        and positive >= max(1, math.ceil(len(folds) / 2))
    )
    return {
        "baseline_engine": _engine_name(baseline_cls),
        "challenger_engine": _engine_name(challenger_cls),
        "fractions": [float(x) for x in fractions],
        "folds": folds,
        "aggregate": {
            "approved": approved,
            "guardrails_ok": guardrails_ok,
            "positive_folds": positive,
            "fold_count": len(folds),
            "mean_composite_delta": round(mean_delta, 8),
            "minimum_mean_gain": required_mean,
        },
    }


def report_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
