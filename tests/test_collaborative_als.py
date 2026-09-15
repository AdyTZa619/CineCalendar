from __future__ import annotations

import inspect

import numpy as np
from implicit.cpu.als import AlternatingLeastSquares

from cinecalendar.collaborative_als import CollaborativeALSProvider, _rating_confidence
from cinecalendar.db import Database
from cinecalendar.recommender_v11 import ALS_WEIGHT, FastRecommendationEngineV11
from cinecalendar.util import identity_key, utcnow_iso


def test_rating_confidence_has_explicit_likes_dislikes_and_neutral_six():
    assert _rating_confidence(10) > _rating_confidence(8) > _rating_confidence(7) > 0
    assert _rating_confidence(6) == 0
    assert _rating_confidence(5) < 0
    assert _rating_confidence(1) < _rating_confidence(5)


def test_established_als_is_primary_not_the_old_hand_written_ranker():
    assert ALS_WEIGHT >= 0.75
    source = inspect.getsource(FastRecommendationEngineV11.recommend)
    assert "collaborative.score_candidates" in source
    assert "ALS_WEIGHT * als_score" in source
    assert "_score_one" in source  # retained only as secondary/fallback signal
    token_source = inspect.getsource(FastRecommendationEngineV11._state_token)
    assert "collaborative_token" in token_source


def test_local_fold_in_scores_candidates_without_uploading_private_ratings(tmp_path):
    db = Database(tmp_path / "CineCalendarData" / "data" / "cinecalendar.db")
    now = utcnow_iso()
    with db.tx() as con:
        for i in range(1, 31):
            imdb_id = f"tt{i:07d}"
            con.execute(
                """INSERT INTO movies(
                    imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                    genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                    imdb_rating,num_votes,release_date,poster_url,source,created_at,updated_at,
                    title_norm,original_title_norm
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    imdb_id, identity_key(f"Movie {i}", f"Movie {i}", 2000 + i % 20, "movie"),
                    f"Movie {i}", f"Movie {i}", 2000 + i % 20, "movie", 100,
                    "[]", "[]", "[]", "", "[]", "{}", 7.0, 10000, None, None,
                    "test", now, now, f"movie {i}", f"movie {i}",
                ),
            )
            if i <= 24:
                movie_id = con.execute("SELECT id FROM movies WHERE imdb_id=?", (imdb_id,)).fetchone()[0]
                rating = 10 if i % 3 == 0 else 8 if i % 3 == 1 else 3
                con.execute(
                    "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (movie_id, rating, "2026-01-01", "test", now, now),
                )

    provider = CollaborativeALSProvider(db)
    rng = np.random.default_rng(42)
    factors = rng.normal(0, 0.25, size=(30, 8)).astype(np.float32)
    model = AlternatingLeastSquares(
        factors=8, regularization=0.1, alpha=1.0, dtype=np.float32,
        iterations=0, num_threads=1, random_state=42,
    )
    model.item_factors = factors
    model.user_factors = np.zeros((1, 8), dtype=np.float32)
    provider._model = model
    provider._imdb_to_item = {f"tt{i:07d}": i - 1 for i in range(1, 31)}
    provider._item_to_imdb = np.arange(1, 31, dtype=np.int64)
    provider._manifest = {"dataset": "MovieLens 32M", "training_users": 200948, "training_items": 87585}
    provider._version = "test-als"
    provider._state = "ready"

    candidates = [f"tt{i:07d}" for i in range(25, 31)]
    normalized, raw, mapped = provider.score_candidates(candidates)

    assert mapped == 24
    assert set(normalized) == set(candidates)
    assert set(raw) == set(candidates)
    assert all(0.0 < value < 1.0 for value in normalized.values())
    assert len(set(round(value, 6) for value in normalized.values())) == len(candidates)
