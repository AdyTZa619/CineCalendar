from datetime import date

from cinecalendar.db import Database
from cinecalendar.recommender_v4 import FastRecommendationEngineV4


class CountingCalendar:
    def __init__(self):
        self.events_calls = 0
        self.season_calls = 0

    def relevant_events(self, when):
        self.events_calls += 1
        return []

    def season_phase(self, when):
        self.season_calls += 1
        return "test season", {"autumn": 1.0}


def test_date_context_is_built_once_per_day(tmp_path):
    db = Database(tmp_path / "cache.db")
    calendar = CountingCalendar()
    engine = FastRecommendationEngineV4(db, calendar)
    when = date(2026, 9, 14)

    for _ in range(50):
        events, label, tags = engine._date_context(when)
        assert events == ()
        assert label == "test season"
        assert tags == {"autumn": 1.0}

    assert calendar.events_calls == 1
    assert calendar.season_calls == 1
