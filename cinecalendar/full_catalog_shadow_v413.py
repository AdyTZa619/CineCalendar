from __future__ import annotations

"""Full-catalog local retrieval evaluated without changing production recommendations."""

from dataclasses import dataclass
from datetime import date
import hashlib
import math
import threading
import time

from .profile import get_profile
from .recommendation import row_to_movie
from .semantic import feature_vector
from .util import normalize_text, utcnow_iso


FULL_CATALOG_RETRIEVAL_VERSION = "full-catalog-retrieval-v4.13.0"
SHADOW_EVALUATION_VERSION = "retrieval-shadow-v4.13.0"
MAX_SIGNALS = 48
PER_SIGNAL_LIMIT = 90
MAX_HYDRATED_CANDIDATES = 5000


@dataclass(frozen=True)
class RetrievedCandidate:
    movie_id: int
    score: float
    personal_score: float
    matched_features: int


class FullCatalogCandidateGeneratorV413:
    """Retrieve strong personal matches outside the loaded MovieLens mapping.

    The first stage uses bounded SQL probes derived from stable profile features. The second stage
    applies the same sparse feature vocabulary used by the existing taste profile. This avoids a
    full Python scan of the 260k catalog while covering directors, genres, countries, themes,
    decades, runtime and their existing interaction features.
    """

    def __init__(self, db, collaborative):
        self.db = db
        self.collaborative = collaborative
        self._lock = threading.RLock()
        self._cache_token = None
        self._cache: tuple[RetrievedCandidate, ...] = ()
        self._status = {
            "version": FULL_CATALOG_RETRIEVAL_VERSION,
            "state": "idle",
            "candidate_count": 0,
            "cache_hit": False,
        }

    def _state_token(self) -> tuple:
        with self.db.connect() as con:
            ratings = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings"
            ).fetchone()
            feedback = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback"
            ).fetchone()
            catalog = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM movies"
            ).fetchone()
        return (
            FULL_CATALOG_RETRIEVAL_VERSION,
            int(ratings[0] or 0), str(ratings[1] or ""),
            int(feedback[0] or 0), str(feedback[1] or ""),
            int(catalog[0] or 0), str(catalog[1] or ""),
            tuple(self.collaborative.state_token()),
        )

    @staticmethod
    def _profile_signals(profile: dict) -> list[tuple[str, float, int]]:
        signals = []
        for feature, raw in dict(profile.get("features") or {}).items():
            preference = float((raw or {}).get("preference", 0.0) or 0.0)
            count = int((raw or {}).get("count", 0) or 0)
            if preference <= 0.04 or count < 2:
                continue
            prefix = str(feature).split(":", 1)[0]
            if prefix not in {"director", "genre", "country", "theme", "decade", "runtime", "combo"}:
                continue
            reliability = min(1.0, count / 6.0)
            signals.append((str(feature), preference * (0.55 + 0.45 * reliability), count))
        signals.sort(key=lambda item: (item[1], item[2]), reverse=True)
        return signals[:MAX_SIGNALS]

    @staticmethod
    def _eligible_clause() -> str:
        return """
            r.movie_id IS NULL
            AND m.id NOT IN (
                SELECT movie_id FROM feedback
                WHERE kind IN ('not_interested','seen','never_similar')
            )
            AND lower(COALESCE(m.title_type,'movie')) IN ('movie','short','tvmovie','video','tv movie')
        """

    def _query_signal(self, con, feature: str) -> list[int]:
        prefix, _, value = feature.partition(":")
        value = normalize_text(value) if prefix in {"director", "genre", "country", "theme"} else value.strip().lower()
        if not value:
            return []
        base = "SELECT DISTINCT m.id FROM movies m LEFT JOIN ratings r ON r.movie_id=m.id WHERE " + self._eligible_clause()
        params: tuple[object, ...]
        if prefix in {"director", "genre", "country"}:
            column = {"director": "directors_json", "genre": "genres_json", "country": "countries_json"}[prefix]
            query = base + f" AND EXISTS (SELECT 1 FROM json_each(COALESCE(m.{column},'[]')) j WHERE lower(trim(CAST(j.value AS TEXT)))=?)"
            params = (value,)
        elif prefix == "theme":
            query = base + " AND json_extract(COALESCE(m.semantic_json,'{}'),?) IS NOT NULL"
            params = (f'$."{value}"',)
        elif prefix == "decade" and value.endswith("s") and value[:-1].isdigit():
            start = int(value[:-1])
            query = base + " AND m.year>=? AND m.year<?"
            params = (start, start + 10)
        elif prefix == "runtime":
            ranges = {
                "<90": ("m.runtime_min<90", ()),
                "90-120": ("m.runtime_min BETWEEN 90 AND 120", ()),
                "121-150": ("m.runtime_min BETWEEN 121 AND 150", ()),
                ">150": ("m.runtime_min>150", ()),
            }
            condition = ranges.get(value)
            if condition is None:
                return []
            query = base + " AND " + condition[0]
            params = condition[1]
        elif prefix == "combo":
            # Components are already queried independently; scoring below evaluates the combo.
            return []
        else:
            return []
        query += " ORDER BY COALESCE(m.imdb_rating,0) DESC,COALESCE(m.num_votes,0) DESC LIMIT ?"
        return [int(row[0]) for row in con.execute(query, params + (PER_SIGNAL_LIMIT,)).fetchall()]

    @staticmethod
    def _public_quality(movie) -> float:
        rating = float(movie.imdb_rating or 0.0)
        votes = max(0, int(movie.num_votes or 0))
        rating_part = max(0.0, min(1.0, (rating - 5.0) / 3.5)) if rating else 0.40
        vote_part = min(1.0, math.log1p(votes) / math.log1p(150000)) if votes else 0.20
        return 0.72 * rating_part + 0.28 * vote_part

    def _score_rows(self, rows, profile: dict) -> list[RetrievedCandidate]:
        stats = dict(profile.get("features") or {})
        out: list[RetrievedCandidate] = []
        for row in rows:
            imdb_id = str(row["imdb_id"] or "")
            if imdb_id and self.collaborative.has_mapping(imdb_id):
                continue
            movie = row_to_movie(row)
            vector = feature_vector(movie)
            weighted = weight_total = 0.0
            matches = 0
            for feature, feature_weight in vector.items():
                evidence = stats.get(feature)
                if not evidence:
                    continue
                preference = float(evidence.get("preference", 0.0) or 0.0)
                count = int(evidence.get("count", 0) or 0)
                reliability = min(1.0, count / 6.0)
                weight = float(feature_weight) * (0.55 + 0.45 * reliability)
                weighted += preference * weight
                weight_total += weight
                matches += 1
            if matches < 2 or weight_total <= 0:
                continue
            personal = max(-1.0, min(1.0, weighted / weight_total))
            if personal <= 0.02:
                continue
            completeness = sum(bool(value) for value in (
                movie.genres, movie.directors, movie.countries,
                movie.overview or movie.keywords, movie.runtime_min,
            )) / 5.0
            score = 0.82 * ((personal + 1.0) / 2.0) + 0.12 * self._public_quality(movie) + 0.06 * completeness
            out.append(RetrievedCandidate(int(movie.id), min(1.0, score), personal, matches))
        out.sort(key=lambda item: (item.score, item.personal_score, item.matched_features), reverse=True)
        return out

    def candidates(self, limit: int = 250) -> list[RetrievedCandidate]:
        limit = max(1, int(limit))
        if not self.collaborative.is_ready():
            with self._lock:
                self._status = {
                    "version": FULL_CATALOG_RETRIEVAL_VERSION,
                    "state": "waiting_als_mapping",
                    "candidate_count": 0,
                    "cache_hit": False,
                }
            return []
        token = self._state_token()
        with self._lock:
            if token == self._cache_token and self._cache:
                self._status["cache_hit"] = True
                return list(self._cache[:limit])

        started = time.perf_counter()
        profile = get_profile(self.db)
        signals = self._profile_signals(profile)
        ids: list[int] = []
        seen: set[int] = set()
        with self.db.connect() as con:
            for feature, _strength, _count in signals:
                for movie_id in self._query_signal(con, feature):
                    if movie_id not in seen:
                        seen.add(movie_id)
                        ids.append(movie_id)
                        if len(ids) >= MAX_HYDRATED_CANDIDATES:
                            break
                if len(ids) >= MAX_HYDRATED_CANDIDATES:
                    break
            rows = []
            for start in range(0, len(ids), 700):
                chunk = ids[start:start + 700]
                marks = ",".join("?" for _ in chunk)
                by_id = {
                    int(row["id"]): row
                    for row in con.execute(f"SELECT * FROM movies WHERE id IN ({marks})", tuple(chunk)).fetchall()
                }
                rows.extend(by_id[mid] for mid in chunk if mid in by_id)
        scored = self._score_rows(rows, profile)
        elapsed_ms = int(round((time.perf_counter() - started) * 1000.0))
        with self._lock:
            self._cache_token = token
            self._cache = tuple(scored)
            self._status = {
                "version": FULL_CATALOG_RETRIEVAL_VERSION,
                "state": "ready",
                "profile_signals": len(signals),
                "probed_candidates": len(ids),
                "non_als_candidates": len(scored),
                "candidate_count": len(scored),
                "duration_ms": elapsed_ms,
                "cache_hit": False,
            }
        return list(scored[:limit])

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)


class FullCatalogShadowEvaluatorV413:
    """Persist counterfactual retrieval lists; never returns them to the production ranker."""

    MIN_CHALLENGER_RATINGS_FOR_REVIEW = 20

    def __init__(self, db, collaborative, baseline_engine: str):
        self.db = db
        self.generator = FullCatalogCandidateGeneratorV413(db, collaborative)
        self.baseline_engine = str(baseline_engine or "unknown")
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_error = ""

    def _run_key(self, context_date: str, slot: str, baseline_ids: list[int]) -> str:
        raw = "|".join((
            SHADOW_EVALUATION_VERSION, self.baseline_engine, str(context_date), str(slot),
            ",".join(str(int(mid)) for mid in baseline_ids),
        ))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def run_once(self, context_date: str, slot: str, baseline_ids: list[int]) -> dict:
        baseline = []
        seen = set()
        for raw in baseline_ids:
            movie_id = int(raw)
            if movie_id > 0 and movie_id not in seen:
                seen.add(movie_id); baseline.append(movie_id)
        if not baseline:
            return {"state": "empty_baseline"}
        run_key = self._run_key(context_date, slot, baseline)
        with self.db.connect() as con:
            existing = con.execute(
                "SELECT id FROM retrieval_shadow_runs WHERE run_key=?", (run_key,)
            ).fetchone()
        if existing:
            return {"state": "already_recorded", "run_id": int(existing[0])}

        started = time.perf_counter()
        challenger = self.generator.candidates(max(len(baseline), 12))
        if not challenger:
            return {"state": self.generator.status().get("state", "no_candidates")}
        challenger = challenger[:len(baseline)]
        challenger_ids = [item.movie_id for item in challenger]
        now = utcnow_iso()
        duration_ms = int(round((time.perf_counter() - started) * 1000.0))
        with self.db.tx() as con:
            cur = con.execute(
                """INSERT INTO retrieval_shadow_runs(
                       run_key,context_date,slot,generated_at,baseline_engine,challenger_version,
                       depth,baseline_count,challenger_count,overlap_count,duration_ms,status,error
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'complete','')""",
                (
                    run_key, str(context_date)[:10], str(slot), now, self.baseline_engine,
                    FULL_CATALOG_RETRIEVAL_VERSION, len(baseline), len(baseline), len(challenger_ids),
                    len(set(baseline) & set(challenger_ids)), duration_ms,
                ),
            )
            run_id = int(cur.lastrowid)
            for rank, movie_id in enumerate(baseline, 1):
                con.execute(
                    "INSERT INTO retrieval_shadow_items(run_id,source,movie_id,rank_position,retrieval_score) VALUES(?,'baseline',?,?,NULL)",
                    (run_id, movie_id, rank),
                )
            for rank, item in enumerate(challenger, 1):
                con.execute(
                    "INSERT INTO retrieval_shadow_items(run_id,source,movie_id,rank_position,retrieval_score) VALUES(?,'challenger',?,?,?)",
                    (run_id, item.movie_id, rank, item.score),
                )
        return {
            "state": "recorded", "run_id": run_id, "baseline": len(baseline),
            "challenger": len(challenger_ids), "overlap": len(set(baseline) & set(challenger_ids)),
            "duration_ms": duration_ms,
        }

    def schedule(self, context_date: date | str, slot: str, baseline_ids: list[int]) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            def worker():
                try:
                    self.run_once(str(context_date)[:10], slot, baseline_ids)
                    self._last_error = ""
                except Exception as exc:
                    self._last_error = str(exc)

            self._thread = threading.Thread(target=worker, name="CineCalendar-RetrievalShadow", daemon=True)
            self._thread.start()
            return True

    def _source_metrics(self, source: str) -> dict:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT r.rating
                   FROM ratings r
                   WHERE EXISTS (
                       SELECT 1
                       FROM retrieval_shadow_items i
                       JOIN retrieval_shadow_runs run ON run.id=i.run_id
                       WHERE i.source=? AND i.movie_id=r.movie_id
                         AND substr(COALESCE(r.date_rated,r.updated_at,''),1,10)>=run.context_date
                   )""",
                (source,),
            ).fetchall()
        ratings = [int(row[0]) for row in rows]
        return {
            "rated": len(ratings),
            "average_rating": (sum(ratings) / len(ratings)) if ratings else None,
            "liked": sum(value >= 8 for value in ratings),
            "liked_rate": (sum(value >= 8 for value in ratings) / len(ratings)) if ratings else None,
        }

    def status(self) -> dict:
        with self.db.connect() as con:
            total = int(con.execute("SELECT COUNT(*) FROM retrieval_shadow_runs").fetchone()[0] or 0)
            latest = con.execute(
                """SELECT context_date,slot,baseline_count,challenger_count,overlap_count,duration_ms
                   FROM retrieval_shadow_runs ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        baseline = self._source_metrics("baseline")
        challenger = self._source_metrics("challenger")
        return {
            "version": SHADOW_EVALUATION_VERSION,
            "state": "running" if self._thread and self._thread.is_alive() else ("collecting" if total else "waiting"),
            "production_unchanged": True,
            "runs": total,
            "latest": dict(latest) if latest is not None else {},
            "baseline": baseline,
            "challenger": challenger,
            "eligible_for_offline_review": challenger["rated"] >= self.MIN_CHALLENGER_RATINGS_FOR_REVIEW,
            "minimum_challenger_ratings": self.MIN_CHALLENGER_RATINGS_FOR_REVIEW,
            "generator": self.generator.status(),
            "last_error": self._last_error,
        }
