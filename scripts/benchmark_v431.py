from __future__ import annotations

from pathlib import Path
import tempfile
import time

from cinecalendar.db import Database
from cinecalendar.learning_insight_v43 import text_signature_cache_info, text_signature_from_values
from cinecalendar.recommendation_history_v43 import recommendation_history_rows
from cinecalendar.recommendation_outcomes_v42 import recommendation_performance
from cinecalendar.util import identity_key, normalize_text, utcnow_iso


EXPOSURES = 8_000
MOVIES = 240
ENGINE = "learning-insight-v4.3.1"


def seed(db: Database) -> None:
    now = utcnow_iso()
    with db.tx() as con:
        movie_ids = []
        for idx in range(MOVIES):
            title = f"4.3.1 Benchmark Movie {idx}"
            cur = con.execute(
                """INSERT INTO movies(
                       imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                       year,title_type,runtime_min,genres_json,directors_json,countries_json,
                       overview,keywords_json,semantic_json,imdb_rating,num_votes,source,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,'["Drama"]','[]','[]',?,'[]','{}',?,?,?, ?,?)""",
                (
                    f"tt{7700000+idx:07d}",
                    identity_key(title, title, 2026, "movie"),
                    title,
                    title,
                    normalize_text(title),
                    normalize_text(title),
                    2026,
                    "movie",
                    100,
                    "A benchmark synopsis for recommendation history performance.",
                    7.0,
                    1000 + idx,
                    "benchmark",
                    now,
                    now,
                ),
            )
            movie_ids.append(int(cur.lastrowid))

        run_id = con.execute(
            """INSERT INTO recommendation_runs(
                   context_date,slot,generated_at,candidate_count,result_count,engine_version
               ) VALUES('2026-09-01','decision',?,100,3,?)""",
            (now, ENGINE),
        ).lastrowid

        for idx in range(EXPOSURES):
            movie_id = movie_ids[idx % len(movie_ids)]
            day = f"2026-09-{1 + (idx % 20):02d}"
            root = con.execute(
                """INSERT INTO recommendation_history(
                       movie_id,recommended_at,context_date,slot,final_score,ignored,action,
                       predicted_rating,confidence
                   ) VALUES(?,?,?,?,?,0,NULL,?,?)""",
                (movie_id, day + "T18:00:00+00:00", day, "decision", .75, 7.4, .68),
            )
            history_id = int(root.lastrowid)
            con.execute(
                """INSERT INTO recommendation_trust_audit(
                       history_id,run_id,movie_id,context_date,slot,rank_position,engine_version,
                       trust_status,trust_score,gate_score,support_count,support_labels,red_flag,created_at
                   ) VALUES(?,?,?,?,?,?,?,'trusted',.8,.8,2,'["benchmark"]',0,?)""",
                (history_id, int(run_id), movie_id, day, "decision", 1 + idx % 3, ENGINE, now),
            )

            mode = idx % 4
            if mode == 0:
                con.execute(
                    """INSERT INTO recommendation_outcomes(
                           exposure_history_id,movie_id,rank_position,context_date,slot,
                           chosen_at,playback_at,watched_at,actual_rating,rating_date,
                           predicted_rating,confidence,final_score,engine_version,absolute_error,
                           resolved_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        history_id, movie_id, 1 + idx % 3, day, "decision",
                        day + "T19:00:00+00:00", day + "T19:05:00+00:00",
                        day + "T21:00:00+00:00", 8, day,
                        7.4, .68, .75, ENGINE, .6,
                        day + "T21:05:00+00:00", now,
                    ),
                )
            elif mode == 1:
                con.execute(
                    """INSERT INTO recommendation_history(
                           movie_id,recommended_at,context_date,slot,exposure_history_id,action
                       ) VALUES(?,?,?,?,?,'skip_today')""",
                    (movie_id, day + "T18:10:00+00:00", day, "decision", history_id),
                )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cinecalendar-v431-bench-", ignore_cleanup_errors=True) as td:
        db = Database(Path(td) / "benchmark-v431.db")

        started = time.perf_counter()
        seed(db)
        seed_seconds = time.perf_counter() - started

        started = time.perf_counter()
        rated = recommendation_history_rows(db, status="rated", engine_version=ENGINE, limit=3000)
        history_seconds = time.perf_counter() - started
        if len(rated) != EXPOSURES // 4:
            raise SystemExit(f"benchmark 4.3.1: rated history mismatch: {len(rated)}")

        started = time.perf_counter()
        dashboard = recommendation_performance(
            db,
            since_date="2026-09-01",
            engine_version=ENGINE,
            reconcile=False,
            recent_limit=12,
        )
        dashboard_seconds = time.perf_counter() - started
        if dashboard.decision_exposures != EXPOSURES:
            raise SystemExit(
                f"benchmark 4.3.1: exposure mismatch: {dashboard.decision_exposures} != {EXPOSURES}"
            )
        if dashboard.rated_outcomes != EXPOSURES // 4:
            raise SystemExit(
                f"benchmark 4.3.1: outcome mismatch: {dashboard.rated_outcomes}"
            )

        overview = "monastery pilgrimage archive contemplation benchmark cache payload"
        keywords = ["monastery", "pilgrimage archive"]
        text_signature_from_values(overview, keywords)
        cache_before = text_signature_cache_info()
        started = time.perf_counter()
        for _ in range(5_000):
            text_signature_from_values(overview, keywords)
        cache_seconds = time.perf_counter() - started
        cache_after = text_signature_cache_info()
        if cache_after["hits"] < cache_before["hits"] + 5_000:
            raise SystemExit("benchmark 4.3.1: semantic signature cache did not hit")

        print(f"v431_seed_seconds={seed_seconds:.3f}")
        print(f"v431_history_8000_seconds={history_seconds:.4f}")
        print(f"v431_dashboard_8000_seconds={dashboard_seconds:.4f}")
        print(f"v431_signature_cache_5000_seconds={cache_seconds:.4f}")
        print(f"v431_signature_cache_size={cache_after['currsize']}")

        # Deliberately conservative CI gates. They catch accidental O(n^2) regressions without
        # turning runner variance into false failures.
        if history_seconds > 2.0:
            raise SystemExit(
                f"benchmark 4.3.1: filtered history too slow ({history_seconds:.3f}s > 2.0s)"
            )
        if dashboard_seconds > 1.5:
            raise SystemExit(
                f"benchmark 4.3.1: dashboard too slow ({dashboard_seconds:.3f}s > 1.5s)"
            )
        if cache_seconds > 0.25:
            raise SystemExit(
                f"benchmark 4.3.1: signature cache too slow ({cache_seconds:.3f}s > 0.25s)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
