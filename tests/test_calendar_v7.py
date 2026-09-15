from datetime import date

from cinecalendar.calendar_engine_v2 import RichCalendarEngine
from cinecalendar.db import Database
from cinecalendar.models import Movie
from cinecalendar.recommender_v7 import (
    EVENT_RELEVANCE_RULES,
    SEASONAL_EVENT_KEYS,
    FastRecommendationEngineV7,
)


def _engine(tmp_path):
    return FastRecommendationEngineV7(Database(tmp_path / "calendar-v7.db"), RichCalendarEngine())


def test_every_concrete_calendar_event_has_an_explicit_precision_rule():
    """A new date must not silently fall back to generic History/War/etc."""
    cal = RichCalendarEngine()
    for year in (2026, 2027):
        events = cal.events_for_year(year)
        missing = sorted(
            {
                ev.key
                for ev in events
                if ev.category != "sezon" and ev.key not in EVENT_RELEVANCE_RULES
            }
        )
        assert not missing, f"Calendar events without explicit relevance rule: {missing}"

        unexpected_seasonal = sorted(
            ev.key for ev in events if ev.category == "sezon" and ev.key not in SEASONAL_EVENT_KEYS
        )
        assert not unexpected_seasonal, f"Seasonal events not classified explicitly: {unexpected_seasonal}"


def test_generic_metadata_cannot_claim_specific_calendar_dates(tmp_path):
    engine = _engine(tmp_path)
    generic = Movie(
        title="Generic Historical Drama",
        original_title="Generic Historical Drama",
        genres=["History", "War", "Drama", "Biography"],
        overview="A broad historical political story set in Romania about war and faith.",
        semantic={
            "history": 1.0,
            "war": 1.0,
            "politics": 1.0,
            "romania": 1.0,
            "christianity": 1.0,
            "faith": 1.0,
            "saints": 1.0,
            "biography": 1.0,
        },
    )

    # Representative Orthodox, Romanian, international and cultural dates. None may accept
    # broad genres/tags as proof that the film is about the event itself.
    for when in (
        date(2026, 9, 14),   # Holy Cross
        date(2026, 4, 23),   # St George
        date(2026, 1, 24),   # Union of the Principalities
        date(2026, 12, 1),   # National Day / Great Union
        date(2026, 9, 11),   # 9/11
        date(2026, 5, 8),    # VE Day
        date(2026, 1, 15),   # Eminescu / Culture Day
    ):
        score, kind, _reason = engine._calendar_score_cached(generic, when)
        assert score == 0.0, (when, score, kind)
        assert kind == "slabă"


def test_real_event_anchors_are_accepted_across_different_date_types(tmp_path):
    engine = _engine(tmp_path)
    samples = [
        (
            date(2026, 9, 14),
            Movie(title="The Passion of the Christ", overview="The crucifixion at Calvary and Golgotha."),
        ),
        (
            date(2026, 1, 24),
            Movie(title="The Little Union", overview="Alexandru Ioan Cuza and the union of Moldavia and Wallachia in 1859 Romania."),
        ),
        (
            date(2026, 12, 1),
            Movie(title="Great Union 1918", overview="Alba Iulia 1918 and the Great Union of Transylvania with Romania."),
        ),
        (
            date(2026, 9, 11),
            Movie(title="September 11", overview="A documentary about the September 11 attacks on the World Trade Center in 2001."),
        ),
        (
            date(2026, 4, 23),
            Movie(title="Saint George the Martyr", overview="The life and martyrdom of Saint George."),
        ),
        (
            date(2026, 1, 15),
            Movie(title="Mihai Eminescu", overview="A biography of Romanian poet Mihai Eminescu."),
        ),
        (
            date(2026, 4, 22),
            Movie(title="Planet Earth", genres=["Documentary"], overview="Nature, wildlife and conservation across the planet."),
        ),
    ]

    for when, movie in samples:
        score, kind, reason = engine._calendar_score_cached(movie, when)
        assert score >= .12, (when, movie.title, score, reason)
        assert kind in {"directă", "istorică", "spirituală"}
        assert "legătură" in reason


def test_concrete_event_never_uses_atmosphere_as_fake_factual_match(tmp_path):
    engine = _engine(tmp_path)
    dark_random = Movie(
        title="Random Dark Thriller",
        genres=["Thriller"],
        overview="A dark bleak disturbing crime story with no religious subject.",
        semantic={"dark": 1.0, "contemplative": .8},
    )
    score, kind, _reason = engine._calendar_score_cached(dark_random, date(2026, 4, 10))
    # 10 April 2026 is during Holy Week, but dark atmosphere alone is not a factual tie.
    assert score == 0.0
    assert kind == "slabă"
