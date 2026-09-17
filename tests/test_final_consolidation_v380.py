from __future__ import annotations

from datetime import date
import inspect

from cinecalendar.accuracy_engine_v37 import LocalContentAccuracyMixinV37, calibrated_accuracy_engine_class
from cinecalendar.adaptive_preferences_v2 import AdaptivePreferenceLearnerV2
from cinecalendar.availability_guard_v37 import AvailabilityGuardMixinV37
from cinecalendar.calendar_engine_v3 import ContextCalendarEngineV35
from cinecalendar.context_recommender_v35 import (
    CONTEXT_RECOMMENDER_VERSION,
    FastRecommendationEngineV16Context35,
)
from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.production_engine import (
    build_production_recommender,
    production_engine_class,
    production_stack_status,
)
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.recommender_v17 import FastRecommendationEngineV17
from cinecalendar.service import CineCalendarService
from cinecalendar.watch_success_v33 import WatchSuccessIntentLearnerV33


def _rec(
    mid: int,
    *,
    final: float,
    predicted: float,
    confidence: float = .75,
    calendar: float = 0.0,
    season: float = .35,
    kind: str = "slabă",
    trust: float = .72,
    red_flag: bool = False,
) -> Recommendation:
    return Recommendation(
        Movie(id=mid, title=f"Film {mid}", genres=["Drama"]),
        ScoreBreakdown(
            final=final,
            predicted_rating=predicted,
            confidence=confidence,
            calendar=calendar,
            season=season,
            calendar_kind=kind,
            calendar_reason="Reper contextual de test.",
            trust_audit={
                "status": "red_flag" if red_flag else "trusted",
                "trusted": not red_flag,
                "red_flag": red_flag,
                "trust": trust,
            },
        ),
    )


def test_canonical_production_stack_contains_every_safety_wrapper():
    cls = production_engine_class(FastRecommendationEngineV16)
    assert issubclass(cls, FastRecommendationEngineV16)
    assert issubclass(cls, AvailabilityGuardMixinV37)
    assert getattr(cls, "CONTEXT_RECOMMENDER_VERSION", "") == CONTEXT_RECOMMENDER_VERSION


def test_canonical_stack_preserves_calibrated_v19_retrieval_instead_of_collapsing_it():
    challenger = calibrated_accuracy_engine_class(FastRecommendationEngineV17, .20)
    cls = production_engine_class(challenger)

    assert issubclass(cls, AvailabilityGuardMixinV37)
    assert LocalContentAccuracyMixinV37 in cls.__mro__
    assert challenger in cls.__mro__
    assert FastRecommendationEngineV17 in cls.__mro__
    assert getattr(cls, "LOCAL_CONTENT_SHARE", None) == .20


def test_builder_installs_exact_current_calendar_and_learners(tmp_path):
    db = Database(tmp_path / "stack.db")
    engine = build_production_recommender(db, FastRecommendationEngineV16)
    status = production_stack_status(engine)

    assert isinstance(engine.calendar, ContextCalendarEngineV35)
    assert isinstance(engine.adaptive, AdaptivePreferenceLearnerV2)
    assert isinstance(engine.watch_intent, WatchSuccessIntentLearnerV33)
    assert status["availability_guard"] is True
    assert status["context_guard"] is True
    assert status["calendar_class"] == "ContextCalendarEngineV35"
    assert status["adaptive_class"] == "AdaptivePreferenceLearnerV2"
    assert status["watch_intent_class"] == "WatchSuccessIntentLearnerV33"


def test_service_uses_single_canonical_builder_not_ad_hoc_wrapper_chain():
    source = inspect.getsource(CineCalendarService.__init__)
    assert "build_production_recommender" in source
    assert "production_stack_status" in source
    assert "availability_engine_class(" not in source
    assert "contextual_engine_class(" not in source


def test_period_slot_promotes_only_a_close_good_contextual_finalist():
    anchor = _rec(1, final=.82, predicted=8.2, calendar=.02)
    generic = _rec(2, final=.805, predicted=8.0, calendar=.03)
    contextual = _rec(
        3,
        final=.79,
        predicted=7.8,
        calendar=.48,
        season=.62,
        kind="spirituală",
    )

    selected = FastRecommendationEngineV16Context35._period_aware_select(
        [anchor, generic, contextual], 3
    )

    assert [rec.movie.id for rec in selected] == [1, 3, 2]
    assert selected[1].score.trust_audit["period_context_slot"] is True
    assert selected[1].score.trust_audit["top3_role"] == "period_context"
    assert selected[1].score.predicted_rating == 7.8
    assert selected[1].score.final == .79
    assert selected[1].score.contributions[0][0] == "Potrivire cu perioada"


def test_period_slot_does_nothing_when_context_is_weak():
    ordered = [
        _rec(1, final=.82, predicted=8.2, calendar=.03, season=.36),
        _rec(2, final=.805, predicted=8.0, calendar=.04, season=.41),
        _rec(3, final=.79, predicted=7.9, calendar=.02, season=.40),
    ]
    selected = FastRecommendationEngineV16Context35._period_aware_select(ordered, 3)
    assert [rec.movie.id for rec in selected] == [1, 2, 3]


def test_period_slot_cannot_rescue_a_materially_weaker_or_low_predicted_movie():
    anchor = _rec(1, final=.84, predicted=8.3)
    far = _rec(2, final=.70, predicted=8.0, calendar=.75, kind="directă")
    weak = _rec(3, final=.81, predicted=6.1, calendar=.80, kind="directă")
    normal = _rec(4, final=.80, predicted=7.8, calendar=.03)

    selected = FastRecommendationEngineV16Context35._period_aware_select(
        [anchor, normal, far, weak], 3
    )
    ids = [rec.movie.id for rec in selected]
    assert ids == [1, 4, 2]
    assert ids[1] != 2
    assert ids[1] != 3


def test_period_slot_never_promotes_red_flag_even_with_perfect_context():
    anchor = _rec(1, final=.82, predicted=8.0)
    safe = _rec(2, final=.79, predicted=7.8, calendar=.02)
    red = _rec(3, final=.81, predicted=8.2, calendar=1.0, kind="directă", red_flag=True)

    selected = FastRecommendationEngineV16Context35._period_aware_select([anchor, safe, red], 2)
    assert [rec.movie.id for rec in selected] == [1, 2]


def test_key_period_examples_are_recognised_by_the_same_calendar_used_in_production():
    engine = ContextCalendarEngineV35()

    cross = Movie(
        title="The Passion",
        overview="The passion of Christ and veneration of the cross.",
        genres=["Drama"],
        semantic={"cross_veneration": 1.0, "christianity": .9, "faith": .9},
    )
    cross_score, cross_kind, cross_reason = engine.calendar_relevance(cross, date(2026, 9, 14))
    assert cross_score >= .40
    assert cross_kind in {"directă", "spirituală"}
    assert "Înălțarea Sfintei Cruci" in cross_reason

    romania = Movie(
        title="Romania 1918",
        overview="Romanian history and the union of 1918.",
        genres=["History"],
        semantic={"romania": 1.0, "history": 1.0},
    )
    ro_score, ro_kind, ro_reason = engine.calendar_relevance(romania, date(2026, 12, 1))
    assert ro_score >= .50
    assert ro_kind in {"directă", "istorică"}
    assert "Ziua Națională a României" in ro_reason


def test_context_influence_fades_instead_of_sticking_to_unrelated_dates():
    engine = ContextCalendarEngineV35()
    movie = Movie(
        title="Cross",
        semantic={"cross_veneration": 1.0, "christianity": .9, "faith": .9},
    )
    on_day = engine.calendar_relevance(movie, date(2026, 9, 14))[0]
    far_day = engine.calendar_relevance(movie, date(2026, 9, 20))[0]
    assert on_day > far_day
