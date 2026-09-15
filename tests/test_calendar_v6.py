from __future__ import annotations

import csv
from datetime import date

from cinecalendar.calendar_engine_v2 import RichCalendarEngine
from cinecalendar.catalog import import_catalog_csv
from cinecalendar.db import Database
from cinecalendar.imdb_import import import_imdb_csv
from cinecalendar.profile import build_profile
from cinecalendar.recommender_v6 import FastRecommendationEngineV6


HEADERS = [
    "Const", "Your Rating", "Date Rated", "Title", "Original Title", "URL",
    "Title Type", "IMDb Rating", "Runtime (mins)", "Year", "Genres",
    "Num Votes", "Release Date", "Directors",
]


def _write_ratings(path):
    rows = []
    for i in range(1, 8):
        title = f"Rated History Faith {i}"
        rows.append({
            "Const": f"tt91000{i:02d}", "Your Rating": str(8 + i % 3),
            "Date Rated": "2026-09-01", "Title": title, "Original Title": title,
            "URL": "", "Title Type": "Movie", "IMDb Rating": "7.5",
            "Runtime (mins)": "110", "Year": str(2015 + i),
            "Genres": "History,Drama", "Num Votes": "50000",
            "Release Date": f"{2015+i}-01-01", "Directors": "Calendar Director",
        })
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADERS)
        writer.writeheader(); writer.writerows(rows)


def test_rich_calendar_is_not_just_a_few_anchors():
    cal = RichCalendarEngine()
    events = cal.events_for_year(2026)
    assert len(events) >= 45
    keys = {e.key for e in events}
    assert "exaltation_cross" in keys
    assert "good_friday" in keys
    assert "nativity_fast" in keys
    assert "union_principalities" in keys
    assert "august_23_1944" in keys
    active = cal.relevant_events(date(2026, 9, 14))
    assert any(ev.key == "exaltation_cross" for ev, _ in active)


def test_holy_cross_program_rejects_generic_history_false_positives(tmp_path, monkeypatch):
    db = Database(tmp_path / "calendar.db")
    ratings = tmp_path / "ratings.csv"
    _write_ratings(ratings)
    import_imdb_csv(db, ratings)
    build_profile(db)

    catalog = tmp_path / "catalog.csv"
    lines = ["imdb_id,title,year,title_type,runtime,genres,directors,imdb_rating,num_votes,overview"]

    genuinely_related = [
        ("tt9200001", "The Holy Cross", "Christian history of the holy cross and faith"),
        ("tt9200002", "Cross of Faith", "Christian faith and the holy cross"),
        ("tt9200003", "Calvary Chronicle", "The passion of Christ, calvary and crucifixion"),
    ]
    for iid, title, overview in genuinely_related:
        lines.append(f"{iid},{title},2024,Movie,105,History,Calendar Director,8.1,40000,{overview}")

    # Real-world type of false positives reported by the UI: History/Biography/Drama is not
    # evidence of a link with the Exaltation of the Holy Cross.
    unrelated = [
        ("tt9200101", "Apollo 13", "NASA lunar mission accident and rescue"),
        ("tt9200102", "Munich", "Political thriller about the aftermath of the 1972 Olympics"),
        ("tt9200103", "Downfall", "The final days of Nazi Germany in Berlin"),
        ("tt9200104", "Argo", "CIA rescue operation during the Iran hostage crisis"),
        ("tt9200105", "Dark Waters", "Corporate pollution investigation"),
        ("tt9200106", "Straight Outta Compton", "Biography of a hip hop group"),
    ]
    for iid, title, overview in unrelated:
        lines.append(f"{iid},{title},2020,Movie,120,History;Drama,Calendar Director,8.0,80000,{overview}")

    lines.append("tt9200201,Autumn Monastery,2024,Movie,100,Drama,Calendar Director,7.8,12000,A contemplative monastery story in autumn")
    for i in range(30, 100):
        lines.append(
            f"tt920{i:04d},Generic History Candidate {i},2023,Movie,100,History,Calendar Director,7.{i%9},{1000+i*300},generic history drama"
        )
    catalog.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import_catalog_csv(db, catalog)

    engine = FastRecommendationEngineV6(db, RichCalendarEngine())
    result = engine.calendar_day_program(date(2026, 9, 14), 6)
    sections = {section["key"]: section for section in result["sections"]}

    assert "exact" in sections
    exact_titles = {rec.movie.title for rec in sections["exact"]["recommendations"]}
    assert exact_titles & {"The Holy Cross", "Cross of Faith", "Calvary Chronicle"}

    factual_lanes = {"exact", "direct", "spiritual", "historical"}
    factual_titles = {
        rec.movie.title
        for section in result["sections"]
        if section["key"] in factual_lanes
        for rec in section["recommendations"]
    }
    forbidden = {title for _iid, title, _overview in unrelated}
    assert factual_titles.isdisjoint(forbidden)
    assert not any(title.startswith("Generic History Candidate") for title in factual_titles)

    # Every factual recommendation shown for the feast must carry a Cross/Passion anchor.
    for section in result["sections"]:
        if section["key"] not in factual_lanes:
            continue
        for rec in section["recommendations"]:
            sem = rec.movie.semantic
            assert max(float(sem.get("cross_veneration", 0) or 0), float(sem.get("passion_of_christ", 0) or 0)) > 0

    assert all(
        rec.movie.imdb_id not in {f"tt91000{i:02d}" for i in range(1, 8)}
        for section in result["sections"]
        for rec in section["recommendations"]
    )

    def should_not_query_again(*args, **kwargs):
        raise AssertionError("calendar_day_program recalculated a cached day")

    monkeypatch.setattr(engine, "_candidate_rows", should_not_query_again)
    cached = engine.calendar_day_program(date(2026, 9, 14), 6)
    assert cached is result
