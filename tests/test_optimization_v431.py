from __future__ import annotations

from cinecalendar.db import Database, SCHEMA_VERSION
from cinecalendar.learning_insight_v43 import (
    TextSemanticBrainV43,
    learning_insight_engine_class,
    text_signature_cache_info,
    text_signature_from_values,
)
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommendation_history_v43 import recommendation_history_rows
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


def _movie(db: Database, idx: int, title: str) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{8800000+idx:07d}",
                identity_key(title, title, 2026, "movie"),
                title,
                title,
                normalize_text(title),
                normalize_text(title),
                2026,
                "movie",
                100,
                json_dumps(["Drama"]),
                "[]",
                "[]",
                "A deliberately unique benchmark synopsis about a monastery archive.",
                json_dumps(["monastery archive"]),
                "{}",
                7.0,
                1000,
                "test",
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def test_schema_v9_adds_read_path_indexes(tmp_path):
    db = Database(tmp_path / "schema-v9.db")
    assert SCHEMA_VERSION == 9
    with db.connect() as con:
        current = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        hist = {row[1] for row in con.execute("PRAGMA index_list(recommendation_history)")}
        trust = {row[1] for row in con.execute("PRAGMA index_list(recommendation_trust_audit)")}
        outcomes = {row[1] for row in con.execute("PRAGMA index_list(recommendation_outcomes)")}
    assert current == 9
    assert "ix_rec_hist_root_context_id" in hist
    assert "ix_rec_hist_exposure_action_id" in hist
    assert "ix_rec_trust_engine_history" in trust
    assert "ix_rec_outcome_engine_context" in outcomes
    assert "ix_rec_outcome_context" in outcomes


def test_semantic_signature_cache_reuses_identical_payload():
    overview = "Unique v431 cache probe monastery pilgrimage archive mountain contemplation 431999"
    keywords = ["unique-v431-probe", "monastery pilgrimage"]
    before = text_signature_cache_info()
    first = text_signature_from_values(overview, keywords)
    middle = text_signature_cache_info()
    second = text_signature_from_values(overview, keywords)
    after = text_signature_cache_info()
    assert first == second
    assert middle["misses"] >= before["misses"] + 1
    assert after["hits"] >= middle["hits"] + 1


def test_semantic_state_token_invalidates_when_rated_movie_metadata_changes(tmp_path):
    db = Database(tmp_path / "semantic-token.db")
    movie_id = _movie(db, 1, "Metadata Token")
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, 8, "2026-09-20", "test", now, now),
        )
    brain = TextSemanticBrainV43(db)
    before = brain.state_token()
    with db.tx() as con:
        con.execute(
            "UPDATE movies SET overview=?,updated_at=? WHERE id=?",
            ("Changed synopsis with different semantic evidence.", "2026-09-21T01:00:00+00:00", movie_id),
        )
    after = brain.state_token()
    assert after != before


def test_history_status_filter_is_applied_before_limit(tmp_path):
    db = Database(tmp_path / "history-filter.db")
    rated_movie = _movie(db, 10, "Older Rated")
    shown_1 = _movie(db, 11, "Newer Shown 1")
    shown_2 = _movie(db, 12, "Newer Shown 2")
    with db.tx() as con:
        rated = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action,predicted_rating,confidence
               ) VALUES(?,?,?,?,?,0,NULL,?,?)""",
            (rated_movie, "2026-09-18T18:00:00+00:00", "2026-09-18", "decision", .8, 8.0, .7),
        )
        rated_id = int(rated.lastrowid)
        con.execute(
            """INSERT INTO recommendation_outcomes(
                   exposure_history_id,movie_id,rank_position,context_date,slot,
                   actual_rating,rating_date,predicted_rating,confidence,final_score,
                   engine_version,absolute_error,resolved_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rated_id, rated_movie, 1, "2026-09-18", "decision",
                9, "2026-09-18", 8.0, .7, .8,
                "learning-insight-v4.3.1", 1.0,
                "2026-09-18T22:00:00+00:00", "2026-09-18T22:00:00+00:00",
            ),
        )
        for movie_id, day in ((shown_1, "2026-09-19"), (shown_2, "2026-09-20")):
            con.execute(
                """INSERT INTO recommendation_history(
                       movie_id,recommended_at,context_date,slot,final_score,ignored,action,predicted_rating,confidence
                   ) VALUES(?,?,?,?,?,0,NULL,?,?)""",
                (movie_id, day + "T18:00:00+00:00", day, "decision", .7, 7.0, .6),
            )

    rows = recommendation_history_rows(db, status="rated", limit=1)
    assert len(rows) == 1
    assert rows[0]["history_id"] == rated_id
    assert rows[0]["status"] == "rated"


def test_learning_models_are_prepared_once_per_recommendation_round(tmp_path):
    db = Database(tmp_path / "prepare-once.db")

    class BaseEngine:
        def __init__(self, db):
            self.db = db

        def _state_token(self):
            return ("base",)

        def recommend(self, **_kwargs):
            recs = []
            for idx in range(3):
                movie = Movie(
                    id=idx + 1,
                    title=f"Candidate {idx}",
                    overview="monastery pilgrimage contemplation",
                    keywords=["monastery"],
                    genres=["Drama"],
                )
                score = ScoreBreakdown(
                    final=.80 - idx * .01,
                    predicted_rating=8.0 - idx * .1,
                    confidence=.70,
                    score_factors={},
                )
                recs.append(Recommendation(movie, score))
            return recs

    Engine = learning_insight_engine_class(BaseEngine)
    engine = Engine(db)
    calls = {"prepare": 0}
    original = engine.learning_insight_v43.prepare

    def counted_prepare():
        calls["prepare"] += 1
        original()

    engine.learning_insight_v43.prepare = counted_prepare
    recs = engine.recommend(count=3)
    assert len(recs) == 3
    assert calls["prepare"] == 1
    runtime = engine.learning_insight_v43.runtime_status()
    assert runtime["prepare"]["count"] == 1
    assert runtime["enhance"]["count"] == 3
