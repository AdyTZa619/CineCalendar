from __future__ import annotations

from pathlib import Path

from .db import Database
from .recommender_v16 import FastRecommendationEngineV16
from .rolling_backtest_v37 import (
    comparison_from_reports,
    rolling_windows,
    run_window_backtest_group,
)
from .util import utcnow_iso
from .v5_event_replay import aggregate_event_reports, event_replay_windows
from .v5_knowledge import V5KnowledgeBase
from .v5_lab import V5LabRecommendationEngine
from .v5_personal_ranker import PersonalUtilityRankerV5
from .v5_shadow_ranker import V5ShadowRankedEngine


V5_EVALUATION_VERSION = "v5-evaluation-alpha1"


def _metric(payload: dict, key: str):
    value = payload.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_guard(baseline: dict, challenger: dict) -> dict:
    b8 = _metric(baseline, "top10_8_plus_recall")
    c8 = _metric(challenger, "top10_8_plus_recall")
    b9 = _metric(baseline, "top10_9_plus_recall")
    c9 = _metric(challenger, "top10_9_plus_recall")
    bbad = _metric(baseline, "top10_dislike_rate")
    cbad = _metric(challenger, "top10_dislike_rate")

    checks = {
        "top10_8_plus_not_worse": b8 is None or c8 is None or c8 >= b8 - 0.005,
        "top10_9_plus_not_worse": b9 is None or c9 is None or c9 >= b9 - 0.010,
        "top10_dislike_not_worse": bbad is None or cbad is None or cbad <= bbad + 0.010,
    }
    return {
        "passed": bool(all(checks.values())),
        "checks": checks,
        "deltas": {
            "top10_8_plus": round(c8 - b8, 6) if b8 is not None and c8 is not None else None,
            "top10_9_plus": round(c9 - b9, 6) if b9 is not None and c9 is not None else None,
            "top10_dislike": round(cbad - bbad, 6) if bbad is not None and cbad is not None else None,
        },
    }


def run_v5_evaluation(
    db: Database,
    *,
    progress=None,
    rolling_folds: int = 3,
    event_days: int = 4,
    candidate_limit: int = 1400,
    final_limit: int = 50,
    als_timeout: float = 150.0,
) -> dict:
    """Compare current V16, V5 retrieval-only, and V5 + validated utility ranker.

    The source DB is never edited by replay. Each historical window uses the existing managed
    temporary SQLite snapshot path and is deleted before moving to the next window, keeping peak
    temporary disk usage bounded to roughly one database copy rather than accumulating backtests.
    """
    progress = progress or (lambda _message: None)
    source = Path(db.path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    knowledge = V5KnowledgeBase(db).status()
    if not bool(knowledge.get("ready_for_rich_ranker")):
        raise RuntimeError("Profilul V5 nu are încă suficiente date factuale pentru evaluarea rankerului.")

    progress("V5 Lab: verific modelul shadow pe istoricul complet…")
    ranker_status = PersonalUtilityRankerV5(db).status()

    windows = rolling_windows(
        db,
        minimum_train=180,
        minimum_holdout=60,
        desired_folds=max(2, int(rolling_folds)),
        maximum_holdout=160,
    )
    if len(windows) < 2:
        raise RuntimeError("Nu există suficiente ferestre temporale pentru comparația V16 vs V5.")

    baseline_reports = []
    retrieval_reports = []
    ranked_reports = []
    classes = [
        FastRecommendationEngineV16,
        V5LabRecommendationEngine,
        V5ShadowRankedEngine,
    ]

    for index, window in enumerate(windows, 1):
        progress(
            f"V5 Lab: replay temporal {index}/{len(windows)} • "
            f"{window.cutoff_date or 'dată istorică'}"
        )
        reports = run_window_backtest_group(
            source,
            window,
            engine_classes=classes,
            candidate_limit=max(500, int(candidate_limit)),
            final_limit=max(25, int(final_limit)),
            als_timeout=max(20.0, float(als_timeout)),
        )
        baseline_reports.append(reports[0])
        retrieval_reports.append(reports[1])
        ranked_reports.append(reports[2])

    retrieval_comparison = comparison_from_reports(
        windows,
        baseline_cls=FastRecommendationEngineV16,
        challenger_cls=V5LabRecommendationEngine,
        baseline_reports=baseline_reports,
        challenger_reports=retrieval_reports,
    )
    ranked_comparison = comparison_from_reports(
        windows,
        baseline_cls=FastRecommendationEngineV16,
        challenger_cls=V5ShadowRankedEngine,
        baseline_reports=baseline_reports,
        challenger_reports=ranked_reports,
    )

    event_payload = {
        "selection": {"available_days": 0, "selected_days": [], "window_count": 0},
        "baseline": {},
        "retrieval": {},
        "ranked": {},
        "ranked_guard": {"passed": False, "checks": {}, "deltas": {}},
    }

    selection = event_replay_windows(
        db,
        desired_days=max(2, int(event_days)),
        minimum_train=300,
        informative_only=True,
    )
    if selection.windows:
        e_baseline = []
        e_retrieval = []
        e_ranked = []
        for index, window in enumerate(selection.windows, 1):
            progress(
                f"V5 Lab: replay pe zi reală {index}/{len(selection.windows)} • "
                f"{window.cutoff_date}"
            )
            reports = run_window_backtest_group(
                source,
                window,
                engine_classes=classes,
                candidate_limit=max(500, int(candidate_limit)),
                final_limit=max(25, int(final_limit)),
                als_timeout=max(20.0, float(als_timeout)),
            )
            e_baseline.append(reports[0])
            e_retrieval.append(reports[1])
            e_ranked.append(reports[2])

        baseline_event = aggregate_event_reports(e_baseline)
        retrieval_event = aggregate_event_reports(e_retrieval)
        ranked_event = aggregate_event_reports(e_ranked)
        event_payload = {
            "selection": selection.as_dict(),
            "baseline": baseline_event,
            "retrieval": retrieval_event,
            "ranked": ranked_event,
            "ranked_guard": _event_guard(baseline_event, ranked_event),
        }

    rolling_ranked = dict(ranked_comparison.get("aggregate") or {})
    rolling_retrieval = dict(retrieval_comparison.get("aggregate") or {})
    ranker_validated = bool(ranker_status.get("validated"))
    event_guard_passed = bool((event_payload.get("ranked_guard") or {}).get("passed"))
    eligible = bool(
        ranker_validated
        and rolling_ranked.get("approved")
        and event_guard_passed
    )

    report = {
        "version": V5_EVALUATION_VERSION,
        "generated_at": utcnow_iso(),
        "source_db": str(source),
        "knowledge": knowledge,
        "ranker_shadow": ranker_status,
        "rolling": {
            "fold_count": len(windows),
            "retrieval_only": retrieval_comparison,
            "ranked": ranked_comparison,
        },
        "event_replay": event_payload,
        "decision": {
            "ranker_validated": ranker_validated,
            "retrieval_rolling_approved": bool(rolling_retrieval.get("approved")),
            "ranked_rolling_approved": bool(rolling_ranked.get("approved")),
            "event_guard_passed": event_guard_passed,
            "eligible_for_visible_alpha_trial": eligible,
            "visible_ranking_changed": False,
            "reason": (
                "V5 poate trece la un trial vizibil controlat în Alpha."
                if eligible else
                "V5 rămâne shadow; nu sunt încă îndeplinite toate gardurile de promovare."
            ),
        },
    }
    db.set_setting("v5_evaluation_report", report)
    db.set_setting("v5_evaluation_last_success", report["generated_at"])
    progress("V5 Lab: evaluarea s-a terminat; recomandările vizibile au rămas neschimbate.")
    return report
