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
from .v5_shadow_ranker import (
    V5ShadowRankedEngine,
    V5ShadowRankedEngine10,
    V5ShadowRankedEngine15,
    V5ShadowRankedEngine20,
)


V5_EVALUATION_VERSION = "v5-evaluation-alpha2"


def _metric(payload: dict, key: str):
    value = payload.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _not_worse(baseline: float | None, challenger: float | None, tolerance: float) -> bool:
    return baseline is None or challenger is None or challenger >= baseline - abs(float(tolerance))


def _not_higher(baseline: float | None, challenger: float | None, tolerance: float) -> bool:
    return baseline is None or challenger is None or challenger <= baseline + abs(float(tolerance))


def _event_guard(baseline: dict, challenger: dict) -> dict:
    bc8 = _metric(baseline, "candidate_8_plus_recall")
    cc8 = _metric(challenger, "candidate_8_plus_recall")
    bc9 = _metric(baseline, "candidate_9_plus_recall")
    cc9 = _metric(challenger, "candidate_9_plus_recall")
    b25_8 = _metric(baseline, "top25_8_plus_recall")
    c25_8 = _metric(challenger, "top25_8_plus_recall")
    b25_9 = _metric(baseline, "top25_9_plus_recall")
    c25_9 = _metric(challenger, "top25_9_plus_recall")
    bbad = _metric(baseline, "top25_dislike_rate")
    cbad = _metric(challenger, "top25_dislike_rate")

    positive_candidate_hits = (
        int(baseline.get("candidate_8_plus_hits", 0) or 0)
        + int(baseline.get("candidate_9_plus_hits", 0) or 0)
        + int(challenger.get("candidate_8_plus_hits", 0) or 0)
        + int(challenger.get("candidate_9_plus_hits", 0) or 0)
    )
    positive_targets = max(
        int(baseline.get("liked_8_plus", 0) or 0),
        int(challenger.get("liked_8_plus", 0) or 0),
    )
    informative = bool(positive_targets > 0 and positive_candidate_hits > 0)

    checks = {
        "candidate_8_plus_not_worse": _not_worse(bc8, cc8, 0.010),
        "candidate_9_plus_not_worse": _not_worse(bc9, cc9, 0.015),
        "top25_8_plus_not_worse": _not_worse(b25_8, c25_8, 0.010),
        "top25_9_plus_not_worse": _not_worse(b25_9, c25_9, 0.015),
        "top25_dislike_not_worse": _not_higher(bbad, cbad, 0.010),
    }
    return {
        "passed": bool(informative and all(checks.values())),
        "informative": informative,
        "checks": checks,
        "deltas": {
            "candidate_8_plus": round(cc8 - bc8, 6) if bc8 is not None and cc8 is not None else None,
            "candidate_9_plus": round(cc9 - bc9, 6) if bc9 is not None and cc9 is not None else None,
            "top25_8_plus": round(c25_8 - b25_8, 6) if b25_8 is not None and c25_8 is not None else None,
            "top25_9_plus": round(c25_9 - b25_9, 6) if b25_9 is not None and c25_9 is not None else None,
            "top25_dislike": round(cbad - bbad, 6) if bbad is not None and cbad is not None else None,
        },
        "reason": (
            "Replay-ul pe zile reale are suficiente hit-uri pentru gardul extern."
            if informative else
            "Replay-ul pe zile reale este neconcludent: nici candidate pool-ul nu a prins suficiente rezultate pozitive."
        ),
    }


def _variant_label(engine_cls: type) -> str:
    override = getattr(engine_cls, "SHADOW_BLEND_OVERRIDE", None)
    if override is None:
        return "learned"
    return f"{int(round(float(override) * 100.0))}%"


def _choose_ranker_variant(variants: dict[str, dict]) -> tuple[str, dict]:
    if not variants:
        return "", {}
    approved = [
        (name, payload)
        for name, payload in variants.items()
        if bool(((payload.get("comparison") or {}).get("aggregate") or {}).get("approved"))
    ]
    pool = approved or list(variants.items())
    return max(
        pool,
        key=lambda item: float(
            (((item[1].get("comparison") or {}).get("aggregate") or {}).get("selection_score", -999.0))
            or -999.0
        ),
    )


def run_v5_evaluation(
    db: Database,
    *,
    progress=None,
    rolling_folds: int = 3,
    event_days: int = 8,
    candidate_limit: int = 1800,
    final_limit: int = 50,
    als_timeout: float = 150.0,
) -> dict:
    """Compare V16, V5 retrieval, and a guarded offline sweep of V5 ranker weights.

    The source DB is never edited. Historical windows use one temporary SQLite snapshot at a time,
    which is deleted before the next window. The sweep may choose a stronger ranker weight for
    evaluation, but nothing here changes the live Alpha ranking.
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

    ranker_classes = [
        V5ShadowRankedEngine,
        V5ShadowRankedEngine10,
        V5ShadowRankedEngine15,
        V5ShadowRankedEngine20,
    ]
    classes = [FastRecommendationEngineV16, V5LabRecommendationEngine, *ranker_classes]

    baseline_reports: list[dict] = []
    retrieval_reports: list[dict] = []
    variant_reports: dict[str, list[dict]] = {
        _variant_label(cls): [] for cls in ranker_classes
    }

    for index, window in enumerate(windows, 1):
        progress(
            f"V5 Lab: sweep temporal {index}/{len(windows)} • "
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
        for offset, cls in enumerate(ranker_classes, start=2):
            variant_reports[_variant_label(cls)].append(reports[offset])

    retrieval_comparison = comparison_from_reports(
        windows,
        baseline_cls=FastRecommendationEngineV16,
        challenger_cls=V5LabRecommendationEngine,
        baseline_reports=baseline_reports,
        challenger_reports=retrieval_reports,
    )

    ranked_variants: dict[str, dict] = {}
    for cls in ranker_classes:
        label = _variant_label(cls)
        comparison = comparison_from_reports(
            windows,
            baseline_cls=FastRecommendationEngineV16,
            challenger_cls=cls,
            baseline_reports=baseline_reports,
            challenger_reports=variant_reports[label],
        )
        ranked_variants[label] = {
            "engine": cls.__name__,
            "blend_override": getattr(cls, "SHADOW_BLEND_OVERRIDE", None),
            "comparison": comparison,
        }

    selected_name, selected_payload = _choose_ranker_variant(ranked_variants)
    selected_engine_name = str(selected_payload.get("engine") or "")
    selected_cls = next(
        (cls for cls in ranker_classes if cls.__name__ == selected_engine_name),
        V5ShadowRankedEngine,
    )
    selected_comparison = dict(selected_payload.get("comparison") or {})

    event_payload = {
        "selection": {"available_days": 0, "selected_days": [], "window_count": 0},
        "baseline": {},
        "retrieval": {},
        "ranked": {},
        "ranked_guard": {
            "passed": False,
            "informative": False,
            "checks": {},
            "deltas": {},
            "reason": "Replay-ul pe zile reale nu a rulat.",
        },
    }

    selection = event_replay_windows(
        db,
        desired_days=max(4, int(event_days)),
        minimum_train=300,
        informative_only=True,
    )
    if selection.windows:
        e_baseline: list[dict] = []
        e_retrieval: list[dict] = []
        e_ranked: list[dict] = []
        event_classes = [FastRecommendationEngineV16, V5LabRecommendationEngine, selected_cls]
        for index, window in enumerate(selection.windows, 1):
            progress(
                f"V5 Lab: validare externă {index}/{len(selection.windows)} • "
                f"{window.cutoff_date} • ranker {selected_name}"
            )
            reports = run_window_backtest_group(
                source,
                window,
                engine_classes=event_classes,
                candidate_limit=max(500, int(candidate_limit)),
                final_limit=max(50, int(final_limit)),
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

    rolling_retrieval = dict(retrieval_comparison.get("aggregate") or {})
    rolling_selected = dict(selected_comparison.get("aggregate") or {})
    ranker_validated = bool(ranker_status.get("validated"))
    event_guard = dict(event_payload.get("ranked_guard") or {})
    event_guard_passed = bool(event_guard.get("passed"))
    eligible = bool(
        ranker_validated
        and rolling_selected.get("approved")
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
            "ranked_variants": ranked_variants,
            "selected_variant": selected_name,
            "selected_ranked": selected_comparison,
        },
        "event_replay": event_payload,
        "decision": {
            "ranker_validated": ranker_validated,
            "retrieval_rolling_approved": bool(rolling_retrieval.get("approved")),
            "selected_variant": selected_name,
            "selected_variant_engine": selected_engine_name,
            "selected_rolling_approved": bool(rolling_selected.get("approved")),
            "event_guard_passed": event_guard_passed,
            "event_guard_informative": bool(event_guard.get("informative")),
            "eligible_for_visible_alpha_trial": eligible,
            "visible_ranking_changed": False,
            "reason": (
                f"V5 ranker {selected_name} poate trece la un trial vizibil controlat în Alpha."
                if eligible else
                f"V5 ranker {selected_name or '—'} rămâne shadow; gardurile de promovare nu sunt încă toate îndeplinite."
            ),
        },
    }
    db.set_setting("v5_evaluation_report", report)
    db.set_setting("v5_evaluation_last_success", report["generated_at"])
    progress("V5 Lab: evaluarea s-a terminat; recomandările vizibile au rămas neschimbate.")
    return report
