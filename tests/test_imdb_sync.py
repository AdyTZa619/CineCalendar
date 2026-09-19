from __future__ import annotations

from pathlib import Path

import pytest

from cinecalendar.db import Database
from cinecalendar.imdb_sync import (
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
