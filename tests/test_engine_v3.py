from __future__ import annotations

import csv
from datetime import date

import pytest

from cinecalendar.catalog import import_catalog_csv
from cinecalendar.db import Database
from cinecalendar.imdb_import import import_imdb_csv
from cinecalendar.models import Movie
from cinecalendar.profile import build_profile
from cinecalendar.recommender_v3 import FastRecommendationEngine
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


HEADERS = [
    "Const", "Your Rating", "Date Rated", "Title", "Original Title", "URL",
    "Title Type", "IMDb Rating", "Runtime (mins)", "Year", "Genres",
    "Num Votes", "Release Date", "Directors",
]


def write_ratings(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(rows)


def rated(imdb_id, rating, title, year="2020", genres="History,Drama", director="Director Test"):
    return {
        "Const": imdb_id, "Your Rating": str(rating), "Date Rated": "2026-09-01",
        "Title": title, "Original Title": title, "URL": "", "Title Type": "Movie",
        "IMDb Rating": "7.2", "Runtime (mins)": "110", "Year": year, "Genres": genres,
        "Num Votes": "50000", "Release Date": f"{year}-01-01", "Directors": director,
    }


def test_engine_refuses_fake_personal_scores_without_ratings(tmp_path):
    db = Database(tmp_path / "empty.db")
    cat = tmp_path / "catalog.csv"
    cat.write_text(
        "imdb_id,title,year,title_type,genres,imdb_rating,num_votes\n"
        "tt8000001,Candidate,2024,Movie,History,8.0,100000\n",
        encoding="utf-8",
    )
    import_catalog_csv(db, cat)
    with pytest.raises(RuntimeError, match="ratinguri personale"):
        FastRecommendationEngine(db).recommend(date(2026, 9, 14), 1)


def test_zero_personal_evidence_never_claims_38_percent_confidence(tmp_path):
    db = Database(tmp_path / "confidence.db")
    engine = FastRecommendationEngine(db)
    movie = Movie(
        id=1, imdb_id="tt8000100", title="Unknown Pattern", original_title="Unknown Pattern",
        year=2025, title_type="Movie", runtime_min=100, genres=["Mystery"],
        directors=["Nobody In Profile"], countries=[], overview="", keywords=[], imdb_rating=8.4,
        num_votes=100000, source="test", semantic={},
    )
    predicted, confidence, evidence, _ = engine._predict_user_rating(
        movie, {"global_mean_rating": 6.5, "features": {}},
    )
    assert predicted == pytest.approx(6.5)
    assert evidence == 0
    assert confidence < 0.11


def test_schindlers_list_and_identity_duplicate_are_never_recommended(tmp_path):
    db = Database(tmp_path / "seen.db")
    ratings = tmp_path / "ratings.csv"
    rows = [
        rated("tt0108052", 10, "Schindler's List", "1993", "Biography,Drama,History", "Steven Spielberg"),
        rated("tt8100002", 9, "Rated History 2", "2019"),
        rated("tt8100003", 8, "Rated History 3", "2020"),
        rated("tt8100004", 9, "Rated History 4", "2021"),
        rated("tt8100005", 8, "Rated History 5", "2022"),
    ]
    write_ratings(ratings, rows)
    import_imdb_csv(db, ratings)
    build_profile(db)

    ident = identity_key("Schindler's List", "Schindler's List", 1993, "Movie")
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
               year,title_type,runtime_min,genres_json,directors_json,countries_json,overview,keywords_json,
               semantic_json,imdb_rating,num_votes,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9998052", ident, "Schindler's List", "Schindler's List",
                normalize_text("Schindler's List"), normalize_text("Schindler's List"), 1993, "Movie", 196,
                json_dumps(["Biography", "Drama", "History"]), json_dumps(["Steven Spielberg"]),
                json_dumps(["United States"]), "Holocaust history", json_dumps([]),
                json_dumps({"history": 1.0, "holocaust": 1.0}), 9.0, 1500000,
                "imdb_dataset", now, now,
            ),
        )

    cat = tmp_path / "candidates.csv"
    lines = ["imdb_id,title,year,title_type,runtime,genres,directors,imdb_rating,num_votes,overview"]
    for i in range(1, 21):
        lines.append(
            f"tt820{i:04d},Candidate {i},2024,Movie,105,History,Director Test,{7.0 + (i % 5) * .2:.1f},{20000 + i * 1000},history drama"
        )
    cat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import_catalog_csv(db, cat)

    engine = FastRecommendationEngine(db)
    recs = engine.recommend(date(2026, 9, 14), 10, candidate_limit=100000)
    assert recs
    assert all(r.movie.title != "Schindler's List" for r in recs)
    assert all(r.movie.imdb_id not in {"tt0108052", "tt9998052"} for r in recs)


def test_candidate_scoring_is_hard_bounded(tmp_path):
    db = Database(tmp_path / "bounded.db")
    engine = FastRecommendationEngine(db)
    assert engine._effective_limit(100000, "decide") == 1800
    assert engine._effective_limit(45000, "decide") == 1800
    assert engine._effective_limit(100000, "surprise") == 3000


def test_alt_film_uses_cached_decision_pool(tmp_path, monkeypatch):
    db = Database(tmp_path / "cache.db")
    ratings = tmp_path / "ratings.csv"
    write_ratings(ratings, [rated(f"tt83000{i:02d}", 8 + i % 3, f"Rated {i}") for i in range(1, 8)])
    import_imdb_csv(db, ratings)
    build_profile(db)

    cat = tmp_path / "catalog.csv"
    lines = ["imdb_id,title,year,title_type,runtime,genres,directors,imdb_rating,num_votes,overview"]
    for i in range(1, 60):
        lines.append(
            f"tt840{i:04d},Choice {i},2024,Movie,100,History,Director Test,{7.0 + (i % 8) * .1:.1f},{10000 + i * 500},history drama"
        )
    cat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import_catalog_csv(db, cat)

    engine = FastRecommendationEngine(db)
    first, _ = engine.decision_pick(date(2026, 9, 14))
    assert first is not None

    def must_not_recalculate(*args, **kwargs):
        raise AssertionError("Alt film recalculated the full recommendation pool")

    monkeypatch.setattr(engine, "recommend", must_not_recalculate)
    second, _ = engine.decision_pick(date(2026, 9, 14), {int(first.movie.id)})
    assert second is not None
    assert second.movie.id != first.movie.id
