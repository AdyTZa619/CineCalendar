from __future__ import annotations

from datetime import date, timedelta
import inspect

from cinecalendar.adaptive_preferences import _Sample
from cinecalendar.adaptive_preferences_v2 import (
    AdaptivePreferenceLearnerV2,
    EPOCH_CANDIDATES,
    HASH_DIM,
)
from cinecalendar.db import Database
from cinecalendar.models import Movie
from cinecalendar.production_engine import build_production_recommender
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.service import CineCalendarService


def _sample(index: int, liked: bool) -> _Sample:
    rating = 9 if liked else 2
    movie = Movie(
        id=index + 1,
        imdb_id=f"tt{9_500_000 + index:07d}",
        title=f"{'Liked' if liked else 'Disliked'} {index}",
        year=2010 + index % 15,
        title_type="movie",
        runtime_min=102,
        genres=["Horror" if liked else "Romance"],
        directors=["Director Positive" if liked else "Director Negative"],
        countries=["United States"],
        imdb_rating=7.0,
        num_votes=25_000,
    )
    return _Sample(
        movie=movie,
        target=AdaptivePreferenceLearnerV2._rating_target(rating),
        weight=1.0,
        date_key=(date(2025, 1, 1) + timedelta(days=index)).isoformat(),
    )


def test_v2_temporal_gate_measures_ranking_and_selects_epochs(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    learner = AdaptivePreferenceLearnerV2(db)
    # Interleave strong positive/negative preferences so the recent holdout contains both classes.
    ratings = [_sample(i, liked=(i % 2 == 0)) for i in range(180)]

    result = learner._validate(ratings)

    assert result["available"] is True
    assert result["selected_epochs"] in EPOCH_CANDIDATES
    assert result["baseline_kind"] == "calibrated_imdb"
    assert result["model_mae"] is not None and result["baseline_mae"] is not None
    assert result["model_ndcg10"] is not None and result["baseline_ndcg10"] is not None
    assert result["model_pairwise"] is not None and result["baseline_pairwise"] is not None
    assert result["model_mae"] < result["baseline_mae"]
    assert result["model_ndcg10"] > result["baseline_ndcg10"]
    assert result["model_pairwise"] > result["baseline_pairwise"]
    assert result["validated"] is True


def test_v2_uses_large_hash_space_and_safe_metric_fallbacks(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    learner = AdaptivePreferenceLearnerV2(db)

    assert HASH_DIM == 65_536
    assert len(learner._weights) == HASH_DIM
    assert learner._ndcg_at_10([2.0, 3.0], [9.0, 8.0]) is None
    assert learner._pairwise_accuracy([6.0, 6.0, 7.0], [1.0, 2.0, 3.0]) is None


def test_production_stack_replaces_legacy_adaptive_model_with_v2(tmp_path):
    service_source = inspect.getsource(CineCalendarService.__init__)
    builder_source = inspect.getsource(build_production_recommender)

    assert "build_production_recommender" in service_source
    assert "AdaptivePreferenceLearnerV2" in builder_source
    assert "engine.adaptive = AdaptivePreferenceLearnerV2(db)" in builder_source

    db = Database(tmp_path / "production-adaptive.db")
    engine = build_production_recommender(db, FastRecommendationEngineV16)
    assert isinstance(engine.adaptive, AdaptivePreferenceLearnerV2)

    # Training remains staggered; the production service must not synchronously force adaptive warmup.
    assert "start_adaptive_background()" not in service_source
