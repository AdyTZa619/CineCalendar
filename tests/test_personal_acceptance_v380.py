from __future__ import annotations

from datetime import date
import csv

from cinecalendar.calendar_engine import orthodox_easter
from cinecalendar.db import Database
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.personal_acceptance_v38 import (
    _known_future_release,
    _validate_top3,
    acceptance_dates,
    csv_fingerprint,
)


def _rec(mid: int, *, predicted=7.5, final=.80, calendar=.0, season=.2, period=False, red=False):
    movie = Movie(
        id=mid,
        imdb_id=f"tt{mid:07d}",
        title=f"Movie {mid}",
        year=2020,
        title_type="movie",
        imdb_rating=7.2,
        num_votes=5000,
    )
    audit = {
        "status": "red_flag" if red else "trusted",
        "red_flag": red,
        "period_context_slot": period,
        "top3_role": "period_context" if period else "standard",
        "period_context_signal": .5 if period else None,
    }
    score = ScoreBreakdown(
        final=final,
        predicted_rating=predicted,
        confidence=.8,
        calendar=calendar,
        season=season,
        trust_audit=audit,
    )
    return Recommendation(movie, score)


def test_acceptance_dates_cover_contractual_contexts():
    anchor = date(2026, 9, 17)
    cases = acceptance_dates(anchor)
    by_day = {item["date"]: set(item["expected"]) for item in cases}

    assert "romanian_culture" in by_day[date(2026, 1, 15)]
    assert "womens_day" in by_day[date(2026, 3, 8)]
    assert "great_lent" in by_day[orthodox_easter(2026).replace() - __import__("datetime").timedelta(days=28)]
    assert "easter" in by_day[orthodox_easter(2026)]
    assert "labour_day" in by_day[date(2026, 5, 1)]
    assert "children_day" in by_day[date(2026, 6, 1)]
    assert "dormition_fast" in by_day[date(2026, 8, 7)]
    assert {"ww2_start", "september_transition"}.issubset(by_day[date(2026, 9, 1)])
    assert "exaltation_cross" in by_day[date(2026, 9, 14)]
    assert "romania_national" in by_day[date(2026, 12, 1)]
    assert "nativity_fast" in by_day[date(2026, 12, 10)]
    assert "nativity" in by_day[date(2026, 12, 25)]


def test_future_release_guard_uses_date_and_year():
    rec = _rec(1)
    rec.movie.release_date = "2026-10-01"
    assert _known_future_release(rec, date(2026, 9, 17))[0] is True

    rec.movie.release_date = None
    rec.movie.year = 2027
    assert _known_future_release(rec, date(2026, 9, 17))[0] is True

    rec.movie.year = 2026
    assert _known_future_release(rec, date(2026, 9, 17))[0] is False


def test_period_context_never_replaces_anchor_and_obeys_guardrails():
    class Engine:
        PERIOD_PREDICTED_FLOOR = 6.35
        PERIOD_FINAL_GAP = .085
        PERIOD_CALENDAR_MIN = .16
        PERIOD_SEASON_MIN = .68

    good = [
        _rec(1, predicted=8.2, final=.86),
        _rec(2, predicted=7.7, final=.82, calendar=.30, period=True),
        _rec(3, predicted=7.5, final=.80),
    ]
    failures, warnings = _validate_top3(good, date(2026, 9, 14), Engine(), set(), set())
    assert failures == []
    assert warnings == []

    bad_anchor = [
        _rec(1, predicted=8.2, final=.86, calendar=.30, period=True),
        _rec(2, predicted=7.7, final=.82),
        _rec(3, predicted=7.5, final=.80),
    ]
    failures, _ = _validate_top3(bad_anchor, date(2026, 9, 14), Engine(), set(), set())
    assert any(item["code"] == "period_replaced_anchor" for item in failures)

    weak = [
        _rec(1, predicted=8.2, final=.86),
        _rec(2, predicted=5.9, final=.74, calendar=.30, period=True),
        _rec(3, predicted=7.5, final=.80),
    ]
    failures, _ = _validate_top3(weak, date(2026, 9, 14), Engine(), set(), set())
    codes = {item["code"] for item in failures}
    assert "weak_period_slot" in codes
    assert "period_slot_score_gap" in codes


def test_top3_rejects_rated_seen_duplicates_and_red_flags():
    class Engine:
        PERIOD_PREDICTED_FLOOR = 6.35
        PERIOD_FINAL_GAP = .085
        PERIOD_CALENDAR_MIN = .16
        PERIOD_SEASON_MIN = .68

    first = _rec(1, red=True)
    second = _rec(1)
    third = _rec(3)
    failures, _ = _validate_top3(
        [first, second, third],
        date(2026, 9, 17),
        Engine(),
        rated={1},
        seen={3},
    )
    codes = {item["code"] for item in failures}
    assert "duplicate_top3" in codes
    assert "rated_movie_leak" in codes
    assert "seen_movie_leak" in codes
    assert "red_flag_visible" in codes


def test_csv_fingerprint_reports_aggregates_only(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    add_manual_rating(db, "One", 2020, 9, imdb_id="tt0000001", genres=["Drama"])
    add_manual_rating(db, "Two", 2021, 6, imdb_id="tt0000002", genres=["Drama"])

    path = tmp_path / "ratings.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Const", "Your Rating", "Date Rated", "Title", "Title Type"])
        writer.writeheader()
        writer.writerow({"Const": "tt0000001", "Your Rating": "9", "Date Rated": "2026-01-01", "Title": "One", "Title Type": "movie"})
        writer.writerow({"Const": "tt0000002", "Your Rating": "7", "Date Rated": "2026-02-01", "Title": "Two", "Title Type": "movie"})
        writer.writerow({"Const": "tt0000003", "Your Rating": "8", "Date Rated": "2026-03-01", "Title": "Three", "Title Type": "movie"})

    report = csv_fingerprint(db, path)
    assert report["rows"] == 3
    assert report["unique_imdb_ids"] == 3
    assert report["duplicate_imdb_ids"] == 0
    assert report["exact_rating_matches"] == 1
    assert report["rating_mismatches"] == 1
    assert report["missing_in_db"] == 1
    assert report["extra_in_db"] == 0
    assert report["date_min"] == "2026-01-01"
    assert report["date_max"] == "2026-03-01"
    # Privacy contract: no raw rating rows or per-title history is returned.
    assert "pairs" not in report
    assert "ratings" not in report
