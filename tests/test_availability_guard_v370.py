from __future__ import annotations

from datetime import date

from cinecalendar.availability_guard_v37 import AvailabilityGuardMixinV37, availability_engine_class
from cinecalendar.calendar_engine_v3 import ContextCalendarEngineV35
from cinecalendar.db import Database
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.util import identity_key, normalize_text, utcnow_iso


def _movie(db: Database, title: str, year: int | None, release_date: str | None) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(identity_key,title,original_title,title_norm,original_title_norm,
                      year,title_type,release_date,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identity_key(title, title, year, "Movie"),
                title,
                title,
                normalize_text(title),
                normalize_text(title),
                year,
                "Movie",
                release_date,
                "test",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


class _DummyBaseline(FastRecommendationEngineV16):
    def _balanced_candidate_ids(self, when, limit: int):
        return list(getattr(self, "_dummy_ids", []))[: int(limit)]


def test_availability_guard_removes_only_known_future_releases(tmp_path):
    db = Database(tmp_path / "availability.db")
    past = _movie(db, "Past", 2025, "2025-06-01")
    future_year = _movie(db, "Future Year", 2027, None)
    future_date = _movie(db, "Future Date", 2026, "2026-12-20")
    unknown_date = _movie(db, "Unknown Current Year", 2026, None)
    unknown_year = _movie(db, "Unknown Year", None, None)

    guarded_cls = availability_engine_class(_DummyBaseline)
    assert guarded_cls.__mro__[1] is AvailabilityGuardMixinV37
    assert _DummyBaseline in guarded_cls.__mro__

    engine = guarded_cls(db, ContextCalendarEngineV35())
    engine._dummy_ids = [future_date, past, future_year, unknown_date, unknown_year]
    ids = engine._balanced_candidate_ids(date(2026, 9, 17), 20)

    assert future_year not in ids
    assert future_date not in ids
    assert ids == [past, unknown_date, unknown_year]


def test_availability_guard_factory_is_stable():
    first = availability_engine_class(_DummyBaseline)
    second = availability_engine_class(_DummyBaseline)
    assert first is second
