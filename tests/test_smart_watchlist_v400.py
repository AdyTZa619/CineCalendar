from __future__ import annotations

from datetime import date
import json

from cinecalendar.db import Database
from cinecalendar.feedback import apply_feedback
from cinecalendar.models import ScoreBreakdown
from cinecalendar.profile import build_profile
from cinecalendar.smart_watchlist import (
    pinned_watchlist_ids,
    rank_watchlist,
    remove_from_watchlist,
    set_watchlist_pinned,
    watchlist_entries,
)
from cinecalendar.util import utcnow_iso


def _movie(
    db: Database,
    imdb_id: str,
    title: str,
    *,
    year: int = 2025,
    imdb_rating: float = 7.0,
    release_date: str = "2025-01-01",
) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,release_date,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, f"smart-{imdb_id}", title, title, year, "movie", 105,
                json.dumps(["Thriller"]), json.dumps(["Director Test"]), json.dumps(["RO"]),
                "A clear premise.", "[]", "{}", imdb_rating, 50000, release_date,
                "test", now, now, title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def _watch(db: Database, movie_id: int) -> None:
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO watchlist(movie_id,status,added_at,updated_at)
               VALUES(?,'want_to_watch',?,?)""",
            (movie_id, now, now),
        )


def _seed_profile(db: Database) -> None:
    mid = _movie(db, "tt4999999", "Rated Seed", imdb_rating=8.0)
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,9,'2026-09-01','test',?,?)""",
            (mid, now, now),
        )
    build_profile(db)


class _FakeProductionRecommender:
    MIN_PERSONAL_RATINGS = 1
    ADAPTIVE_POOL_MIN = 60

    def __init__(self, db):
        self.db = db
        self.collaborative = None
        self.rerank_called = False
        self.annotate_called = False

    @staticmethod
    def _run_context():
        return {}

    @staticmethod
    def _catalog_quality_is_trustworthy(movie):
        return True

    @staticmethod
    def _score_one(movie, when, profile, context, exclude_romance, mode):
        final = float(movie.imdb_rating or 0.0) / 10.0
        return ScoreBreakdown(
            final=final,
            predicted_rating=float(movie.imdb_rating or 0.0) + 0.5,
            confidence=0.75,
            calendar=0.0,
            calendar_reason="",
            personal_reason=f"personal-{movie.title}",
        )

    @staticmethod
    def _select_candidates(candidates, count, mode):
        return list(candidates[:count])

    def _adaptive_rerank(self, recs, count):
        self.rerank_called = True
        return list(recs[:count])

    def _annotate_final_als(self, selected):
        self.annotate_called = True


def test_smart_watchlist_ranks_only_eligible_available_explicit_items(tmp_path):
    db = Database(tmp_path / "smart.db")
    _seed_profile(db)

    ids = []
    for index, rating in enumerate((6.8, 8.4, 7.2, 9.0, 7.8, 8.1), start=1):
        mid = _movie(db, f"tt41000{index:02d}", f"Candidate {index}", imdb_rating=rating)
        _watch(db, mid)
        ids.append(mid)

    future = _movie(
        db,
        "tt4199991",
        "Future Film",
        year=2027,
        imdb_rating=9.9,
        release_date="2027-03-01",
    )
    _watch(db, future)

    seen = _movie(db, "tt4199992", "Seen Film", imdb_rating=9.8)
    _watch(db, seen)
    with db.tx() as con:
        con.execute(
            "INSERT INTO feedback(movie_id,kind,weight,created_at) VALUES(?,'seen',0,?)",
            (seen, utcnow_iso()),
        )

    engine = _FakeProductionRecommender(db)
    result = rank_watchlist(engine, date(2026, 9, 20), 5)

    assert result.total == 8
    assert result.eligible == 7
    assert result.available == 6
    assert result.future_hidden == 1
    assert len(result.recommendations) == 5
    assert [rec.movie.title for rec in result.recommendations] == [
        "Candidate 4",
        "Candidate 2",
        "Candidate 6",
        "Candidate 5",
        "Candidate 3",
    ]
    assert all(rec.movie.id not in {future, seen} for rec in result.recommendations)
    assert engine.rerank_called
    assert engine.annotate_called


def test_remove_from_watchlist_does_not_create_negative_feedback(tmp_path):
    db = Database(tmp_path / "remove.db")
    mid = _movie(db, "tt4200001", "Remove Me")
    _watch(db, mid)

    assert remove_from_watchlist(db, mid)
    assert not watchlist_entries(db)
    with db.connect() as con:
        kinds = con.execute("SELECT kind FROM feedback WHERE movie_id=?", (mid,)).fetchall()
    assert kinds == []


def test_seen_feedback_removes_stale_watchlist_entry(tmp_path):
    db = Database(tmp_path / "seen.db")
    mid = _movie(db, "tt4300001", "Seen From Watchlist")
    _watch(db, mid)

    apply_feedback(db, mid, "seen")

    with db.connect() as con:
        assert con.execute("SELECT 1 FROM watchlist WHERE movie_id=?", (mid,)).fetchone() is None
        row = con.execute(
            "SELECT kind FROM feedback WHERE movie_id=? ORDER BY id DESC LIMIT 1",
            (mid,),
        ).fetchone()
    assert row["kind"] == "seen"


def test_watchlist_priority_pin_boosts_close_candidate(tmp_path):
    db = Database(tmp_path / "pin.db")
    _seed_profile(db)
    regular = _movie(db, "tt4400001", "Regular", imdb_rating=8.2)
    pinned = _movie(db, "tt4400002", "Pinned", imdb_rating=7.8)
    _watch(db, regular)
    _watch(db, pinned)
    assert set_watchlist_pinned(db, pinned, True)
    assert pinned in pinned_watchlist_ids(db)

    result = rank_watchlist(_FakeProductionRecommender(db), date(2026, 9, 20), 2)
    assert [rec.movie.title for rec in result.recommendations][:2] == ["Pinned", "Regular"]
    assert result.recommendations[0].score.contributions[0][0] == "Prioritate Watchlist"


def test_watchlist_duration_and_documentary_filters(tmp_path):
    db = Database(tmp_path / "filters.db")
    _seed_profile(db)
    short = _movie(db, "tt4500001", "Short Runtime", imdb_rating=8.0)
    long = _movie(db, "tt4500002", "Long Runtime", imdb_rating=8.5)
    documentary = _movie(db, "tt4500003", "Documentary", imdb_rating=8.7)
    for mid in (short, long, documentary):
        _watch(db, mid)
    with db.tx() as con:
        con.execute("UPDATE movies SET runtime_min=80 WHERE id=?", (short,))
        con.execute("UPDATE movies SET runtime_min=145 WHERE id=?", (long,))
        con.execute("UPDATE movies SET genres_json=? WHERE id=?", (json.dumps(["Documentary"]), documentary))

    engine = _FakeProductionRecommender(db)
    short_result = rank_watchlist(engine, date(2026, 9, 20), 5, runtime_bucket="short")
    assert [rec.movie.title for rec in short_result.recommendations] == ["Short Runtime"]

    doc_result = rank_watchlist(engine, date(2026, 9, 20), 5, content_type="documentary")
    assert [rec.movie.title for rec in doc_result.recommendations] == ["Documentary"]

    movie_result = rank_watchlist(engine, date(2026, 9, 20), 5, content_type="movie")
    assert "Documentary" not in [rec.movie.title for rec in movie_result.recommendations]


def test_removing_watchlist_item_clears_priority_pin(tmp_path):
    db = Database(tmp_path / "remove-pin.db")
    mid = _movie(db, "tt4600001", "Pinned Then Removed")
    _watch(db, mid)
    set_watchlist_pinned(db, mid, True)
    assert mid in pinned_watchlist_ids(db)

    assert remove_from_watchlist(db, mid)
    assert mid not in pinned_watchlist_ids(db)
