from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.story_period import infer_story_period, order_recommendations_by_story_period


def rec(title, overview="", keywords=None, release_year=2026):
    return Recommendation(
        movie=Movie(title=title, overview=overview, keywords=keywords or [], year=release_year),
        score=ScoreBreakdown(final=0.8),
    )


def test_story_chronology_ignores_release_year():
    items = [
        rec("Future", "In the year 2150, humanity lives on Mars.", release_year=1980),
        rec("WWII", "During World War II in 1944, a resistance cell fights occupation.", release_year=2025),
        rec("Ancient", "A Roman Empire general returns to Rome in 180.", release_year=2024),
        rec("Nineties", "A family story set in the 1990s.", release_year=1970),
        rec("WWI", "A soldier enters the trenches of World War I in 1916.", release_year=2026),
    ]
    ordered = order_recommendations_by_story_period(items)
    assert [x.movie.title for x in ordered] == ["Ancient", "WWI", "WWII", "Nineties", "Future"]


def test_explicit_period_beats_unrelated_release_year():
    item = rec("Old release", "A spy works behind the Iron Curtain during the Cold War.", release_year=2026)
    period = infer_story_period(item)
    assert period is not None
    assert period.label == "Perioada comunistă"


def test_unknown_setting_is_stable_and_does_not_fake_release_year():
    a = rec("Unknown A", "Two siblings face a difficult choice.", release_year=1901)
    b = rec("Unknown B", "A detective investigates a disappearance.", release_year=2026)
    assert infer_story_period(a) is None
    assert order_recommendations_by_story_period([a, b]) == [a, b]


def test_sort_changes_order_not_membership():
    items = [
        rec("Present", "A present-day investigation."),
        rec("Medieval", "A medieval knight returns from a crusade."),
        rec("Interwar", "In the 1930s, a journalist uncovers a conspiracy."),
    ]
    ordered = order_recommendations_by_story_period(items)
    assert {id(x) for x in ordered} == {id(x) for x in items}
    assert [x.movie.title for x in ordered] == ["Medieval", "Interwar", "Present"]
