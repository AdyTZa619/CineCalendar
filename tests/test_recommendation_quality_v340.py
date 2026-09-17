from __future__ import annotations

from cinecalendar.db import Database
from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.personal_candidates_v34 import PersonalCandidateGeneratorV34, RETRIEVAL_VERSION
from cinecalendar.recommendation_backtest import HoldoutRating, quality_verdict, ranking_quality_metrics
from cinecalendar.recommender_v17 import FastRecommendationEngineV17
from cinecalendar.util import json_dumps, utcnow_iso


def _insert_rating(db: Database, imdb_id: str, title: str, rating: int) -> int:
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                 imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                 genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                 imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                imdb_id, f"q34-{imdb_id}", title, title, 2024, "movie", 105,
                json_dumps(["Drama"]), json_dumps([]), json_dumps([]), "", json_dumps([]),
                json_dumps({}), 7.0, 1000, "test", now, now, title.lower(), title.lower(),
            ),
        )
        mid = int(cur.lastrowid)
        con.execute(
            "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
            (mid, int(rating), "2026-09-01", "test", now, now),
        )
        return mid


def test_v34_positive_retrieval_includes_eight_of_ten_but_keeps_low_ratings_negative(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    _insert_rating(db, "tt0000001", "Eight", 8)
    _insert_rating(db, "tt0000002", "Nine", 9)
    _insert_rating(db, "tt0000003", "Bad", 3)
    mapping = {"tt0000001": 11, "tt0000002": 12, "tt0000003": 13}
    generator = PersonalCandidateGeneratorV34(db, collaborative=None)

    positive = generator._anchor_seed_items(mapping, positive=True, limit=20)
    negative = generator._anchor_seed_items(mapping, positive=False, limit=20)

    assert (11, 8) in positive
    assert (12, 9) in positive
    assert all(rating >= 8 for _item, rating in positive)
    assert negative == [(13, 3)]
    assert RETRIEVAL_VERSION.endswith("liked-consensus")


def _rec(mid: int, *, final: float, predicted: float, confidence: float, trust: float,
         genres: list[str], calendar: float = 0.0, season: float = 0.0, novelty: float = 0.5):
    movie = Movie(
        id=mid,
        imdb_id=f"tt{mid:07d}",
        title=f"Movie {mid}",
        year=2024,
        runtime_min=105,
        genres=genres,
        directors=[f"Director {mid}"],
        imdb_rating=7.2,
        num_votes=5000,
        overview=f"Distinct premise for movie {mid}",
    )
    score = ScoreBreakdown(
        final=final,
        predicted_rating=predicted,
        confidence=confidence,
        calendar=calendar,
        season=season,
        novelty=novelty,
        trust_audit={"status": "trusted", "trust": trust, "red_flag": False},
    )
    return Recommendation(movie, score)


def test_v17_top3_roles_only_reorder_close_safe_finalists():
    engine = object.__new__(FastRecommendationEngineV17)
    engine._role_stats = {"enabled": False, "roles": []}
    anchor = _rec(1, final=.90, predicted=8.8, confidence=.85, trust=.86, genres=["Thriller"])
    similar = _rec(2, final=.885, predicted=8.5, confidence=.80, trust=.82, genres=["Thriller"])
    contextual = _rec(3, final=.87, predicted=8.2, confidence=.76, trust=.80, genres=["Drama", "History"], calendar=.82)
    exploratory = _rec(4, final=.86, predicted=8.1, confidence=.72, trust=.78, genres=["Documentary"], novelty=.9)
    weak = _rec(5, final=.70, predicted=7.0, confidence=.70, trust=.75, genres=["Comedy"])

    selected = engine._role_select([anchor, similar, contextual, exploratory, weak], 3)

    assert selected[0] is anchor
    assert contextual in selected
    assert weak not in selected
    assert [rec.score.trust_audit.get("top3_role") for rec in selected] == ["anchor", "context", "explore"]


def test_rank_quality_tracks_likes_loves_and_dislikes():
    holdout = [
        HoldoutRating(1, "tt1", 10, "2026-09-01"),
        HoldoutRating(2, "tt2", 8, "2026-09-02"),
        HoldoutRating(3, "tt3", 3, "2026-09-03"),
    ]
    metrics = ranking_quality_metrics(["tt1", "tt3", "tt2"], holdout, cutoffs=(1, 3))
    assert metrics["hit_9_plus_at_1"] is True
    assert metrics["recall_8_plus_at_3"] == 1.0
    assert metrics["recall_9_plus_at_3"] == 1.0
    assert metrics["dislike_recall_at_3"] == 1.0
    assert metrics["ndcg_at_1"] == 1.0


def _report(*, c8: float, c9: float, ndcg: float, r8: float, r9: float, mrr: float, bad: float):
    return {
        "engine": "test",
        "candidate_recall": {"recall_8_plus_at_500": c8, "recall_9_plus_at_500": c9},
        "final_quality": {
            "ndcg_at_25": ndcg,
            "recall_8_plus_at_25": r8,
            "recall_9_plus_at_25": r9,
            "mrr_8_plus": mrr,
            "dislike_recall_at_25": bad,
            "dislike_recall_at_100": bad,
        },
    }


def test_quality_verdict_promotes_only_measured_gain_without_guardrail_regression():
    baseline = _report(c8=.50, c9=.40, ndcg=.20, r8=.15, r9=.10, mrr=.10, bad=.10)
    better = _report(c8=.56, c9=.46, ndcg=.28, r8=.22, r9=.16, mrr=.18, bad=.08)
    worse_recall = _report(c8=.40, c9=.30, ndcg=.50, r8=.30, r9=.25, mrr=.30, bad=.08)

    assert quality_verdict(baseline, better)["approved"] is True
    rejected = quality_verdict(baseline, worse_recall)
    assert rejected["approved"] is False
    assert rejected["guardrails"]["candidate_8_plus_no_material_regression"] is False
