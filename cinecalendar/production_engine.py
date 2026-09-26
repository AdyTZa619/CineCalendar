from __future__ import annotations

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from .availability_guard_v37 import AvailabilityGuardMixinV37, availability_engine_class
from .calendar_engine_v3 import ContextCalendarEngineV35
from .context_recommender_v35 import CONTEXT_RECOMMENDER_VERSION, contextual_engine_class
from .recommender_v16 import FastRecommendationEngineV16, recommendation_engine_identity
from .watch_success_v33 import WatchSuccessIntentLearnerV33
from .personalization_v41 import PERSONALIZATION_V41_VERSION, personalization_engine_class
from .learning_insight_v43 import LEARNING_INSIGHT_VERSION, learning_insight_engine_class


# Ranking identity changes only when recommendation ordering changes.  Operational/UI releases must
# not invalidate a multi-gigabyte personal backtest by themselves.
RANKING_STACK_VERSION = "production-stack-v4.6.0-personal-hybrid"
PRODUCTION_STACK_VERSION = "production-stack-v4.14.0-full-catalog-evaluation"


def production_engine_class(base_cls: type) -> type:
    """Return the one canonical production class for a validated taste engine.

    Every caller must use this composition instead of rebuilding wrappers ad-hoc. The order is
    intentional: availability filters the fully assembled retrieval pool, then Context 3.5 wraps
    the exact resulting engine without collapsing V17/V18/V19 behavior back to an older class.
    """
    if not isinstance(base_cls, type) or not issubclass(base_cls, FastRecommendationEngineV16):
        base_cls = FastRecommendationEngineV16

    available_cls = (
        base_cls
        if issubclass(base_cls, AvailabilityGuardMixinV37)
        else availability_engine_class(base_cls)
    )
    if str(getattr(available_cls, "CONTEXT_RECOMMENDER_VERSION", "")) == CONTEXT_RECOMMENDER_VERSION:
        context_cls = available_cls
    else:
        context_cls = contextual_engine_class(available_cls)
    personalized_cls = personalization_engine_class(context_cls)
    return learning_insight_engine_class(personalized_cls)


def build_production_recommender(db, base_cls: type, calendar=None):
    """Instantiate the exact recommender stack used by both the app and quality evaluation."""
    calendar = calendar or ContextCalendarEngineV35()
    engine_cls = production_engine_class(base_cls)
    engine = engine_cls(db, calendar)

    # These are deliberately replaced with the current validated learners. Older base classes
    # construct compatible predecessors in __init__, but production and backtests must use the
    # same final learners rather than depending on inheritance side effects.
    engine.adaptive = AdaptivePreferenceLearnerV2(db)
    engine.watch_intent = WatchSuccessIntentLearnerV33(db)
    return engine


def production_stack_status(engine) -> dict:
    cls = type(engine)
    return {
        "version": PRODUCTION_STACK_VERSION,
        "engine_class": cls.__name__,
        "availability_guard": issubclass(cls, AvailabilityGuardMixinV37),
        "context_guard": str(getattr(cls, "CONTEXT_RECOMMENDER_VERSION", "")) == CONTEXT_RECOMMENDER_VERSION,
        "calendar_class": type(getattr(engine, "calendar", None)).__name__,
        "adaptive_class": type(getattr(engine, "adaptive", None)).__name__,
        "watch_intent_class": type(getattr(engine, "watch_intent", None)).__name__,
        "hybrid_calibration_version": str(
            getattr(engine, "HYBRID_CALIBRATION_VERSION", "global-baseline-70-30")
        ),
        "als_weight": round(float(getattr(engine, "ALS_WEIGHT", .70)), 2),
        "content_weight": round(float(getattr(engine, "CONTENT_WEIGHT", .30)), 2),
        "recommendation_engine_identity": recommendation_engine_identity(engine),
        "personalization_version": str(getattr(engine, "PERSONALIZATION_V41_VERSION", "")),
        "personalization_status": (
            engine.personalization_status()
            if callable(getattr(engine, "personalization_status", None))
            else {"version": PERSONALIZATION_V41_VERSION, "quality_gate": {"approved": False, "reason": "unavailable"}}
        ),
        "learning_insight_version": str(getattr(engine, "LEARNING_INSIGHT_VERSION", "")),
        "learning_insight_status": (
            engine.learning_insight_status()
            if callable(getattr(engine, "learning_insight_status", None))
            else {"version": LEARNING_INSIGHT_VERSION}
        ),
    }
