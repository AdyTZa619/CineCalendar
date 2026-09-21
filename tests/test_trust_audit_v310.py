from __future__ import annotations

from datetime import date
import json

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.recommender_v16 import (
    FastRecommendationEngineV16,
    recommendation_engine_identity,
)
from cinecalendar.trust_audit import build_trust_outcome_audit
from cinecalendar.util import utcnow_iso


class _Collaborative:
    def __init__(self, scores=None, ready=True):
        self.scores = dict(scores or {})
        self.ready = ready

    def is_ready(self):
        return self.ready

    def score_candidates(self, imdb_ids):
        return ({iid: self.scores[iid] for iid in imdb_ids if iid in self.scores}, {}, 500)


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
                imdb_id, imdb_id, title, title, 2024, "movie", 105,
                json.dumps(["Drama"]), "[]", "[]", "", "[]", "{}",
                7.2, 20000, "test", now, now, title.lower(), title.lower(),
            ),
        )
        return int(cur.lastrowid)


def _rec(movie_id: int, imdb_id: str, *, final: float = 0.80, predicted: float = 7.8,
         confidence: float = 0.75, evidence: float = 3.0, startability: float = 0.68,
         imdb_rating: float = 7.2, votes: int = 20000) -> Recommendation:
    return Recommendation(
        movie=Movie(
            id=movie_id,
            imdb_id=imdb_id,
            title=f"Movie {movie_id}",
            year=2024,
            title_type="movie",
            runtime_min=105,
            genres=["Drama"],
            imdb_rating=imdb_rating,
            num_votes=votes,
        ),
        score=ScoreBreakdown(
            final=final,
            predicted_rating=predicted,
            confidence=confidence,
            evidence=evidence,
            startability=startability,
            personal_reason="base",
        ),
    )


def _event(db: Database, movie_id: int, action: str) -> None:
    now = utcnow_iso()
    with db.tx() as con:
        con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action
               ) VALUES(?,?,?,?,?,?,?)""",
            (movie_id, now, date.today().isoformat(), "today", 0.8, 0, action),
        )


def test_quality_gate_attaches_persistable_status_to_selected_results():
    engine = object.__new__(FastRecommendationEngineV16)
    engine.collaborative = _Collaborative({"tt9100001": 0.94, "tt9100002": 0.40})
    engine._quality_gate_stats = {}
    strong = _rec(1, "tt9100001", final=0.80, predicted=8.3, confidence=0.84, evidence=4.2)
    weak = _rec(
        2, "tt9100002", final=0.79, predicted=6.0, confidence=0.34,
        evidence=0.2, startability=0.50, imdb_rating=6.0, votes=300,
    )

    selected = engine._quality_gate([weak, strong], 2)

    assert selected[0].score.trust_audit["status"] == "trusted"
    assert selected[0].score.trust_audit["trust"] >= engine.TRUST_MIN_SCORE
    assert selected[1].score.trust_audit["status"] == "backfill"


def test_v16_recording_uses_real_engine_version_and_persists_trust_snapshot(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    first_id = _movie(db, "tt9200001", "First")
    second_id = _movie(db, "tt9200002", "Second")
    first = _rec(first_id, "tt9200001")
    second = _rec(second_id, "tt9200002", final=0.76, predicted=6.6, confidence=0.48, evidence=0.8)
    first.score.trust_audit = {
        "status": "trusted",
        "trust": 0.78,
        "gate_score": 0.817,
        "supports": ["rating personal", "încredere personală", "ALS"],
        "red_flag": False,
        "red_reason": "",
        "gap": 0.0,
        "als": 0.91,
        "public_bayes": 7.05,
    }
    second.score.trust_audit = {
        "status": "backfill",
        "trust": 0.56,
        "gate_score": 0.764,
        "supports": ["rating personal"],
        "red_flag": False,
        "red_reason": "",
        "gap": 0.04,
        "als": 0.52,
        "public_bayes": 6.8,
    }
    engine = object.__new__(FastRecommendationEngineV16)
    engine.db = db

    engine._record_selected([first, second], date.today(), "today", candidate_count=24)

    with db.connect() as con:
        run = con.execute(
            "SELECT candidate_count,result_count,engine_version FROM recommendation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        rows = con.execute(
            """SELECT rank_position,trust_status,trust_score,support_count,engine_version
               FROM recommendation_trust_audit ORDER BY rank_position"""
        ).fetchall()
    identity = recommendation_engine_identity(engine)
    assert run["engine_version"] == identity
    assert run["candidate_count"] == 24
    assert run["result_count"] == 2
    assert [row["trust_status"] for row in rows] == ["trusted", "backfill"]
    assert [row["rank_position"] for row in rows] == [1, 2]
    assert rows[0]["support_count"] == 3
    assert rows[0]["engine_version"] == identity


def test_trust_audit_correlates_gate_status_with_real_watch_outcomes(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    trusted_id = _movie(db, "tt9300001", "Trusted")
    backfill_id = _movie(db, "tt9300002", "Backfill")
    trusted = _rec(trusted_id, "tt9300001")
    backfill = _rec(backfill_id, "tt9300002", final=0.76)
    trusted.score.trust_audit = {
        "status": "trusted", "trust": 0.81, "gate_score": 0.819,
        "supports": ["rating personal", "ALS", "calitate publică"], "red_flag": False,
    }
    backfill.score.trust_audit = {
        "status": "backfill", "trust": 0.55, "gate_score": 0.763,
        "supports": ["rating personal"], "red_flag": False,
    }
    engine = object.__new__(FastRecommendationEngineV16)
    engine.db = db
    engine._record_selected([trusted, backfill], date.today(), "today", candidate_count=20)

    for action in ("stremio_opened", "playback_confirmed", "watched"):
        _event(db, trusted_id, action)
    _event(db, backfill_id, "skip_today")

    report = build_trust_outcome_audit(db, days=30)

    assert report["available"] is True
    assert report["recommendations"] == 2
    assert report["engine_versions"] == {recommendation_engine_identity(engine): 2}
    assert report["by_status"]["trusted"]["confirmed_starts"] == 1
    assert report["by_status"]["trusted"]["watched"] == 1
    assert report["by_status"]["trusted"]["watch_rate"] == 1.0
    assert report["by_status"]["backfill"]["skipped"] == 1
    assert report["by_status"]["backfill"]["skip_rate"] == 1.0
    assert report["enough_data_for_tuning"] is False
