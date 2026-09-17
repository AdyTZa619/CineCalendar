from __future__ import annotations

from datetime import date, timedelta
import inspect

from cinecalendar.calendar_engine import orthodox_easter
from cinecalendar.calendar_engine_v3 import ContextCalendarEngineV35
from cinecalendar.context_recommender_v35 import (
    FastRecommendationEngineV16Context35,
    FastRecommendationEngineV17Context35,
    contextual_engine_class,
)
from cinecalendar.context_ui_v35 import install_context_ui_v35
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.recommender_v17 import FastRecommendationEngineV17
from cinecalendar import production_engine as production_engine_module
from cinecalendar import service as service_module
from cinecalendar import ui_composition as ui_composition_module


def _event(engine, year: int, key: str):
    return next(event for event in engine.events_for_year(year) if event.key == key)


def test_key_dates_expected_by_context_intelligence_are_indexed():
    engine = ContextCalendarEngineV35()
    events = engine.events_for_year(2026)
    by_key = {event.key: event for event in events}

    assert by_key["romanian_culture"].start == date(2026, 1, 15)
    assert by_key["womens_day"].start == date(2026, 3, 8)
    assert by_key["labour_day"].start == date(2026, 5, 1)
    assert by_key["children_day"].start == date(2026, 6, 1)
    assert by_key["ww2_start"].start == date(2026, 9, 1)
    assert by_key["september_transition"].start == date(2026, 9, 1)
    assert by_key["exaltation_cross"].start == date(2026, 9, 14)
    assert by_key["romania_national"].start == date(2026, 12, 1)


def test_cross_influence_is_strongest_on_feast_and_fades_afterwards():
    engine = ContextCalendarEngineV35()
    feast = date(2026, 9, 14)

    def proximity(day):
        for event, value in engine.relevant_events(day):
            if event.key == "exaltation_cross":
                return value
        return 0.0

    assert proximity(feast) == 1.0
    assert 0.0 < proximity(feast + timedelta(days=1)) < 1.0
    assert 0.0 < proximity(feast + timedelta(days=2)) < proximity(feast + timedelta(days=1))
    assert proximity(feast + timedelta(days=3)) == 0.0


def test_lent_has_its_own_context_phase():
    engine = ContextCalendarEngineV35()
    easter = orthodox_easter(2026)
    middle = easter - timedelta(days=25)
    phase, tags = engine.season_phase(middle)

    assert phase == "Postul Mare"
    assert tags["faith"] >= .8
    assert tags["contemplative"] >= .8


def test_context_vocabulary_recognises_subjects_without_rewriting_profile_semantics():
    engine = ContextCalendarEngineV35()
    movie = Movie(
        title="Women of the Factory",
        original_title="Women of the Factory",
        overview="Women workers organise at a factory while a teacher documents their lives.",
        genres=["Documentary"],
    )
    sem = engine.semantic_for(movie)

    assert sem["women"] > 0
    assert sem["labour"] > 0
    assert sem["education"] > 0


def test_womens_day_can_produce_direct_context_reason():
    engine = ContextCalendarEngineV35()
    movie = Movie(
        title="Women Who Changed History",
        overview="A documentary about women whose lives changed public history.",
        genres=["Documentary", "Biography"],
    )

    score, kind, reason = engine.calendar_relevance(movie, date(2026, 3, 8))

    assert score >= .12
    assert kind == "directă"
    assert "Ziua Internațională a Femeii" in reason
    assert "reper activ azi" in reason


def test_atmosphere_is_capped_and_cannot_impersonate_direct_event_match():
    engine = ContextCalendarEngineV35()
    movie = Movie(
        title="Autumn Leaves",
        overview="An autumn landscape and a contemplative journey through a forest.",
        genres=["Drama"],
    )

    score, kind, _reason = engine.calendar_relevance(movie, date(2026, 9, 1))

    if kind == "atmosferică":
        assert score <= .34


def _rec(predicted: float, confidence: float) -> Recommendation:
    return Recommendation(
        Movie(id=1, title="Context test", genres=["Drama"]),
        ScoreBreakdown(predicted_rating=predicted, confidence=confidence, final=.8),
    )


def test_soft_context_lane_cannot_rescue_confidently_weak_personal_match():
    assert FastRecommendationEngineV16Context35._context_candidate_allowed(_rec(5.7, .80), "related") is False
    assert FastRecommendationEngineV16Context35._context_candidate_allowed(_rec(5.7, .80), "season") is False
    assert FastRecommendationEngineV16Context35._context_candidate_allowed(_rec(6.4, .80), "related") is True


def test_factual_lane_remains_visible_but_does_not_change_normal_top3_rule():
    weak = _rec(4.5, .90)
    assert FastRecommendationEngineV16Context35._context_candidate_allowed(weak, "direct") is True
    assert FastRecommendationEngineV16Context35._context_candidate_allowed(weak, "exact") is True


def test_quality_verdict_maps_to_matching_context_bounded_engine():
    assert contextual_engine_class(FastRecommendationEngineV16) is FastRecommendationEngineV16Context35
    assert contextual_engine_class(FastRecommendationEngineV17) is FastRecommendationEngineV17Context35
    assert issubclass(FastRecommendationEngineV17Context35, FastRecommendationEngineV16)


def test_production_service_uses_v35_calendar_and_canonical_safety_stack():
    source = inspect.getsource(service_module.CineCalendarService.__init__)
    composition = inspect.getsource(production_engine_module.production_engine_class)
    builder = inspect.getsource(production_engine_module.build_production_recommender)

    assert "ContextCalendarEngineV35" in source
    assert "build_production_recommender" in source
    assert "FastRecommendationEngineV16" in source
    assert "issubclass" in source
    assert "availability_engine_class" in composition
    assert "contextual_engine_class" in composition
    assert "AdaptivePreferenceLearnerV2" in builder
    assert "WatchSuccessIntentLearnerV33" in builder


def test_context_diagnostics_are_in_canonical_ui_composition():
    composition = inspect.getsource(ui_composition_module.compose_premium_window)
    patch = inspect.getsource(install_context_ui_v35)
    assert "install_context_ui_v35(window_cls)" in composition
    assert "Motor recomandări • diagnostic" in patch
    assert "De ce acum:" in patch
