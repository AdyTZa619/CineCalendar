from __future__ import annotations

from pathlib import Path

from .db import Database
from .recommender_v16 import FastRecommendationEngineV16, recommendation_engine_identity
from .rolling_backtest_v37 import (
    comparison_from_reports,
    rolling_windows,
    run_window_backtest_group,
)
from .util import utcnow_iso
from .v5_event_replay import aggregate_event_reports, event_replay_windows
from .v5_knowledge import V5KnowledgeBase
from .v5_lab import (
    DiscoveryRecommendationEngine,
    discovery_engine_class,
)
from .v5_personal_ranker import PersonalUtilityRankerV5
from .v5_rating_snapshot import rating_history_snapshot
from .v5_decision_replay import run_visible_decision_replay
from .v5_shadow_ranker import adaptive_engine_class


V5_EVALUATION_VERSION = "v5-evaluation-alpha4-discovery"


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

    positive_targets = max(
        int(baseline.get("liked_8_plus", 0) or 0),
        int(challenger.get("liked_8_plus", 0) or 0),
    )
    # Candidate hits only prove retrieval worked. For an external *ranking* guard we need at least
    # one positive target to reach the evaluated final list. Otherwise a 0-vs-0 ranking comparison
    # is inconclusive and must never authorize a visible trial.
    positive_final_hits = (
        int(baseline.get("top50_8_plus_hits", 0) or 0)
        + int(baseline.get("top50_9_plus_hits", 0) or 0)
        + int(challenger.get("top50_8_plus_hits", 0) or 0)
        + int(challenger.get("top50_9_plus_hits", 0) or 0)
    )
    informative = bool(positive_targets > 0 and positive_final_hits > 0)

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
            "Replay-ul pe zile reale are hit-uri pozitive în Top50 și poate valida rankingul final."
            if informative else
            "Replay-ul pe zile reale este NECONCLUDENT: pool-ul poate găsi filme bune, dar nici Stabil, nici varianta comparată nu au pus vreun 8+/9+ în Top50."
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


def _choose_discovery_variant(variants: dict[str, dict]) -> tuple[str, dict]:
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
    event_days: int = 12,
    candidate_limit: int = 1800,
    final_limit: int = 50,
    als_timeout: float = 150.0,
    stable_engine_cls: type | None = None,
) -> dict:
    """Compare Stable, conservative Discovery, and a guarded sweep of Adaptive weights.

    The source DB is never edited. Historical windows use one temporary SQLite snapshot at a time,
    which is deleted before the next window. The sweep may choose a stronger ranker weight for
    evaluation, but nothing here changes the live Alpha ranking.
    """
    progress = progress or (lambda _message: None)
    source = Path(db.path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    rating_snapshot = rating_history_snapshot(db)
    stable_cls = stable_engine_cls or FastRecommendationEngineV16
    if not isinstance(stable_cls, type) or not issubclass(stable_cls, FastRecommendationEngineV16):
        stable_cls = FastRecommendationEngineV16
    stable_identity = recommendation_engine_identity(stable_cls)

    knowledge = V5KnowledgeBase(db).status()

    # Descoperire changes retrieval only and must remain evaluable even when the richer
    # personal model lacks enough factual metadata. Adaptiv simply stays inactive/blocked.
    progress("Comparare motor: verific modelul Adaptiv pe istoricul complet…")
    ranker_status = PersonalUtilityRankerV5(db).status()

    windows = rolling_windows(
        db,
        minimum_train=180,
        minimum_holdout=60,
        desired_folds=max(2, int(rolling_folds)),
        maximum_holdout=160,
    )
    if len(windows) < 2:
        raise RuntimeError("Nu există suficiente ferestre temporale pentru comparația Stabil vs variantele noi.")

    discovery_classes = [
        discovery_engine_class(stable_cls, "strict"),
        discovery_engine_class(stable_cls, "balanced"),
        discovery_engine_class(stable_cls, "wide"),
    ]
    discovery_classes_by_name = {
        str(getattr(cls, "DISCOVERY_VARIANT", cls.__name__)): cls
        for cls in discovery_classes
    }

    baseline_reports: list[dict] = []
    discovery_variant_reports: dict[str, list[dict]] = {
        label: [] for label in discovery_classes_by_name
    }

    # Phase 1: keep the exact Stable scorer fixed and choose only the retrieval frontier.
    for index, window in enumerate(windows, 1):
        progress(
            f"Comparare motor: Descoperire {index}/{len(windows)} • "
            f"{window.cutoff_date or 'dată istorică'}"
        )
        reports = run_window_backtest_group(
            source,
            window,
            engine_classes=[stable_cls, *discovery_classes],
            candidate_limit=max(500, int(candidate_limit)),
            final_limit=max(25, int(final_limit)),
            als_timeout=max(20.0, float(als_timeout)),
        )
        baseline_reports.append(reports[0])
        for offset, cls in enumerate(discovery_classes, start=1):
            label = str(getattr(cls, "DISCOVERY_VARIANT", cls.__name__))
            discovery_variant_reports[label].append(reports[offset])

    discovery_variants: dict[str, dict] = {}
    for label, cls in discovery_classes_by_name.items():
        comparison = comparison_from_reports(
            windows,
            baseline_cls=stable_cls,
            challenger_cls=cls,
            baseline_reports=baseline_reports,
            challenger_reports=discovery_variant_reports[label],
        )
        discovery_variants[label] = {
            "engine": cls.__name__,
            "extra_share": float(getattr(cls, "DISCOVERY_EXTRA_SHARE", 0.0)),
            "trusted_single_rank_limit": int(
                getattr(cls, "DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT", 0)
            ),
            "comparison": comparison,
        }
    selected_discovery_name, selected_discovery_payload = _choose_discovery_variant(
        discovery_variants
    )
    selected_discovery_cls = discovery_classes_by_name.get(
        selected_discovery_name,
        discovery_engine_class(stable_cls, "balanced"),
    )
    selected_discovery_engine_name = selected_discovery_cls.__name__
    discovery_comparison = dict(selected_discovery_payload.get("comparison") or {})

    # Phase 2: only after retrieval is fixed do we test personal re-ordering weights.
    ranker_classes = [
        adaptive_engine_class(stable_cls, selected_discovery_name, None),
        adaptive_engine_class(stable_cls, selected_discovery_name, 0.10),
        adaptive_engine_class(stable_cls, selected_discovery_name, 0.15),
        adaptive_engine_class(stable_cls, selected_discovery_name, 0.20),
    ]
    variant_reports: dict[str, list[dict]] = {
        _variant_label(cls): [] for cls in ranker_classes
    }
    for index, window in enumerate(windows, 1):
        progress(
            f"Comparare motor: Adaptiv {index}/{len(windows)} • "
            f"{window.cutoff_date or 'dată istorică'}"
        )
        reports = run_window_backtest_group(
            source,
            window,
            engine_classes=ranker_classes,
            candidate_limit=max(500, int(candidate_limit)),
            final_limit=max(25, int(final_limit)),
            als_timeout=max(20.0, float(als_timeout)),
        )
        for offset, cls in enumerate(ranker_classes):
            variant_reports[_variant_label(cls)].append(reports[offset])

    ranked_variants: dict[str, dict] = {}
    for cls in ranker_classes:
        label = _variant_label(cls)
        comparison = comparison_from_reports(
            windows,
            baseline_cls=stable_cls,
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
        adaptive_engine_class(stable_cls, selected_discovery_name, None),
    )
    selected_comparison = dict(selected_payload.get("comparison") or {})

    event_payload = {
        "selection": {"available_days": 0, "selected_days": [], "window_count": 0},
        "baseline": {},
        "retrieval": {},
        "discovery": {},
        "ranked": {},
        "discovery_guard": {
            "passed": False,
            "informative": False,
            "checks": {},
            "deltas": {},
            "reason": "Replay-ul pe zile reale nu a rulat.",
        },
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
        e_discovery: list[dict] = []
        e_ranked: list[dict] = []
        event_classes = [stable_cls, selected_discovery_cls, selected_cls]
        for index, window in enumerate(selection.windows, 1):
            progress(
                f"Comparare motor: validare pe zile reale {index}/{len(selection.windows)} • "
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
            e_discovery.append(reports[1])
            e_ranked.append(reports[2])

        baseline_event = aggregate_event_reports(e_baseline)
        discovery_event = aggregate_event_reports(e_discovery)
        ranked_event = aggregate_event_reports(e_ranked)
        event_payload = {
            "selection": selection.as_dict(),
            "baseline": baseline_event,
            "retrieval": discovery_event,
            "discovery": discovery_event,
            "ranked": ranked_event,
            "discovery_guard": _event_guard(baseline_event, discovery_event),
            "ranked_guard": _event_guard(baseline_event, ranked_event),
        }

    decision_replay = run_visible_decision_replay(
        source,
        desired_folds=max(2, int(rolling_folds)),
        als_timeout=als_timeout,
        progress=progress,
        baseline_cls=stable_cls,
        discovery_cls=selected_discovery_cls,
        adaptive_cls=selected_cls,
    )
    decision_guards = dict(decision_replay.get("guards") or {})
    discovery_decision_guard = dict(decision_guards.get("discovery") or {})
    adaptive_decision_guard = dict(
        decision_guards.get("adaptive")
        or decision_replay.get("guard")
        or {}
    )
    decision_guard = adaptive_decision_guard
    rolling_discovery = dict(discovery_comparison.get("aggregate") or {})
    rolling_selected = dict(selected_comparison.get("aggregate") or {})
    ranker_validated = bool(ranker_status.get("validated"))
    discovery_event_guard = dict(event_payload.get("discovery_guard") or {})
    discovery_event_guard_passed = bool(discovery_event_guard.get("passed"))
    event_guard = dict(event_payload.get("ranked_guard") or {})
    event_guard_passed = bool(event_guard.get("passed"))
    discovery_candidate_for_stable = bool(
        rolling_discovery.get("approved")
        and discovery_event_guard_passed
        and discovery_decision_guard.get("passed")
    )
    eligible = bool(
        selected_name == "20%"
        and ranker_validated
        and rolling_selected.get("approved")
        and event_guard_passed
        and decision_guard.get("passed")
    )

    report = {
        "version": V5_EVALUATION_VERSION,
        "generated_at": utcnow_iso(),
        "rating_snapshot": rating_snapshot,
        "source_db": str(source),
        "stable_engine": {
            "class": stable_cls.__name__,
            "identity": stable_identity,
        },
        "knowledge": knowledge,
        "ranker_shadow": ranker_status,
        "rolling": {
            "fold_count": len(windows),
            "retrieval_only": discovery_comparison,
            "discovery": discovery_comparison,
            "discovery_variants": discovery_variants,
            "selected_discovery_variant": selected_discovery_name,
            "selected_discovery_engine": selected_discovery_engine_name,
            "ranked_variants": ranked_variants,
            "selected_variant": selected_name,
            "selected_ranked": selected_comparison,
        },
        "event_replay": event_payload,
        "visible_decision_replay": decision_replay,
        "decision": {
            "ranker_validated": ranker_validated,
            "retrieval_rolling_approved": bool(rolling_discovery.get("approved")),
            "discovery_rolling_approved": bool(rolling_discovery.get("approved")),
            "selected_discovery_variant": selected_discovery_name,
            "selected_discovery_engine": selected_discovery_engine_name,
            "discovery_event_guard_passed": discovery_event_guard_passed,
            "discovery_visible_decision_guard_passed": bool(discovery_decision_guard.get("passed")),
            "discovery_candidate_for_stable": discovery_candidate_for_stable,
            "selected_variant": selected_name,
            "selected_variant_engine": selected_engine_name,
            "selected_rolling_approved": bool(rolling_selected.get("approved")),
            "event_guard_passed": event_guard_passed,
            "event_guard_informative": bool(event_guard.get("informative")),
            "visible_decision_guard_passed": bool(decision_guard.get("passed")),
            "eligible_for_visible_alpha_trial": eligible,
            "visible_ranking_changed": False,
            "reason": (
                f"Adaptiv {selected_name} poate trece la un test vizibil controlat în Alpha."
                if eligible else
                f"Adaptiv {selected_name or '—'} rămâne în test; gardurile de promovare nu sunt încă toate îndeplinite."
            ),
        },
    }
    if rating_history_snapshot(db) != rating_snapshot:
        raise RuntimeError("Ratingurile s-au schimbat în timpul evaluării; rulează din nou V5 Lab pentru un raport coerent.")
    db.set_setting("v5_evaluation_report", report)
    db.set_setting("v5_evaluation_last_success", report["generated_at"])
    progress("Comparare motor: evaluarea s-a terminat; recomandările vizibile au rămas neschimbate.")
    return report
