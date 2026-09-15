from __future__ import annotations

from datetime import date
import math
import time

from .profile import get_profile
from .recommendation import FEATURE_KIND_WEIGHT, RecommendationEngine
from .semantic import feature_vector
from .util import clamp, identity_key


ENGINE_VERSION = "3.2.0"


class FastRecommendationEngine(RecommendationEngine):
    """Fast, defensive engine for a large local IMDb catalog.

    Correctness and performance are deliberately separated:
    - SQL only builds small, index-friendly candidate pools.
    - rated/seen/rejected titles are removed in memory by movie_id, IMDb Const and identity_key.
    - the same blocked-identity barrier is asserted again before results can reach the UI.
    - only a bounded shortlist receives the expensive personal/calendar scoring.
    - the top decision pool is cached, so "Alt film" does not rerun the engine.
    """

    NORMAL_POOL = 1800
    EXPLORE_POOL = 3000
    DECISION_CACHE_SIZE = 30
    MIN_PERSONAL_RATINGS = 5
    BLOCKING_FEEDBACK = ("not_interested", "seen", "never_similar")

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self._decision_cache: dict[tuple, list] = {}
        self._blocked_cache_token = None
        self._blocked_cache = (set(), set(), set())
        self.last_candidate_count = 0
        self.last_candidate_query_seconds = 0.0
        self._ensure_performance_indexes()

    def _ensure_performance_indexes(self) -> None:
        try:
            with self.db.connect() as con:
                con.execute("CREATE INDEX IF NOT EXISTS ix_movies_type_votes_v3 ON movies(title_type,num_votes DESC)")
                con.execute("CREATE INDEX IF NOT EXISTS ix_movies_type_rating_votes_v3 ON movies(title_type,imdb_rating DESC,num_votes DESC)")
                con.execute("CREATE INDEX IF NOT EXISTS ix_movies_type_year_votes_v3 ON movies(title_type,year DESC,num_votes DESC)")
                con.execute("CREATE INDEX IF NOT EXISTS ix_feedback_kind_movie_v3 ON feedback(kind,movie_id)")
        except Exception:
            pass

    def _state_token(self) -> tuple:
        with self.db.connect() as con:
            r = con.execute("SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings").fetchone()
            f = con.execute("SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback").fetchone()
            w = con.execute("SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM watchlist").fetchone()
            p = con.execute("SELECT COALESCE(MAX(updated_at),'') FROM user_profile").fetchone()
        return (int(r[0]), str(r[1]), int(f[0]), str(f[1]), int(w[0]), str(w[1]), str(p[0]))

    def _blocked_identities(self) -> tuple[set[int], set[str], set[str]]:
        token = self._state_token()[:4]
        if token == self._blocked_cache_token:
            return self._blocked_cache
        marks = ",".join("?" for _ in self.BLOCKING_FEEDBACK)
        with self.db.connect() as con:
            rated = con.execute(
                """SELECT m.id,m.imdb_id,m.identity_key
                   FROM ratings r JOIN movies m ON m.id=r.movie_id"""
            ).fetchall()
            blocked_feedback = con.execute(
                f"""SELECT DISTINCT m.id,m.imdb_id,m.identity_key
                    FROM feedback f JOIN movies m ON m.id=f.movie_id
                    WHERE f.kind IN ({marks})""",
                self.BLOCKING_FEEDBACK,
            ).fetchall()
        rows = list(rated) + list(blocked_feedback)
        ids = {int(r["id"]) for r in rows}
        imdb = {str(r["imdb_id"]) for r in rows if r["imdb_id"]}
        ident = {str(r["identity_key"]) for r in rows if r["identity_key"]}
        self._blocked_cache_token = token
        self._blocked_cache = (ids, imdb, ident)
        return self._blocked_cache

    def _eligible_sql(self) -> str:
        # No joins or correlated subqueries here. They dominated latency on 260k titles.
        # Seen/rated/rejected identities are removed from the small query results in Python
        # and asserted once again before returning recommendations.
        return """ FROM movies m
            WHERE m.title_type IN ('movie','short','tvMovie','video','Movie','TV Movie','tv movie') """

    @staticmethod
    def _is_blocked(row, blocked: tuple[set[int], set[str], set[str]]) -> bool:
        ids, imdb, ident = blocked
        mid = int(row["id"])
        iid = str(row["imdb_id"]) if row["imdb_id"] else ""
        ikey = str(row["identity_key"]) if row["identity_key"] else ""
        return mid in ids or (iid and iid in imdb) or (ikey and ikey in ident)

    @classmethod
    def _round_robin(cls, groups: list[list], limit: int, blocked) -> list:
        out = []
        seen: set[int] = set()
        pos = [0] * len(groups)
        while len(out) < limit:
            progressed = False
            for i, group in enumerate(groups):
                while pos[i] < len(group):
                    row = group[pos[i]]
                    pos[i] += 1
                    mid = int(row["id"])
                    if mid in seen or cls._is_blocked(row, blocked):
                        continue
                    seen.add(mid)
                    out.append(row)
                    progressed = True
                    break
                if len(out) >= limit:
                    break
            if not progressed:
                break
        return out

    def _effective_limit(self, requested: int, mode: str = "decide") -> int:
        ceiling = self.EXPLORE_POOL if mode in {"surprise", "calendar"} else self.NORMAL_POOL
        return max(400, min(int(requested or ceiling), ceiling))

    def _candidate_rows(self, when: date, limit: int = 100000):
        t0 = time.perf_counter()
        limit = max(400, min(int(limit or self.NORMAL_POOL), self.EXPLORE_POOL))
        base = self._eligible_sql()
        blocked = self._blocked_identities()
        # Fetch a little more than needed because pools overlap and blocked identities are
        # discarded in memory. These ORDER BY clauses are backed by the v3 indexes.
        group_fetch = max(600, int(limit * .58))
        cutoff = when.year - 10
        with self.db.connect() as con:
            watch = con.execute(
                "SELECT m.*" + base +
                " AND m.id IN (SELECT movie_id FROM watchlist) ORDER BY m.num_votes DESC LIMIT 250"
            ).fetchall()
            popular = con.execute(
                "SELECT m.*" + base + " ORDER BY m.num_votes DESC LIMIT ?", (group_fetch,)
            ).fetchall()
            hidden = con.execute(
                "SELECT m.*" + base +
                " AND m.num_votes BETWEEN 50 AND 25000 "
                "ORDER BY m.imdb_rating DESC,m.num_votes DESC LIMIT ?", (group_fetch,)
            ).fetchall()
            recent = con.execute(
                "SELECT m.*" + base +
                " AND m.year>=? ORDER BY m.num_votes DESC LIMIT ?", (cutoff, group_fetch)
            ).fetchall()
            quality = con.execute(
                "SELECT m.*" + base +
                " AND m.num_votes>=250 ORDER BY m.imdb_rating DESC,m.num_votes DESC LIMIT ?", (group_fetch,)
            ).fetchall()
        rows = self._round_robin(
            [list(watch), list(popular), list(hidden), list(recent), list(quality)], limit, blocked
        )
        self.last_candidate_count = len(rows)
        self.last_candidate_query_seconds = time.perf_counter() - t0
        return rows

    def _predict_user_rating(self, movie, profile: dict):
        feats = profile.get("features", {})
        global_mean = float(profile.get("global_mean_rating", 6.5) or 6.5)
        vec = feature_vector(movie)
        num = den = evidence = 0.0
        details = []
        for feature, fw in vec.items():
            st = feats.get(feature)
            if not st or st.get("mean_rating") is None:
                continue
            count = max(0, int(st.get("count", 0) or 0))
            mean = float(st.get("mean_rating", global_mean))
            std = float(st.get("std_rating", 1.6) or 1.6)
            reliability = count / (count + 6.0)
            precision = 1.0 / (.85 + max(.40, std))
            kind_w = FEATURE_KIND_WEIGHT.get(feature.split(":", 1)[0], .5)
            w = max(.05, float(fw)) * kind_w * reliability * precision
            residual = mean - global_mean
            num += residual * w
            den += abs(w)
            ev = max(.05, float(fw)) * kind_w * min(1.0, count / 14.0)
            evidence += ev
            details.append({
                "feature": feature, "mean": mean, "count": count, "std": std,
                "weight": w, "support": residual * w,
            })

        raw_delta = (num / den) if den else 0.0
        shrink = evidence / (evidence + .85) if evidence > 0 else 0.0
        predicted = max(1.0, min(10.0, global_mean + raw_delta * shrink))

        metadata = 0.0
        if movie.genres: metadata += .30
        if movie.directors: metadata += .20
        if movie.runtime_min: metadata += .10
        if movie.semantic or movie.overview or movie.keywords: metadata += .25
        if movie.year: metadata += .15

        # Confidence measures personal evidence. Metadata can add only a small amount.
        confidence = .04 + .88 * (1.0 - math.exp(-evidence / 2.45)) + .05 * metadata
        confidence = clamp(confidence)
        details.sort(key=lambda x: abs(x["support"]), reverse=True)
        return predicted, confidence, evidence, details

    def _score_one(self, movie, when, profile, context, exclude_romance=True, mode="decide"):
        score = super()._score_one(movie, when, profile, context, exclude_romance, mode)
        if score is None:
            return None
        if score.evidence < .08:
            penalty = .18
            score.final = clamp(score.final - penalty)
            score.personal_reason = (
                f"Date personale insuficiente pentru o predicție fermă. {score.predicted_rating:.1f}/10 "
                "este aproape de media ta generală, nu o potrivire confirmată."
            )
            score.contributions.append((
                "Dovezi personale insuficiente", -penalty * 100,
                "Titlul nu are încă suficiente caracteristici comparabile cu ratingurile tale."
            ))
        elif score.evidence < .35:
            penalty = .08
            score.final = clamp(score.final - penalty)
            score.contributions.append((
                "Dovezi personale puține", -penalty * 100,
                "Predicția rămâne conservatoare până există mai multe semnale personale."
            ))
        return score

    def _assert_no_blocked_leak(self, recs) -> None:
        ids, imdb, ident = self._blocked_identities()
        leaks = []
        for rec in recs:
            m = rec.movie
            ikey = identity_key(m.title, m.original_title or m.title, m.year, m.title_type or "Movie")
            if int(m.id) in ids or (m.imdb_id and m.imdb_id in imdb) or ikey in ident:
                leaks.append(m.imdb_id or m.title)
        if leaks:
            raise RuntimeError("Protecția anti-văzut a detectat un titlu blocat: " + ", ".join(leaks[:3]))

    def recommend(self, when: date | None = None, count: int = 3, exclude_ids: set[int] | None = None,
                  record: bool = False, slot: str = "today", candidate_limit: int = 100000,
                  mode: str = "decide", runtime_max: int | None = None, runtime_min: int | None = None):
        profile = get_profile(self.db)
        rated_count = int(profile.get("rated_count", 0) or 0)
        if rated_count < self.MIN_PERSONAL_RATINGS:
            raise RuntimeError(
                "Nu am suficiente ratinguri personale încărcate pentru recomandări. "
                "CineCalendar nu va inventa un scor «pentru tine»."
            )
        effective = self._effective_limit(candidate_limit, mode)
        recs = super().recommend(
            when=when, count=count, exclude_ids=exclude_ids, record=record, slot=slot,
            candidate_limit=effective, mode=mode, runtime_max=runtime_max, runtime_min=runtime_min,
        )
        self._assert_no_blocked_leak(recs)
        return recs

    def decision_pick(self, when: date | None = None, exclude_ids: set[int] | None = None,
                      mode: str = "decide"):
        when = when or date.today()
        exclude_ids = set(exclude_ids or set())
        key = (when.isoformat(), mode, self._state_token())
        pool = self._decision_cache.get(key)
        if pool is None:
            pool = self.recommend(
                when, self.DECISION_CACHE_SIZE, record=False, slot="decision",
                candidate_limit=self.NORMAL_POOL if mode != "surprise" else self.EXPLORE_POOL,
                mode=mode,
            )
            self._decision_cache = {key: list(pool)}
        available = [r for r in pool if int(r.movie.id) not in exclude_ids]
        if len(available) < 3:
            refill = self.recommend(
                when, self.DECISION_CACHE_SIZE, exclude_ids=exclude_ids, record=False,
                slot="decision-refill", candidate_limit=self.EXPLORE_POOL, mode=mode,
            )
            known = {int(r.movie.id) for r in available}
            available.extend(r for r in refill if int(r.movie.id) not in known)
        self._assert_no_blocked_leak(available[:3])
        return (available[0] if available else None, available[1:3])
