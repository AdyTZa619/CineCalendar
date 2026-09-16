from __future__ import annotations

from datetime import date, timedelta
import threading

import numpy as np
from scipy.sparse import csr_matrix

from cinecalendar.db import Database
from cinecalendar.personal_candidates import PersonalCandidateGenerator, RETRIEVAL_VERSION
from cinecalendar.recommendation_backtest import HoldoutRating, candidate_recall_metrics, temporal_holdout
from cinecalendar.util import utcnow_iso


class _Rows:
    def __init__(self, positive_rows, negative_rows):
        self.positive_rows = positive_rows
        self.negative_rows = negative_rows
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        text = str(sql)
        if "r.rating>=9" in text:
            self.current = list(self.positive_rows)
        elif "r.rating<=4" in text:
            self.current = list(self.negative_rows)
        else:
            self.current = []
        return self

    def fetchall(self):
        return list(self.current)


class _DB:
    def __init__(self, positive_rows, negative_rows):
        self.positive_rows = positive_rows
        self.negative_rows = negative_rows

    def connect(self):
        return _Rows(self.positive_rows, self.negative_rows)


class _Model:
    def __init__(self):
        # 0/1 are positive anchors; 2 is a strong negative anchor.
        # 3 is a clean positive neighbour. 4 is also positive-ish but very close to the negative.
        self.item_factors = np.asarray(
            [
                [1.00, 0.00],
                [0.96, 0.12],
                [0.45, 0.89],
                [0.99, 0.03],
                [0.72, 0.69],
                [-0.90, 0.05],
            ],
            dtype=np.float32,
        )


class _Collaborative:
    def __init__(self):
        self._lock = threading.RLock()
        self._model = _Model()
        self._item_to_imdb = np.asarray([2000001, 2000002, 2000003, 2000004, 2000005, 2000006])
        self._imdb_to_item = {f"tt{x:07d}": i for i, x in enumerate(self._item_to_imdb)}
        self._version = "test-v2"
        self._history_token = (3, "now", 0, "")

    def is_ready(self):
        return True

    def _build_user_representation(self):
        user_items = csr_matrix(
            (
                np.asarray([4.0, 3.0, -3.25], dtype=np.float32),
                np.asarray([0, 1, 2], dtype=np.int32),
                np.asarray([0, 3], dtype=np.int32),
            ),
            shape=(1, 6),
        )
        return user_items, np.asarray([1.0, 0.0], dtype=np.float32), 120


def test_contrastive_favorite_retrieval_uses_positive_and_negative_anchors():
    db = _DB(
        positive_rows=[
            {"imdb_id": "tt2000001", "rating": 10},
            {"imdb_id": "tt2000002", "rating": 9},
        ],
        negative_rows=[{"imdb_id": "tt2000003", "rating": 1}],
    )
    generator = PersonalCandidateGenerator(db, _Collaborative())

    groups = generator.groups(als_limit=0, favorite_limit=3)
    status = generator.status()

    assert groups["favorites"][0] == "tt2000004"
    assert groups["favorites"].index("tt2000004") < groups["favorites"].index("tt2000005")
    assert "tt2000001" not in groups["favorites"]
    assert "tt2000002" not in groups["favorites"]
    assert "tt2000003" not in groups["favorites"]
    assert status["retrieval_version"] == RETRIEVAL_VERSION
    assert status["positive_anchors"] == 2
    assert status["negative_anchors"] == 1


def test_negative_anchor_conservatively_demotes_risky_candidate():
    factors = _Model().item_factors
    positive = [(0, 10), (1, 9)]
    no_veto = PersonalCandidateGenerator._contrastive_favorite_scores(factors, positive, [])
    with_veto = PersonalCandidateGenerator._contrastive_favorite_scores(factors, positive, [(2, 1)])

    assert with_veto[3] >= no_veto[3] - 1e-6
    assert with_veto[4] < no_veto[4]
    assert with_veto[3] > with_veto[4]


def test_candidate_recall_metrics_separates_good_recall_from_bad_contamination():
    holdout = [
        HoldoutRating(1, "tt1", 10, "2026-01-01"),
        HoldoutRating(2, "tt2", 9, "2026-01-02"),
        HoldoutRating(3, "tt3", 8, "2026-01-03"),
        HoldoutRating(4, "tt4", 3, "2026-01-04"),
        HoldoutRating(5, "tt5", 6, "2026-01-05"),
    ]
    metrics = candidate_recall_metrics(["tt2", "tt4", "tt3", "tt9", "tt1"], holdout, cutoffs=(2, 5))

    assert metrics["recall_9_plus_at_2"] == 0.5
    assert metrics["recall_9_plus_at_5"] == 1.0
    assert metrics["recall_8_plus_at_5"] == 1.0
    assert metrics["dislike_recall_at_2"] == 1.0
    assert metrics["matched_mean_rating"] == 7.5


def test_temporal_holdout_is_newest_and_does_not_mutate_database(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    now = utcnow_iso()
    start = date(2025, 1, 1)
    with db.tx() as con:
        for i in range(100):
            imdb_id = f"tt{3000000 + i:07d}"
            title = f"Film {i}"
            cur = con.execute(
                """INSERT INTO movies(
                       imdb_id,identity_key,title,original_title,year,title_type,runtime_min,
                       genres_json,directors_json,countries_json,overview,keywords_json,semantic_json,
                       imdb_rating,num_votes,source,created_at,updated_at,title_norm,original_title_norm
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    imdb_id, f"film-{i}", title, title, 2020, "movie", 100,
                    "[]", "[]", "[]", "", "[]", "{}", 7.0, 1000,
                    "test", now, now, title.lower(), title.lower(),
                ),
            )
            mid = int(cur.lastrowid)
            day = (start + timedelta(days=i)).isoformat()
            con.execute(
                "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
                (mid, 8 if i % 3 else 9, day, "test", now, now),
            )

    holdout = temporal_holdout(db, fraction=0.20, min_holdout=10, max_holdout=30)
    with db.connect() as con:
        remaining = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])

    assert len(holdout) == 20
    assert holdout[0].date_rated == (start + timedelta(days=80)).isoformat()
    assert holdout[-1].date_rated == (start + timedelta(days=99)).isoformat()
    assert remaining == 100
