from __future__ import annotations

from cinecalendar.db import Database
from cinecalendar.full_catalog_evaluation_v414 import (
    bootstrap_mean_interval,
    fixed_budget_mix,
    live_shadow_metrics,
)
from cinecalendar.full_catalog_shadow_v414 import (
    FULL_CATALOG_RETRIEVAL_VERSION,
    FullCatalogCandidateGeneratorV414,
)
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


class _Collaborative:
    def is_ready(self):
        return True

    def state_token(self):
        return ("ready", "test-model")

    def has_mapping(self, imdb_id):
        return False


def _movie(db: Database, idx: int, title: str, *, director="Director Bun", genres=("Crime",), year=2022):
    now = utcnow_iso()
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO movies(
                   imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,countries_json,
                   overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"tt{9900000+idx:07d}", identity_key(title,title,year,"movie"), title, title,
                normalize_text(title), normalize_text(title), year, "movie", 105,
                json_dumps(list(genres)), json_dumps([director]), json_dumps(["Romania"]),
                "A dark crime investigation.", json_dumps(["investigation"]),
                json_dumps({"crime": 1.0}), 7.4, 15000, "test", now, now,
            ),
        )
        return int(cur.lastrowid)


def _rating(db: Database, movie_id: int, rating: int, day: str):
    now = day + "T20:00:00+00:00"
    with db.tx() as con:
        con.execute(
            """INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (movie_id, rating, day, "test", now, now),
        )


def _shadow_run(db: Database, *, version: str, day: str, baseline: int, challenger: int, suffix: str):
    with db.tx() as con:
        cur = con.execute(
            """INSERT INTO retrieval_shadow_runs(
                   run_key,context_date,slot,generated_at,baseline_engine,challenger_version,
                   depth,baseline_count,challenger_count,overlap_count,duration_ms,status,error
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'complete','')""",
            (f"run-{suffix}", day, "browse", utcnow_iso(), "baseline", version, 1, 1, 1, 0, 5),
        )
        run_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO retrieval_shadow_items(run_id,source,movie_id,rank_position,retrieval_score) VALUES(?,'baseline',?,1,NULL)",
            (run_id, baseline),
        )
        con.execute(
            "INSERT INTO retrieval_shadow_items(run_id,source,movie_id,rank_position,retrieval_score) VALUES(?,'challenger',?,1,.9)",
            (run_id, challenger),
        )
    return run_id


def test_live_shadow_metrics_are_version_scoped_and_paired(tmp_path):
    db = Database(tmp_path / "shadow.db")
    base = _movie(db, 1, "Baseline")
    challenger = _movie(db, 2, "Challenger")
    old_base = _movie(db, 3, "Old baseline")
    old_challenger = _movie(db, 4, "Old challenger")
    _shadow_run(
        db, version=FULL_CATALOG_RETRIEVAL_VERSION, day="2026-09-01",
        baseline=base, challenger=challenger, suffix="current",
    )
    _shadow_run(
        db, version="full-catalog-retrieval-v4.13.0", day="2026-09-01",
        baseline=old_base, challenger=old_challenger, suffix="old",
    )
    _rating(db, base, 5, "2026-09-10")
    _rating(db, challenger, 9, "2026-09-11")
    _rating(db, old_base, 10, "2026-09-12")
    _rating(db, old_challenger, 1, "2026-09-12")

    report = live_shadow_metrics(db, challenger_version=FULL_CATALOG_RETRIEVAL_VERSION)

    assert report["baseline"]["rated"] == 1
    assert report["challenger"]["rated"] == 1
    assert report["paired"]["runs"] == 1
    assert report["paired"]["rating_delta_interval"]["mean"] == 4.0
    assert report["promotion_allowed_from_live_shadow"] is False
    assert report["limitation"] == "hidden_challenger_has_unequal_exposure"


def test_live_shadow_window_rejects_late_outcome(tmp_path):
    db = Database(tmp_path / "late.db")
    base = _movie(db, 10, "Baseline")
    challenger = _movie(db, 11, "Challenger")
    _shadow_run(
        db, version=FULL_CATALOG_RETRIEVAL_VERSION, day="2026-01-01",
        baseline=base, challenger=challenger, suffix="late",
    )
    _rating(db, base, 8, "2026-01-10")
    _rating(db, challenger, 10, "2026-06-01")

    report = live_shadow_metrics(
        db, window_days=120, challenger_version=FULL_CATALOG_RETRIEVAL_VERSION
    )

    assert report["baseline"]["rated"] == 1
    assert report["challenger"]["rated"] == 0
    assert report["paired"]["runs"] == 0


def test_fixed_budget_mix_keeps_budget_and_injects_unique_challenger():
    mixed = fixed_budget_mix(
        list(range(1, 101)),
        [90, 91, *range(101, 151)],
        limit=100,
        share=.15,
    )

    assert len(mixed) == 100
    assert len(set(mixed)) == 100
    assert any(value >= 101 for value in mixed[:20])
    assert sum(value >= 101 for value in mixed) >= 13


def test_bootstrap_interval_is_deterministic_and_contains_mean():
    a = bootstrap_mean_interval([1.0, 2.0, 3.0, 4.0], seed=7, samples=400)
    b = bootstrap_mean_interval([1.0, 2.0, 3.0, 4.0], seed=7, samples=400)

    assert a == b
    assert a["low"] <= a["mean"] <= a["high"]
    assert a["mean"] == 2.5


def test_v414_combo_signal_can_seed_direct_candidate_query(tmp_path):
    db = Database(tmp_path / "combo.db")
    wanted = _movie(db, 20, "Wanted", director="Director Bun", genres=("Crime", "Thriller"), year=2022)
    _movie(db, 21, "Wrong director", director="Alt Director", genres=("Crime",), year=2022)
    _movie(db, 22, "Wrong decade", director="Director Bun", genres=("Crime",), year=1992)
    generator = FullCatalogCandidateGeneratorV414(db, _Collaborative())

    with db.connect() as con:
        director_genre = generator._query_signal(con, "combo:director:director bun|genre:crime")
        genre_decade = generator._query_signal(con, "combo:genre:crime|decade:2020s")

    assert wanted in director_genre
    assert wanted in genre_decade
