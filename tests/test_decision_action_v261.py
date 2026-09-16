from __future__ import annotations

from datetime import date
import json

from cinecalendar.db import Database
from cinecalendar.decision_action_patch import (
    CHOICE_SETTING,
    clear_today_choice,
    current_today_choice,
    record_decision_action,
    set_today_choice,
)
from cinecalendar.util import utcnow_iso
from cinecalendar.watch_intent import WatchIntentLearner


def _movie(db: Database) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   identity_key,title,original_title,year,title_type,runtime_min,
                   genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                   imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "choice-test", "Choice Test", "Choice Test", 2025, "movie", 105,
                json.dumps(["Thriller"]), json.dumps(["Director X"]), json.dumps(["RO"]),
                "", "[]", "{}", 7.4, 25000, "test", now, now, "choice test", "choice test",
            ),
        )
        return int(cur.lastrowid)


def test_choose_does_not_rewrite_stale_historical_exposure(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)
    old = "2020-01-01T00:00:00+00:00"
    with db.tx() as con:
        old_row = con.execute(
            """INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score)
               VALUES(?,?,?,?,?)""",
            (movie_id, old, "2020-01-01", "decision", 0.8),
        )
        old_id = int(old_row.lastrowid)

    token_before = WatchIntentLearner(db).state_token()
    row_id = record_decision_action(db, movie_id, "chosen")
    token_after = WatchIntentLearner(db).state_token()

    with db.connect() as con:
        current = con.execute("SELECT * FROM recommendation_history WHERE id=?", (row_id,)).fetchone()
        historical = con.execute("SELECT * FROM recommendation_history WHERE id=?", (old_id,)).fetchone()
        count = con.execute("SELECT COUNT(*) FROM recommendation_history WHERE movie_id=?", (movie_id,)).fetchone()[0]

    assert count == 2
    assert row_id != old_id
    assert historical["action"] is None
    assert historical["recommended_at"] == old
    assert historical["context_date"] == "2020-01-01"
    assert current["action"] == "chosen"
    assert current["ignored"] == 0
    assert current["context_date"] == date.today().isoformat()
    assert current["exposure_history_id"] is None
    assert token_after != token_before


def test_current_same_day_exposure_is_updated_in_place(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)
    old_click_time = "2020-01-01T00:00:00+00:00"
    today = date.today().isoformat()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score)
               VALUES(?,?,?,?,?)""",
            (movie_id, old_click_time, today, "decision", 0.8),
        )
        exposure_id = int(cur.lastrowid)

    row_id = record_decision_action(db, movie_id, "chosen")
    with db.connect() as con:
        row = con.execute("SELECT * FROM recommendation_history WHERE id=?", (row_id,)).fetchone()
        count = con.execute("SELECT COUNT(*) FROM recommendation_history WHERE movie_id=?", (movie_id,)).fetchone()[0]

    assert row_id == exposure_id
    assert count == 1
    assert row["action"] == "chosen"
    assert row["recommended_at"] != old_click_time
    assert row["context_date"] == today


def test_chosen_then_changed_mind_keeps_both_explicit_intent_events(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)
    first_id = record_decision_action(db, movie_id, "chosen")
    token_after_choose = WatchIntentLearner(db).state_token()
    second_id = record_decision_action(db, movie_id, "skip_today")
    token_after_skip = WatchIntentLearner(db).state_token()

    assert second_id > first_id
    assert token_after_skip != token_after_choose
    with db.connect() as con:
        rows = con.execute(
            "SELECT action,ignored FROM recommendation_history WHERE movie_id=? ORDER BY id", (movie_id,)
        ).fetchall()
    assert [(r["action"], r["ignored"]) for r in rows] == [("chosen", 0), ("skip_today", 1)]


def test_today_choice_is_visible_state_and_can_be_cleared(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    movie_id = _movie(db)

    set_today_choice(db, movie_id)
    payload = db.get_setting(CHOICE_SETTING, {})
    selected = current_today_choice(db)

    assert payload["date"] == date.today().isoformat()
    assert payload["movie_id"] == movie_id
    assert selected is not None
    assert selected.id == movie_id
    assert selected.title == "Choice Test"

    clear_today_choice(db, movie_id)
    assert current_today_choice(db) is None
