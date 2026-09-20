from __future__ import annotations

from pathlib import Path

import pytest

from cinecalendar.db import Database
from cinecalendar.imdb_sync import (
    backfill_public_rating_metadata,
    fetch_public_ratings,
    resolve_public_user_id,
    sync_public_ratings,
    user_id_from_profile_url,
)


URL = "https://www.imdb.com/user/p.666yozwb6likjcvvjlu2hwmtli/ratings/"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payloads.pop(0))


def profile_payload(user_id="ur123456"):
    return {"data": {"userProfile": {"userId": user_id}}}


def session_for(*payloads):
    return Session([profile_payload(), *payloads])


def payload(items, next_cursor=None):
    return {"data": {"userRatings": {
        "edges": [{"node": {
            "userRating": {"value": rating, "date": rated},
            "title": {
                "id": iid,
                "titleText": {"text": title},
                "originalTitleText": {"text": title},
                "releaseYear": {"year": year},
                "titleType": {"text": "Movie"},
            },
        }} for iid, title, rating, rated, year in items],
        "pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor},
    }}}


def test_profile_url_is_strict():
    assert user_id_from_profile_url(URL) == "p.666yozwb6likjcvvjlu2hwmtli"
    with pytest.raises(ValueError):
        user_id_from_profile_url("https://example.com/user/p.bad/ratings/")


def test_resolves_modern_public_profile_id_before_ratings():
    s = Session([profile_payload("ur7654321")])
    assert resolve_public_user_id("p.666yozwb6likjcvvjlu2hwmtli", session=s) == "ur7654321"
    assert s.calls[0][1]["json"]["variables"] == {"profileId": "p.666yozwb6likjcvvjlu2hwmtli"}


def test_fetch_paginates_and_deduplicates():
    s = session_for(
        payload([("tt1000001", "A", 8, "2026-09-10", 2020)], "next"),
        payload([
            ("tt1000001", "A", 8, "2026-09-10", 2020),
            ("tt1000002", "B", 7, "2026-09-11", 2021),
        ]),
    )
    rows = fetch_public_ratings(URL, session=s)
    assert [r.imdb_id for r in rows] == ["tt1000001", "tt1000002"]
    assert len(s.calls) == 3
    assert s.calls[1][1]["json"]["variables"]["userId"] == "ur123456"


def test_sync_only_after_csv_baseline_and_is_idempotent(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    s = session_for(payload([
        ("tt1000001", "Old", 6, "2026-09-05", 2020),
        ("tt1000002", "New", 9, "2026-09-06", 2021),
    ]))
    r = sync_public_ratings(db, URL, session=s)
    assert r.new_ratings == [("New", 9)]
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 1

    s2 = session_for(payload([("tt1000002", "New", 9, "2026-09-06", 2021)]))
    r2 = sync_public_ratings(db, URL, session=s2)
    assert not r2.new_ratings
    assert r2.unchanged == 1


def test_sync_updates_changed_rating_without_duplicate(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    sync_public_ratings(
        db,
        URL,
        session=session_for(payload([
            ("tt1000003", "Changed", 7, "2026-09-07", 2022),
        ])),
    )
    r = sync_public_ratings(
        db,
        URL,
        session=session_for(payload([
            ("tt1000003", "Changed", 8, "2026-09-08", 2022),
        ])),
    )
    assert r.changed_ratings == [("Changed", 7, 8)]
    with db.connect() as con:
        row = con.execute("SELECT rating,date_rated FROM ratings").fetchone()
        assert tuple(row) == (8, "2026-09-08")


def test_graphql_errors_never_touch_database(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    with pytest.raises(RuntimeError):
        sync_public_ratings(
            db,
            URL,
            session=Session([
                {"errors": [{"message": "private"}]},
                {"errors": [{"message": "private"}]},
                {"errors": [{"message": "private"}]},
                {"errors": [{"message": "private"}]},
            ]),
        )
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 0


def test_legacy_rating_shape_is_still_accepted():
    legacy = {"data": {"userRatings": {
        "edges": [{"node": {
            "rating": 8,
            "date": "2026-09-12",
            "title": {
                "id": "tt1000099",
                "titleText": {"text": "Legacy"},
                "originalTitleText": {"text": "Legacy"},
                "releaseYear": {"year": 2024},
                "titleType": {"text": "Movie"},
            },
        }}],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }}}
    rows = fetch_public_ratings(URL, session=session_for(legacy))
    assert len(rows) == 1
    assert rows[0].rating == 8
    assert rows[0].date_rated == "2026-09-12"


def test_full_profile_sync_imports_pre_baseline_ratings(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    s = session_for(payload([
        ("tt1000100", "Historical Rating", 10, "2020-01-02", 1989),
    ]))
    r = sync_public_ratings(db, URL, baseline_date=None, session=s)
    assert r.new_ratings == [("Historical Rating", 10)]
    with db.connect() as con:
        row = con.execute(
            "SELECT m.imdb_id,r.rating,r.date_rated FROM ratings r JOIN movies m ON m.id=r.movie_id"
        ).fetchone()
        assert tuple(row) == ("tt1000100", 10, "2020-01-02")


def test_ui_sync_uses_full_public_profile_history():
    root = Path(__file__).resolve().parents[1]
    ui = (root / "cinecalendar" / "qt_ui.py").read_text(encoding="utf-8")
    sync_block = ui[ui.index("def sync_imdb_public"):ui.index("def manual_rating")]
    assert "baseline_date=None" in sync_block


def test_public_sync_sends_required_imdb_web_headers():
    session = session_for(payload([
        ("tt1000200", "Headers", 8, "2026-09-19", 2025),
    ]))
    fetch_public_ratings(URL, session=session)
    for _url, call in session.calls:
        headers = call["headers"]
        assert headers["Origin"] == "https://www.imdb.com"
        assert headers["Referer"] == "https://www.imdb.com/"
        assert headers["x-imdb-client-name"] == "imdb-web-next"
        assert "application/graphql+json" in headers["Accept"]


def test_full_profile_prunes_stale_csv_rating_with_same_title_year(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    now = "2026-05-23T00:00:00+00:00"
    with db.tx() as con:
        old = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,genres_json,directors_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt1861356","jana|jana|2004|movie","Jana","Jana","jana","jana",
                2004,"Movie","[\"Action\",\"Drama\"]","[\"Shaji Kailas\"]",
                "imdb_csv",now,now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(old.lastrowid),5,"2026-05-23","imdb",now,now),
        )

    result = sync_public_ratings(
        db,
        URL,
        baseline_date=None,
        session=session_for(payload([
            ("tt4568192","Jana",5,"2026-05-23",2004),
        ])),
    )

    assert result.removed_ratings == [("Jana", 5, "tt1861356")]
    with db.connect() as con:
        rows = con.execute(
            """SELECT m.imdb_id,r.rating,r.source
               FROM ratings r JOIN movies m ON m.id=r.movie_id
               ORDER BY m.imdb_id"""
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("tt4568192", 5, "imdb_public_sync"),
    ]


def test_full_profile_keeps_two_distinct_same_title_year_movies_if_both_are_live(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    result = sync_public_ratings(
        db,
        URL,
        baseline_date=None,
        session=session_for(payload([
            ("tt1861356","Jana",5,"2026-05-23",2004),
            ("tt4568192","Jana",5,"2026-05-23",2004),
        ])),
    )
    assert result.removed_ratings == []
    with db.connect() as con:
        ids = [
            row[0] for row in con.execute(
                """SELECT m.imdb_id
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   ORDER BY m.imdb_id"""
            ).fetchall()
        ]
    assert ids == ["tt1861356","tt4568192"]


def test_full_profile_never_prunes_manual_rating(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    now = "2026-01-01T00:00:00+00:00"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,genres_json,directors_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt9999999","manual|manual|2020|movie","Manual","Manual","manual","manual",
                2020,"Movie","[]","[]","manual",now,now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(cur.lastrowid),7,"2026-01-01","manual",now,now),
        )

    sync_public_ratings(
        db,
        URL,
        baseline_date=None,
        session=session_for(payload([
            ("tt1000999","Other",8,"2026-09-01",2024),
        ])),
    )
    with db.connect() as con:
        manual = con.execute(
            """SELECT r.rating FROM ratings r JOIN movies m ON m.id=r.movie_id
               WHERE m.imdb_id='tt9999999'"""
        ).fetchone()
    assert manual is not None and int(manual[0]) == 7


def test_empty_remote_profile_does_not_wipe_existing_imdb_history(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    now = "2026-01-01T00:00:00+00:00"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,genres_json,directors_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt1000001","film|film|2020|movie","Film","Film","film","film",
                2020,"Movie","[]","[]","imdb_csv",now,now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(cur.lastrowid),8,"2026-01-01","imdb",now,now),
        )

    with pytest.raises(RuntimeError):
        sync_public_ratings(
            db,
            URL,
            baseline_date=None,
            session=session_for(payload([])),
        )
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 1


def test_public_sync_metadata_backfill_fills_sparse_profile_rows(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    sync_public_ratings(
        db,
        URL,
        baseline_date=None,
        session=session_for(payload([
            ("tt4568192","Jana",5,"2026-05-23",2004),
        ])),
    )

    metadata = {
        "data": {
            "titles": [{
                "id": "tt4568192",
                "runtime": {"seconds": 4800},
                "ratingsSummary": {"aggregateRating": 4.5, "voteCount": 16},
                "genres": {"genres": [{"text": "Drama"}]},
                "primaryImage": {"url": "https://m.media-amazon.com/images/M/jana.jpg"},
                "principalCredits": [{
                    "category": {"id": "director", "text": "Director"},
                    "credits": [{"name": {"nameText": {"text": "Valeriu Gagiu"}}}],
                }],
            }]
        }
    }
    count = backfill_public_rating_metadata(db, session=Session([metadata]))
    assert count == 1

    with db.connect() as con:
        row = con.execute(
            """SELECT runtime_min,genres_json,directors_json,imdb_rating,num_votes,poster_url
               FROM movies WHERE imdb_id='tt4568192'"""
        ).fetchone()
    assert int(row["runtime_min"]) == 80
    assert row["genres_json"] == '["Drama"]'
    assert row["directors_json"] == '["Valeriu Gagiu"]'
    assert float(row["imdb_rating"]) == 4.5
    assert int(row["num_votes"]) == 16
    assert row["poster_url"] == "https://m.media-amazon.com/images/M/jana.jpg"


def test_full_profile_marks_existing_csv_rating_as_live_confirmed(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    now = "2026-05-23T00:00:00+00:00"
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                year,title_type,genres_json,directors_json,source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "tt1861356","jana|jana|2004|movie","Jana","Jana","jana","jana",
                2004,"Movie","[]","[]","imdb_csv",now,now,
            ),
        )
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (int(cur.lastrowid),5,"2026-05-23","imdb",now,now),
        )

    result = sync_public_ratings(
        db,
        URL,
        baseline_date=None,
        session=session_for(payload([
            ("tt1861356","Jana",5,"2026-05-23",2004),
        ])),
    )
    assert result.removed_ratings == []
    with db.connect() as con:
        source = con.execute(
            """SELECT r.source FROM ratings r JOIN movies m ON m.id=r.movie_id
               WHERE m.imdb_id='tt1861356'"""
        ).fetchone()[0]
    assert source == "imdb_public_sync"
