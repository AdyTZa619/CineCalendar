from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from cinecalendar.db import Database
from cinecalendar.metadata_consistency_v41 import audit_metadata_consistency
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.personalization_v41 import PersonalizationBrainV41
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


def _add_rating(
    db: Database,
    idx: int,
    rating: int,
    *,
    genre: str = "Drama",
    director: str = "Director",
    title_type: str = "movie",
    rated_date: str = "2026-01-01",
    runtime: int | None = 100,
    original_title: str | None = None,
) -> int:
    now = utcnow_iso()
    title = f"Film {idx}"
    original = original_title if original_title is not None else title
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,runtime_min,genres_json,directors_json,countries_json,
                imdb_rating,num_votes,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{7000000+idx:07d}",
                identity_key(title, original or title, 2020 + idx % 5, title_type),
                title,
                original,
                normalize_text(title),
                normalize_text(original or title),
                2020 + idx % 5,
                title_type,
                runtime,
                json_dumps([genre]),
                json_dumps([director]),
                json_dumps(["Romania"]),
                7.0,
                10000,
                "test",
                now,
                now,
            ),
        )
        mid = int(cur.lastrowid)
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (mid, rating, rated_date, "test", now, now),
        )
    return mid


def test_v41_quality_gate_approves_clear_personal_signal(tmp_path):
    db = Database(tmp_path / "v41-quality.db")
    start = date(2025, 1, 1)
    for idx in range(160):
        genre = "Action" if idx % 2 == 0 else "Drama"
        rating = 9 if genre == "Action" else 3
        _add_rating(
            db,
            idx,
            rating,
            genre=genre,
            director=f"Director {idx%8}",
            rated_date=(start + timedelta(days=idx)).isoformat(),
        )

    brain = PersonalizationBrainV41(db)
    status = brain.status()
    gate = status["quality_gate"]
    assert gate["approved"] is True
    assert gate["model_mae"] < gate["baseline_mae"]
    assert gate["holdout_count"] >= 30


def test_v41_detects_taste_evolution_and_content_profiles(tmp_path):
    db = Database(tmp_path / "v41-trend.db")
    idx = 0
    # Older horror was liked; recent horror is disliked. Comedy moves the other way.
    for year, horror_rating, comedy_rating in (
        (2021, 9, 3),
        (2022, 8, 4),
        (2026, 3, 9),
    ):
        for repeat in range(8):
            idx += 1
            _add_rating(db, idx, horror_rating, genre="Horror", rated_date=f"{year}-03-{repeat+1:02d}")
            idx += 1
            _add_rating(db, idx, comedy_rating, genre="Comedy", rated_date=f"{year}-04-{repeat+1:02d}")

    brain = PersonalizationBrainV41(db)
    evolution = {row["feature"]: row for row in brain.taste_evolution(20)}
    assert evolution["genre:horror"]["trend"] < 0
    assert evolution["genre:comedy"]["trend"] > 0

    status = brain.status()
    assert "movie" in status["content_profiles"]
    assert status["content_profiles"]["movie"]["count"] == 48


def test_v41_adds_concrete_evidence_explanation_and_why_not(tmp_path):
    db = Database(tmp_path / "v41-explain.db")
    for idx, rating in enumerate((9, 8, 2, 3), start=1):
        _add_rating(
            db,
            idx,
            rating,
            genre="Mystery",
            director="Same Director",
            rated_date=f"2026-01-{idx:02d}",
        )

    brain = PersonalizationBrainV41(db)
    brain._ensure()
    # Force only the bounded layer on for this unit test; quality gate itself is tested separately.
    brain._quality = {**brain._quality, "approved": True}
    movie = Movie(
        id=999,
        title="Candidate",
        original_title="Candidate",
        year=2024,
        title_type="movie",
        runtime_min=105,
        genres=["Mystery"],
        directors=["Same Director"],
        countries=["Romania"],
        imdb_rating=7.2,
    )
    score = ScoreBreakdown(
        final=.72,
        taste=.70,
        semantic=.66,
        calendar=.0,
        season=.5,
        director_cinema=.8,
        novelty=.7,
        quality=.7,
        predicted_rating=6.0,
        confidence=.45,
        personal_reason="Potrivire de bază.",
    )

    brain.enhance_score(movie, score, date(2026, 9, 20))
    assert "Repere concrete:" in score.personal_reason
    assert "9/10" in score.personal_reason or "8/10" in score.personal_reason
    assert score.why_not
    assert "gust" in score.score_factors
    assert score.content_profile == "movie"


def test_v41_anti_repetition_penalizes_duplicate_visible_lane(tmp_path):
    db = Database(tmp_path / "v41-diversity.db")
    for idx in range(130):
        _add_rating(
            db,
            idx,
            8 if idx % 2 == 0 else 6,
            genre="Drama" if idx % 2 == 0 else "Comedy",
            director=f"Director {idx%12}",
            rated_date=(date(2025, 1, 1) + timedelta(days=idx)).isoformat(),
        )

    brain = PersonalizationBrainV41(db)
    brain._ensure()
    brain._quality = {**brain._quality, "approved": True}

    def rec(mid, title, director, genre, final):
        return Recommendation(
            Movie(
                id=mid,
                title=title,
                original_title=title,
                year=2024,
                title_type="movie",
                genres=[genre],
                directors=[director],
                countries=["Romania"],
                imdb_rating=7.5,
            ),
            ScoreBreakdown(
                final=final,
                predicted_rating=8.0,
                confidence=.8,
                taste=.8,
                semantic=.8,
                quality=.8,
            ),
        )

    a = rec(1001, "A", "Repeat Director", "Drama", .90)
    b = rec(1002, "B", "Repeat Director", "Drama", .895)
    c = rec(1003, "C", "Other Director", "Comedy", .885)
    selected = brain.enhance_and_diversify([a, b, c], date(2026, 9, 20), 3)
    assert selected[0].movie.title == "A"
    assert selected[1].movie.title == "C"
    assert any(name == "Anti-repetiție 4.1" for name, _pts, _reason in b.score.contributions)


def test_v41_runtime_chooser_bounds(tmp_path):
    db = Database(tmp_path / "v41-runtime.db")
    brain = PersonalizationBrainV41(db)

    db.set_setting("chooser_runtime_bucket", "90")
    assert brain.choose_runtime_bounds() == (90, None)
    db.set_setting("chooser_runtime_bucket", "180plus")
    assert brain.choose_runtime_bounds() == (None, 181)
    db.set_setting("chooser_runtime_bucket", "all")
    assert brain.choose_runtime_bounds() == (None, None)


def test_metadata_consistency_audit_flags_real_suspicious_values(tmp_path):
    db = Database(tmp_path / "metadata-audit.db")
    _add_rating(
        db,
        1,
        8,
        genre="Drama",
        runtime=999,
        original_title="",
    )
    report = audit_metadata_consistency(db)
    assert report.issue_count >= 1
    assert any("titlu original lipsă" in issue for issue in report.issues)
    assert any("durată suspectă" in issue for issue in report.issues)


def test_production_stack_composes_v41_personalization():
    root = Path(__file__).resolve().parents[1]
    production = (root / "cinecalendar" / "production_engine.py").read_text(encoding="utf-8")
    premium = (root / "cinecalendar" / "premium_ui.py").read_text(encoding="utf-8")
    smart = (root / "cinecalendar" / "smart_watchlist.py").read_text(encoding="utf-8")

    assert "personalization_engine_class(context_cls)" in production
    assert "personalization_status" in production
    assert "chooser_runtime_bucket" in premium
    assert "chooser_mood" in premium
    assert "Compromisuri" in premium
    assert "Prioritate Watchlist dinamică" in smart
