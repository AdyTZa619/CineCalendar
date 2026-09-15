from datetime import date

from cinecalendar.calendar_engine_v2 import RichCalendarEngine
from cinecalendar.db import Database
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.models import Movie
from cinecalendar.recommender_v9 import FastRecommendationEngineV9
from cinecalendar.recommender_v10 import FastRecommendationEngineV10


def _engine(tmp_path):
    return FastRecommendationEngineV10(Database(tmp_path / "calendar-v10.db"), RichCalendarEngine())


def test_holy_cross_related_lane_accepts_christian_subject_not_beach(tmp_path):
    engine = _engine(tmp_path)
    when = date(2026, 9, 15)  # influence day after the Exaltation of the Holy Cross

    christian = Movie(
        title="Paul, Apostle of Christ",
        original_title="Paul, Apostle of Christ",
        genres=["Drama"],
    )
    beach = Movie(
        title="The Beach",
        original_title="The Beach",
        genres=["Adventure", "Drama", "Romance"],
    )

    c_strength, c_event, _label, c_reason = engine._related_relation(christian, when)
    b_strength, b_event, _label, _reason = engine._related_relation(beach, when)

    assert c_strength > 0
    assert c_event is not None and c_event.key == "exaltation_cross"
    assert "Nu este prezentat ca legătură factuală directă" in c_reason
    assert b_strength == 0
    assert b_event is None


def test_rated_religious_favourite_is_seed_but_random_beach_is_not(tmp_path):
    db = Database(tmp_path / "seed-v10.db")
    add_manual_rating(db, "The Passion of the Christ", 2004, 10, "tt0335345", ["Drama"])
    add_manual_rating(db, "The Beach", 2000, 10, "tt0163978", ["Adventure", "Drama", "Romance"])
    engine = FastRecommendationEngineV10(db, RichCalendarEngine())

    seeds = engine._rated_event_seeds(date(2026, 9, 15))
    titles = {seed[0].title for seed in seeds}

    assert "The Passion of the Christ" in titles
    assert "The Beach" not in titles


def test_concrete_event_removes_generic_seasonal_filler(tmp_path, monkeypatch):
    engine = _engine(tmp_path)

    def fake_parent(self, when=None, count_per_section=6):
        return {
            "date": when,
            "phase": "vară târzie",
            "sections": [
                {"key": "season", "title": "Sezon și perioadă", "subtitle": "x", "recommendations": ["The Beach"]},
                {"key": "atmosphere", "title": "Atmosfera zilei", "subtitle": "x", "recommendations": ["Summer Vacation"]},
            ],
        }

    monkeypatch.setattr(FastRecommendationEngineV9, "calendar_day_program", fake_parent)
    monkeypatch.setattr(engine, "_build_related_recommendations", lambda *args, **kwargs: [])

    result = engine.calendar_day_program(date(2026, 9, 15), 6)
    keys = [section["key"] for section in result["sections"]]

    assert "season" not in keys
    assert "atmosphere" not in keys
    assert result["specific_event_active"] is True


def test_ordinary_day_can_keep_seasonal_fallback(tmp_path, monkeypatch):
    engine = _engine(tmp_path)

    def fake_parent(self, when=None, count_per_section=6):
        return {
            "date": when,
            "phase": "vară",
            "sections": [
                {"key": "season", "title": "Sezon și perioadă", "subtitle": "x", "recommendations": ["Seasonal Film"]},
            ],
        }

    monkeypatch.setattr(FastRecommendationEngineV9, "calendar_day_program", fake_parent)

    # A date without a concrete indexed event keeps the honest seasonal fallback.
    result = engine.calendar_day_program(date(2026, 7, 13), 6)
    assert any(section["key"] == "season" for section in result["sections"])
