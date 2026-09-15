from __future__ import annotations

import threading

import numpy as np
from scipy.sparse import csr_matrix

from cinecalendar.personal_candidates import PersonalCandidateGenerator
from cinecalendar.recommender_v13 import FastRecommendationEngineV13


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params=()):
        return self

    def fetchall(self):
        return list(self.rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def connect(self):
        return _Rows(self.rows)


class _Model:
    def __init__(self):
        # Item 0 is the user's rated 10/10 anchor and must never be returned as unseen.
        # Item 1 is strongest for the global user factor; item 2 is the next favourite neighbour.
        self.item_factors = np.asarray(
            [
                [1.00, 0.00],
                [0.95, 0.05],
                [0.82, 0.18],
                [0.10, 0.95],
                [-0.90, 0.00],
            ],
            dtype=np.float32,
        )


class _Collaborative:
    def __init__(self):
        self._lock = threading.RLock()
        self._model = _Model()
        self._item_to_imdb = np.asarray([1000001, 1000002, 1000003, 1000004, 1000005])
        self._imdb_to_item = {f"tt{x:07d}": i for i, x in enumerate(self._item_to_imdb)}
        self._version = "test-model"
        self._history_token = (1, "now", 0, "")

    def is_ready(self):
        return True

    def _build_user_representation(self):
        user_items = csr_matrix(
            (
                np.asarray([4.0], dtype=np.float32),
                np.asarray([0], dtype=np.int32),
                np.asarray([0, 1], dtype=np.int32),
            ),
            shape=(1, 5),
        )
        return user_items, np.asarray([1.0, 0.0], dtype=np.float32), 100


def test_global_personal_retrieval_considers_all_items_and_excludes_seen_anchor():
    db = _DB([{"imdb_id": "tt1000001", "rating": 10}])
    provider = _Collaborative()
    generator = PersonalCandidateGenerator(db, provider)

    groups = generator.groups(als_limit=1, favorite_limit=2)

    assert groups["als"] == ["tt1000002"]
    assert "tt1000001" not in groups["als"]
    assert "tt1000001" not in groups["favorites"]
    assert "tt1000002" not in groups["favorites"]
    assert groups["favorites"][0] == "tt1000003"


def test_candidate_mix_reserves_most_of_pool_for_real_personal_retrieval():
    merged, stats = FastRecommendationEngineV13._merge_candidate_groups(
        als_ids=list(range(1, 20)),
        favorite_ids=list(range(101, 120)),
        generic_ids=list(range(201, 240)),
        limit=10,
    )

    assert len(merged) == 10
    assert stats == {"als": 5, "favorites": 2, "generic": 3, "total": 10}
    assert merged[:5] == [1, 2, 3, 4, 5]
    assert merged[5:7] == [101, 102]
    assert merged[7:] == [201, 202, 203]


def test_candidate_mix_backfills_missing_movielens_mapping_without_shrinking_pool():
    merged, stats = FastRecommendationEngineV13._merge_candidate_groups(
        als_ids=[1],
        favorite_ids=[],
        generic_ids=list(range(50, 80)),
        limit=10,
    )

    assert len(merged) == 10
    assert merged[0] == 1
    assert stats["als"] == 1
    assert stats["generic"] == 9
    assert stats["total"] == 10


def test_duplicate_candidates_cannot_consume_multiple_quota_slots():
    merged, stats = FastRecommendationEngineV13._merge_candidate_groups(
        als_ids=[1, 2, 3, 4, 5],
        favorite_ids=[3, 4, 6, 7],
        generic_ids=[1, 7, 8, 9, 10, 11],
        limit=8,
    )

    assert len(merged) == len(set(merged)) == 8
    assert stats["total"] == 8
