from __future__ import annotations

from datetime import date, timedelta

from cinecalendar.accuracy_engine_v37 import (
    LocalContentAccuracyMixinV37,
    allowed_local_shares,
    calibrated_accuracy_engine_class,
)
from cinecalendar.context_recommender_v35 import (
    FastRecommendationEngineV16Context35,
    FastRecommendationEngineV17Context35,
    _ContextGuardMixin,
    contextual_engine_class,
)
from cinecalendar.db import Database
from cinecalendar.imdb_import import add_manual_rating
from cinecalendar.recommender_v16 import FastRecommendationEngineV16
from cinecalendar.recommender_v17 import FastRecommendationEngineV17
from cinecalendar.recommender_v18 import FastRecommendationEngineV18
from cinecalendar.rolling_backtest_v37 import (
    _remove_future,
    rolling_windows,
    strict_rolling_verdict,
)
from cinecalendar.recommendation_backtest import _sqlite_backup


def _seed_history(db: Database, count: int = 210) -> None:
    start = date(2024, 1, 1)
    for idx in range(count):
        rating = 1 + (idx % 10)
        mid = add_manual_rating(
            db,
            f"History {idx:03d}",
            1980 + (idx % 45),
            rating,
            imdb_id=f"tt{9000000 + idx:07d}",
            genres=["Drama" if idx % 2 else "Thriller"],
        )
        day = (start + timedelta(days=idx)).isoformat()
        with db.tx() as con:
            con.execute("UPDATE ratings SET date_rated=?,updated_at=? WHERE movie_id=?", (day, day + "T12:00:00Z", mid))


def _fold(delta=0.02, *, c8=0.01, c9=0.01, ndcg=0.01, bad=0.0):
    return {
        "verdict": {
            "composite_delta": delta,
            "candidate_8_plus_delta": c8,
            "candidate_9_plus_delta": c9,
            "ndcg25_delta": ndcg,
            "final_dislike_delta": bad,
        }
    }


def test_v37_challenger_wraps_exact_approved_baseline():
    v16 = calibrated_accuracy_engine_class(FastRecommendationEngineV16, 0.08)
    v17 = calibrated_accuracy_engine_class(FastRecommendationEngineV17, 0.20)

    assert v16.__mro__[0] is v16
    assert v16.__mro__[1] is LocalContentAccuracyMixinV37
    assert v16.__mro__[2] is FastRecommendationEngineV16
    assert FastRecommendationEngineV17 not in v16.__mro__

    assert v17.__mro__[1] is LocalContentAccuracyMixinV37
    assert v17.__mro__[2] is FastRecommendationEngineV17
    assert v17.LOCAL_CONTENT_SHARE == 0.20
    assert allowed_local_shares() == (0.08, 0.14, 0.20)


def test_context_wrapper_preserves_exact_approved_engine():
    assert contextual_engine_class(FastRecommendationEngineV16) is FastRecommendationEngineV16Context35
    assert contextual_engine_class(FastRecommendationEngineV17) is FastRecommendationEngineV17Context35

    wrapped_v18 = contextual_engine_class(FastRecommendationEngineV18)
    assert wrapped_v18.__mro__[1] is _ContextGuardMixin
    assert FastRecommendationEngineV18 in wrapped_v18.__mro__
    assert contextual_engine_class(FastRecommendationEngineV18) is wrapped_v18

    v19 = calibrated_accuracy_engine_class(FastRecommendationEngineV16, 0.14)
    wrapped_v19 = contextual_engine_class(v19)
    assert wrapped_v19.__mro__[1] is _ContextGuardMixin
    assert v19 in wrapped_v19.__mro__
    assert FastRecommendationEngineV16 in wrapped_v19.__mro__
    assert FastRecommendationEngineV17 not in wrapped_v19.__mro__


def test_v37_candidate_share_is_bounded_and_baseline_dominant():
    baseline = list(range(1, 101))
    local = list(range(1001, 1101))
    for share in allowed_local_shares():
        merged, stats = LocalContentAccuracyMixinV37._merge_accuracy_candidates(
            baseline, local, 100, share
        )
        assert len(merged) == 100
        assert len(set(merged)) == 100
        assert stats["local_content"] <= round(100 * share)
        assert stats["baseline"] >= 100 - round(100 * share)


def test_rolling_windows_are_non_overlapping_and_remove_all_future_ratings(tmp_path):
    source = Database(tmp_path / "rolling.db")
    _seed_history(source, 210)
    windows = rolling_windows(source)

    assert len(windows) == 3
    seen = set()
    for window in windows:
        current = {row.movie_id for row in window.holdout}
        assert current
        assert seen.isdisjoint(current)
        seen.update(current)
    assert windows[0].training_count < windows[1].training_count < windows[2].training_count
    assert windows[0].cutoff_date < windows[1].cutoff_date < windows[2].cutoff_date

    clone_path = tmp_path / "clone.db"
    _sqlite_backup(source.path, clone_path)
    clone = Database(clone_path)
    _remove_future(clone, windows[0])
    with clone.connect() as con:
        remaining = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
    assert remaining == windows[0].training_count


def test_rolling_verdict_requires_stable_positive_majority():
    good = strict_rolling_verdict([_fold(0.018), _fold(0.012), _fold(0.009)])
    assert good["approved"] is True
    assert good["positive_folds"] == 3

    one_material_regression = strict_rolling_verdict([
        _fold(0.020),
        _fold(0.014),
        _fold(-0.006),
    ])
    assert one_material_regression["approved"] is False
    assert one_material_regression["guardrails"]["no_bad_fold"] is False


def test_rolling_verdict_rejects_dislike_or_high_rating_regression():
    bad_dislike = strict_rolling_verdict([
        _fold(0.02),
        _fold(0.02, bad=0.011),
        _fold(0.02),
    ])
    assert bad_dislike["approved"] is False

    bad_loved = strict_rolling_verdict([
        _fold(0.02),
        _fold(0.02, c9=-0.016),
        _fold(0.02),
    ])
    assert bad_loved["approved"] is False


def test_rolling_windows_refuse_weak_history(tmp_path):
    db = Database(tmp_path / "small.db")
    _seed_history(db, 120)
    assert rolling_windows(db) == []
