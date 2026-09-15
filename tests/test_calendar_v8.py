from datetime import date

from cinecalendar.calendar_engine_v2 import RichCalendarEngine
from cinecalendar.catalog import import_catalog_csv
from cinecalendar.db import Database
from cinecalendar.recommender_v7 import FastRecommendationEngineV7
from cinecalendar.recommender_v8 import FastRecommendationEngineV8


def _catalog(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text(
        "imdb_id,title,year,title_type,runtime,genres,directors,imdb_rating,num_votes,overview\n"
        "tt8000001,Calvary: The Passion,2014,Movie,100,Drama,Event Director,6.1,51,\n"
        "tt8000002,Apollo 13,1995,Movie,140,History;Drama,Generic Director,9.0,1000000,NASA lunar mission accident and rescue\n"
        "tt8000003,Generic History Epic,2020,Movie,120,History;War,Generic Director,8.8,900000,A broad historical war drama\n",
        encoding="utf-8",
    )
    return path


def test_event_scan_searches_full_catalog_and_rejects_generic_history(tmp_path):
    db = Database(tmp_path / "calendar-v8.db")
    import_catalog_csv(db, _catalog(tmp_path))
    engine = FastRecommendationEngineV8(db, RichCalendarEngine())

    rows = engine._event_candidate_rows(date(2026, 9, 14))
    titles = {row["title"] for row in rows}

    assert "Calvary: The Passion" in titles
    assert "Apollo 13" not in titles
    assert "Generic History Epic" not in titles


def test_calendar_pool_injects_real_event_titles_even_when_normal_pool_misses_them(tmp_path, monkeypatch):
    db = Database(tmp_path / "calendar-v8-inject.db")
    import_catalog_csv(db, _catalog(tmp_path))
    engine = FastRecommendationEngineV8(db, RichCalendarEngine())

    with db.connect() as con:
        generic = con.execute("SELECT * FROM movies WHERE title='Apollo 13'").fetchone()

    # Simulate the old balanced shortlist missing the rare but actually relevant title.
    monkeypatch.setattr(
        FastRecommendationEngineV7,
        "_candidate_rows",
        lambda self, when, limit=100000: [generic],
    )

    engine._calendar_candidate_mode = True
    try:
        rows = engine._candidate_rows(date(2026, 9, 14), 400)
    finally:
        engine._calendar_candidate_mode = False

    titles = [row["title"] for row in rows]
    assert titles[0] == "Calvary: The Passion"
    assert "Apollo 13" in titles
    assert engine.last_event_candidate_count >= 1
