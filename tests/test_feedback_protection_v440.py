from __future__ import annotations

from pathlib import Path

from cinecalendar.adaptive_preferences import AdaptivePreferenceLearner
from cinecalendar.collaborative_als import _FEEDBACK_CONFIDENCE
from cinecalendar.db import Database
from cinecalendar.feedback import (
    FEEDBACK_WEIGHTS,
    apply_feedback,
    apply_feedback_with_receipt,
    undo_feedback,
)
from cinecalendar.profile import build_profile
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso
from cinecalendar.watch_intent import _FEEDBACK_SIGNALS as LEGACY_INTENT_SIGNALS
from cinecalendar.watch_success import _FEEDBACK_SIGNALS as WATCH_SUCCESS_SIGNALS


ROOT = Path(__file__).resolve().parents[1]


def _movie(db: Database, idx: int, title: str, *, rated: int | None = None) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{9400000+idx:07d}",
                identity_key(title, title, 2024, "movie"),
                title,
                title,
                normalize_text(title),
                normalize_text(title),
                2024,
                "movie",
                112,
                json_dumps(["Drama", "History"]),
                json_dumps(["Regizor Test"]),
                json_dumps(["Romania"]),
                "A historical drama about faith and duty.",
                json_dumps(["history", "faith"]),
                json_dumps({"history": 1.0, "faith": 0.8}),
                7.4,
                25_000,
                "test",
                now,
                now,
            ),
        )
        movie_id = int(cur.lastrowid)
        if rated is not None:
            con.execute(
                """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (movie_id, int(rated), "2026-09-01", "test", now, now),
            )
        return movie_id


def test_hide_exact_title_does_not_poison_similar_title_features(tmp_path):
    db = Database(tmp_path / "title-only.db")
    _movie(db, 1, "Rated anchor", rated=9)
    hidden_id = _movie(db, 2, "Hidden candidate")
    before = build_profile(db)

    apply_feedback(db, hidden_id, "not_interested")
    after = build_profile(db)

    assert FEEDBACK_WEIGHTS["not_interested"] == 0.0
    assert after["features"] == before["features"]
    with db.connect() as con:
        row = con.execute(
            "SELECT kind,weight FROM feedback WHERE movie_id=? ORDER BY id DESC LIMIT 1",
            (hidden_id,),
        ).fetchone()
    assert row["kind"] == "not_interested"
    assert float(row["weight"]) == 0.0


def test_only_explicit_similarity_actions_train_similarity_models(tmp_path):
    db = Database(tmp_path / "learners.db")
    hidden_id = _movie(db, 3, "Exact title only")
    apply_feedback(db, hidden_id, "not_interested")

    learner = AdaptivePreferenceLearner(db)
    assert learner._feedback_samples() == []
    assert "not_interested" not in learner._FEEDBACK_TARGETS
    assert "not_interested" not in _FEEDBACK_CONFIDENCE
    assert "not_interested" not in LEGACY_INTENT_SIGNALS
    assert "not_interested" not in WATCH_SUCCESS_SIGNALS


def test_undo_restores_watchlist_and_can_walk_back_multiple_feedback_events(tmp_path):
    db = Database(tmp_path / "undo.db")
    movie_id = _movie(db, 4, "Undo candidate")

    _profile, wanted = apply_feedback_with_receipt(db, movie_id, "want_to_watch")
    with db.connect() as con:
        assert con.execute("SELECT 1 FROM watchlist WHERE movie_id=?", (movie_id,)).fetchone()

    _profile, hidden = apply_feedback_with_receipt(db, movie_id, "not_interested")
    with db.connect() as con:
        assert con.execute("SELECT 1 FROM watchlist WHERE movie_id=?", (movie_id,)).fetchone() is None

    assert undo_feedback(db, hidden.feedback_id) == hidden
    with db.connect() as con:
        assert con.execute("SELECT 1 FROM watchlist WHERE movie_id=?", (movie_id,)).fetchone()

    assert undo_feedback(db, wanted.feedback_id) == wanted
    with db.connect() as con:
        assert con.execute("SELECT 1 FROM watchlist WHERE movie_id=?", (movie_id,)).fetchone() is None
        assert con.execute("SELECT COUNT(*) FROM feedback WHERE movie_id=?", (movie_id,)).fetchone()[0] == 0


def test_production_ui_explains_title_only_hide_and_exposes_undo():
    base = (ROOT / "cinecalendar" / "qt_ui.py").read_text(encoding="utf-8")
    premium = (ROOT / "cinecalendar" / "premium_ui.py").read_text(encoding="utf-8")

    assert "Ascunde doar filmul" in base
    assert "Ascunde doar filmul (nu schimbă gustul)" in premium
    assert "Anulează ultimul feedback" in base
    assert "QKeySequence.StandardKey.Undo" in base
    assert "undo_feedback(self.db, receipt.feedback_id)" in base
