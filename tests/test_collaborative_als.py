from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np
import pytest
from implicit.cpu.als import AlternatingLeastSquares

from cinecalendar.collaborative_als import CollaborativeALSProvider, MODEL_MANIFEST_URL, _rating_confidence
from cinecalendar.db import Database
from cinecalendar.recommender_v11 import ALS_WEIGHT, CONTENT_WEIGHT, FastRecommendationEngineV11
from cinecalendar.util import identity_key, utcnow_iso


def test_rating_confidence_matches_explicit_1_to_10_semantics():
    assert _rating_confidence(10) > _rating_confidence(9) > _rating_confidence(8) > _rating_confidence(7) > 0
    assert 0 < _rating_confidence(6) < _rating_confidence(7)
    assert _rating_confidence(5) < 0
    assert abs(_rating_confidence(5)) < abs(_rating_confidence(4))
    assert _rating_confidence(1) < _rating_confidence(4)


def test_model_manifest_is_from_standalone_cinecalendar_repo():
    assert "AdyTZa619/CineCalendar/main/collaborative-model.json" in MODEL_MANIFEST_URL
    assert "DuplicateDownloadGuard-Releases" not in MODEL_MANIFEST_URL


def test_established_als_remains_primary_with_independent_content_check():
    assert ALS_WEIGHT >= 0.65
    source = inspect.getsource(FastRecommendationEngineV11.recommend)
    assert "collaborative.score_candidates" in source
    assert "self._hybrid_blend(als_score, old_final)" in source
    baseline = object.__new__(FastRecommendationEngineV11)
    blended, als_weight, content_weight = baseline._hybrid_blend(.9, .5)
    assert als_weight == ALS_WEIGHT
    assert content_weight == pytest.approx(CONTENT_WEIGHT)
    assert blended == pytest.approx(ALS_WEIGHT * .9 + CONTENT_WEIGHT * .5)
    assert "_score_one" in source
    assert "_mapped_candidate_is_trustworthy" in source
    assert "_catalog_quality_is_trustworthy" in source
    token_source = inspect.getsource(FastRecommendationEngineV11._state_token)
    assert "collaborative_token" in token_source


def test_global_percentile_calibration_does_not_make_small_bad_pool_look_great():
    reference = np.arange(-10.0, 11.0, 1.0)
    values = np.asarray([-10.0, -9.0])
    scores = CollaborativeALSProvider._global_percentiles(values, reference)
    assert scores[0] < 0.10
    assert scores[1] < 0.15
    assert scores[1] > scores[0]


def test_low_vote_raw_rating_is_bayesian_checked():
    thin = SimpleNamespace(num_votes=50, imdb_rating=10.0)
    mature = SimpleNamespace(num_votes=500, imdb_rating=6.0)
    convincing = SimpleNamespace(num_votes=200, imdb_rating=8.0)
    assert not FastRecommendationEngineV11._catalog_quality_is_trustworthy(thin)
    assert FastRecommendationEngineV11._catalog_quality_is_trustworthy(mature)
    assert FastRecommendationEngineV11._catalog_quality_is_trustworthy(convincing)


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
    factors = rng.normal(0, 0.25, size=(1200, 8)).astype(np.float32)
    model = AlternatingLeastSquares(
        factors=8, regularization=0.1, alpha=1.0, dtype=np.float32,
        iterations=0, num_threads=1, random_state=42,
    )
    model.item_factors = factors
    model.user_factors = np.zeros((1, 8), dtype=np.float32)
    provider._model = model
    provider._imdb_to_item = {f"tt{i:07d}": i - 1 for i in range(1, 1201)}
    provider._item_to_imdb = np.arange(1, 1201, dtype=np.int64)
    provider._manifest = {"dataset": "MovieLens 32M", "training_users": 200948, "training_items": 87585}
    provider._version = "test-als"
    provider._state = "ready"

    candidates = [f"tt{i:07d}" for i in range(25, 31)]
    normalized, raw, mapped = provider.score_candidates(candidates)

    assert mapped == 24
    assert set(normalized) == set(candidates)
    assert set(raw) == set(candidates)
    assert all(0.0 <= value <= 1.0 for value in normalized.values())
    assert len(set(round(value, 6) for value in normalized.values())) == len(candidates)
