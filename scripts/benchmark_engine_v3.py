from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import time

from cinecalendar.calendar_engine_v2 import RichCalendarEngine
from cinecalendar.db import Database
from cinecalendar.profile import build_profile
from cinecalendar.recommender_v6 import FastRecommendationEngineV6
from cinecalendar.semantic import extract_semantic
from cinecalendar.models import Movie
from cinecalendar.util import identity_key, json_dumps, normalize_text, utcnow_iso


RATED = 2434
CANDIDATES = 260_000


def add_ratings(db: Database) -> None:
    genres = ["History", "Crime", "Thriller", "Sci-Fi", "Documentary", "Biography", "War", "Adventure"]
    now = utcnow_iso()
    with db.tx() as con:
        for i in range(1, RATED + 1):
            title = f"Rated Benchmark {i}"
            year = 1980 + (i % 46)
            genre = genres[i % len(genres)]
            director = f"Director {i % 80}"
            imdb_id = f"tt9{i:06d}"
            m = Movie(title=title, original_title=title, year=year, title_type="Movie", runtime_min=85 + i % 80,
                      genres=[genre], directors=[director], imdb_rating=6.0 + (i % 30) / 10.0,
                      num_votes=500 + i * 10, source="benchmark")
            m.semantic = extract_semantic(m)
            ident = identity_key(title, title, year, "Movie")
            cur = con.execute(
                """INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
                   year,title_type,runtime_min,genres_json,directors_json,semantic_json,imdb_rating,num_votes,
                   source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (imdb_id, ident, title, title, normalize_text(title), normalize_text(title), year, "Movie",
                 m.runtime_min, json_dumps(m.genres), json_dumps(m.directors), json_dumps(m.semantic),
                 m.imdb_rating, m.num_votes, "benchmark", now, now),
            )
            rating = 3 + (i * 7) % 8
            con.execute(
                "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
                (cur.lastrowid, rating, f"202{(i % 7)}-09-01", "benchmark", now, now),
            )
    build_profile(db)


def add_candidates(db: Database) -> None:
    genres = ["History", "Crime", "Thriller", "Sci-Fi", "Documentary", "Biography", "War", "Adventure", "Mystery", "Horror"]
    now = utcnow_iso()
    sql = """INSERT INTO movies(imdb_id,identity_key,title,original_title,title_norm,original_title_norm,
             year,title_type,runtime_min,genres_json,directors_json,semantic_json,imdb_rating,num_votes,
             source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    with db.tx() as con:
        batch = []
        for i in range(1, CANDIDATES + 1):
            # Sprinkle real calendar semantics through the synthetic catalog so the day
            # program must actually build direct/spiritual/history lanes, not just season.
            if i % 997 == 0:
                title = f"Holy Cross Chronicle {i}"
                genre = "History"
                semantic = {"cross_veneration": .92, "christianity": .86, "history": .72}
            elif i % 613 == 0:
                title = f"Faith Chronicle {i}"
                genre = "Biography"
                semantic = {"christianity": .82, "faith": .78, "history": .45}
            else:
                title = f"Candidate Benchmark {i}"
                genre = genres[i % len(genres)]
                semantic = {normalize_text(genre).replace(" ", "_"): 0.85}
            year = 1940 + (i % 87)
            director = f"Director {i % 160}"
            imdb_id = f"tt8{i:06d}"
            votes = 50 + ((i * 7919) % 1_500_000)
            rating = 5.5 + ((i * 37) % 35) / 10.0
            batch.append((
                imdb_id, identity_key(title, title, year, "Movie"), title, title,
                normalize_text(title), normalize_text(title), year, "Movie", 80 + i % 120,
                json_dumps([genre]), json_dumps([director]), json_dumps(semantic), rating, votes,
                "benchmark", now, now,
            ))
            if len(batch) >= 5000:
                con.executemany(sql, batch)
                batch.clear()
        if batch:
            con.executemany(sql, batch)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cinecalendar-v6-bench-", ignore_cleanup_errors=True) as td:
        db = Database(Path(td) / "benchmark.db")
        t0 = time.perf_counter()
        add_ratings(db)
        add_candidates(db)
        seed_seconds = time.perf_counter() - t0

        t1 = time.perf_counter()
        engine = FastRecommendationEngineV6(db, RichCalendarEngine())
        index_seconds = time.perf_counter() - t1

        t2 = time.perf_counter()
        primary, backups = engine.decision_pick(date(2026, 9, 14))
        first_seconds = time.perf_counter() - t2
        if primary is None or len(backups) < 2:
            raise SystemExit("benchmark: no recommendation result")
        normal_pre_rank = engine.last_pre_rank_count
        normal_full_score = engine.last_full_score_count
        if engine.last_candidate_count > FastRecommendationEngineV6.NORMAL_POOL:
            raise SystemExit(f"benchmark: pre-ranked too many candidates: {engine.last_candidate_count}")
        if normal_full_score > FastRecommendationEngineV6.FINALISTS_NORMAL:
            raise SystemExit(f"benchmark: fully scored too many finalists: {normal_full_score}")

        excluded = {int(primary.movie.id)}
        t3 = time.perf_counter()
        next_primary, _ = engine.decision_pick(date(2026, 9, 14), excluded)
        cached_seconds = time.perf_counter() - t3
        if next_primary is None or next_primary.movie.id == primary.movie.id:
            raise SystemExit("benchmark: cached Alt film failed")

        t4 = time.perf_counter()
        calendar_result = engine.calendar_day_program(date(2026, 9, 14), 6)
        calendar_seconds = time.perf_counter() - t4
        if not calendar_result.get("sections"):
            raise SystemExit("benchmark: calendar day returned no sections")
        if engine.last_calendar_pre_rank_count > FastRecommendationEngineV6.CALENDAR_POOL:
            raise SystemExit("benchmark: calendar pre-rank exceeded hard pool")
        if engine.last_calendar_full_score_count > FastRecommendationEngineV6.CALENDAR_FINALISTS:
            raise SystemExit("benchmark: calendar full-score set exceeded hard bound")

        t5 = time.perf_counter()
        cached_calendar = engine.calendar_day_program(date(2026, 9, 14), 6)
        calendar_cached_seconds = time.perf_counter() - t5
        if cached_calendar is not calendar_result:
            raise SystemExit("benchmark: calendar day cache did not reuse result")

        print(f"seed_seconds={seed_seconds:.3f}")
        print(f"index_seconds={index_seconds:.3f}")
        print(f"candidate_query_seconds={engine.last_candidate_query_seconds:.3f}")
        print(f"first_decision_seconds={first_seconds:.3f}")
        print(f"cached_alt_seconds={cached_seconds:.4f}")
        print(f"pre_ranked_candidates={normal_pre_rank}")
        print(f"fully_scored_finalists={normal_full_score}")
        print(f"calendar_day_seconds={calendar_seconds:.3f}")
        print(f"calendar_day_cached_seconds={calendar_cached_seconds:.4f}")
        print(f"calendar_pre_ranked={engine.last_calendar_pre_rank_count}")
        print(f"calendar_fully_scored={engine.last_calendar_full_score_count}")

        if first_seconds > 10.0:
            raise SystemExit(f"benchmark: first decision too slow ({first_seconds:.2f}s > 10s)")
        if cached_seconds > 0.40:
            raise SystemExit(f"benchmark: cached Alt film too slow ({cached_seconds:.3f}s > 0.40s)")
        if calendar_seconds > 10.0:
            raise SystemExit(f"benchmark: calendar day too slow ({calendar_seconds:.2f}s > 10s)")
        if calendar_cached_seconds > 0.25:
            raise SystemExit(f"benchmark: cached calendar day too slow ({calendar_cached_seconds:.3f}s > 0.25s)")
        if index_seconds > 12.0:
            raise SystemExit(f"benchmark: one-time index setup too slow ({index_seconds:.2f}s > 12s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
