from __future__ import annotations

from datetime import date
import json

from cinecalendar.db import Database
from cinecalendar.util import utcnow_iso
from cinecalendar.watch_success_audit import build_watch_success_audit


def _movie(db: Database, imdb_id: str, title: str) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, imdb_id, title, title, 2025, "movie", 100,
                json.dumps(["Drama"]), "[]", "[]", "", "[]", "{}",
                7.2, 10000, "test", now, now, title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def _event(db: Database, movie_id: int, action: str) -> None:
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action
               ) VALUES(?,?,?,?,?,?,?)""",
            (movie_id, now, date.today().isoformat(), "test", 0.8, 0, action),
        )


def test_audit_reports_observed_funnel_without_treating_legacy_launch_as_play(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    a = _movie(db, "tt9910001", "A")
    b = _movie(db, "tt9910002", "B")
    c = _movie(db, "tt9910003", "C")

    for action in ("chosen", "stremio_opened", "playback_confirmed", "watched"):
        _event(db, a, action)
    for action in ("chosen", "play_opened"):
        _event(db, b, action)
    for action in ("chosen", "skip_today"):
        _event(db, c, action)

    report = build_watch_success_audit(db, days=30)

    assert report["funnels"] == 3
    assert report["stremio_attempts"] == 2
    assert report["playback_confirmations"] == 1
    assert report["confirmed_starts"] == 1
    assert report["watched"] == 1
    assert report["skipped"] == 1
    assert report["stremio_to_confirmed_rate"] == 0.5
    assert report["confirmed_to_watched_rate"] == 1.0
    assert report["watch_rate_per_funnel"] == round(1 / 3, 4)
    assert report["final_outcomes"]["watched"] == 1
    assert report["final_outcomes"]["stremio_opened"] == 1
    assert report["final_outcomes"]["skip_today"] == 1
    assert report["enough_data_for_tuning"] is False
