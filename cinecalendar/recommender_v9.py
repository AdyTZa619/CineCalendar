from __future__ import annotations

from datetime import date
import time

from .models import Recommendation
from .profile import get_profile
from .recommendation import romance_policy, row_to_movie
from .recommender_v8 import FastRecommendationEngineV8


ENGINE_VERSION = "9.0.0"


class FastRecommendationEngineV9(FastRecommendationEngineV8):
    """HDD-friendly recommender.

    Previous engines asked SQLite for `m.*` separately from five ordered candidate pools.
    On an SSD this is acceptable, but on a mechanical disk it becomes thousands of random
    table-page reads: Task Manager shows ~100% active time at only a few hundred KB/s.

    v9 keeps the same ranking logic but does candidate discovery in two phases:
      1. ordered pools return only rowids (served from small B-tree indexes);
      2. the final ~1.8k unique rows are hydrated in rowid order in a few batched queries.

    It also persists the already-selected daily decision pool. On the next application start,
    if ratings/feedback/watchlist/profile state is unchanged, only that small pool is rescored
    instead of rebuilding the large shortlist from the HDD again.
    """

    HYDRATE_CHUNK = 700
    PERSISTED_POOL_LIMIT = 30

    def _balanced_candidate_ids(self, when: date, limit: int) -> list[int]:
        limit = max(400, min(int(limit or self.NORMAL_POOL), self.EXPLORE_POOL))
        base = """ FROM movies m
            WHERE m.title_type IN ('movie','short','tvMovie','video','Movie','TV Movie','tv movie') """
        blocked_ids = self._blocked_identities()[0]
        group_fetch = max(600, int(limit * .58))
        cutoff = when.year - 10

        with self.db.connect() as con:
            watch = con.execute(
                "SELECT m.id" + base +
                " AND m.id IN (SELECT movie_id FROM watchlist) ORDER BY m.num_votes DESC LIMIT 250"
            ).fetchall()
            popular = con.execute(
                "SELECT m.id" + base + " ORDER BY m.num_votes DESC LIMIT ?", (group_fetch,)
            ).fetchall()
            hidden = con.execute(
                "SELECT m.id" + base +
                " AND m.num_votes BETWEEN 50 AND 25000 "
                "ORDER BY m.imdb_rating DESC,m.num_votes DESC LIMIT ?", (group_fetch,)
            ).fetchall()
            recent = con.execute(
                "SELECT m.id" + base +
                " AND m.year>=? ORDER BY m.num_votes DESC LIMIT ?", (cutoff, group_fetch)
            ).fetchall()
            quality = con.execute(
                "SELECT m.id" + base +
                " AND m.num_votes>=250 ORDER BY m.imdb_rating DESC,m.num_votes DESC LIMIT ?", (group_fetch,)
            ).fetchall()

        groups = [[int(r["id"]) for r in g] for g in (watch, popular, hidden, recent, quality)]
        out: list[int] = []
        seen: set[int] = set()
        pos = [0] * len(groups)
        while len(out) < limit:
            progressed = False
            for i, group in enumerate(groups):
                while pos[i] < len(group):
                    mid = group[pos[i]]
                    pos[i] += 1
                    if mid in seen or mid in blocked_ids:
                        continue
                    seen.add(mid)
                    out.append(mid)
                    progressed = True
                    break
                if len(out) >= limit:
                    break
            if not progressed:
                break
        return out

    def _hydrate_ids(self, ordered_ids: list[int]):
        if not ordered_ids:
            return []
        rows_by_id = {}
        # Ascending rowid access is dramatically friendlier to a spinning disk than fetching
        # wide rows through five different secondary-index orders.
        sorted_ids = sorted(set(int(x) for x in ordered_ids))
        with self.db.connect() as con:
            for start in range(0, len(sorted_ids), self.HYDRATE_CHUNK):
                chunk = sorted_ids[start:start + self.HYDRATE_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"SELECT * FROM movies WHERE id IN ({marks}) ORDER BY id", tuple(chunk)
                ).fetchall()
                for row in rows:
                    rows_by_id[int(row["id"])] = row

        blocked = self._blocked_identities()
        out = []
        for mid in ordered_ids:
            row = rows_by_id.get(int(mid))
            if row is None or self._is_blocked(row, blocked):
                continue
            out.append(row)
        return out

    def _candidate_rows(self, when: date, limit: int = 100000):
        t0 = time.perf_counter()
        effective = max(400, min(int(limit or self.NORMAL_POOL), self.EXPLORE_POOL))
        base_ids = self._balanced_candidate_ids(when, effective)
        base = self._hydrate_ids(base_ids)

        if self._calendar_candidate_mode:
            # v8's strict event lookup remains separate. It is used only after the user opens
            # Program calendar; ordinary Home/Browse never scans event metadata.
            event_rows = self._event_candidate_rows(when, self.EVENT_SCAN_LIMIT)
            if event_rows:
                merged = []
                seen: set[int] = set()
                for row in list(event_rows) + list(base):
                    mid = int(row["id"])
                    if mid in seen:
                        continue
                    seen.add(mid)
                    merged.append(row)
                    if len(merged) >= effective:
                        break
                base = merged

        self.last_candidate_count = len(base)
        self.last_candidate_query_seconds = time.perf_counter() - t0
        return base

    def _persistent_key(self, when: date, mode: str) -> str:
        return f"decision_pool_v9:{when.isoformat()}:{mode}"

    def _load_persisted_decision_pool(self, when: date, mode: str):
        payload = self.db.get_setting(self._persistent_key(when, mode), None)
        if not isinstance(payload, dict) or payload.get("engine") != ENGINE_VERSION:
            return None
        stored_token = payload.get("state_token")
        if not isinstance(stored_token, list) or tuple(stored_token) != tuple(self._state_token()):
            return None
        ids = [int(x) for x in (payload.get("movie_ids") or [])[:self.PERSISTED_POOL_LIMIT] if str(x).isdigit()]
        if len(ids) < 3:
            return None

        rows = self._hydrate_ids(ids)
        if len(rows) < 3:
            return None
        profile = get_profile(self.db)
        context = self._run_context()
        exclude_romance = bool(self.db.get_setting("exclude_romance", True))
        recs: list[Recommendation] = []
        for row in rows:
            movie = row_to_movie(row)
            allowed, _penalty, _reason = romance_policy(movie, exclude_romance)
            if not allowed:
                continue
            score = self._score_one(movie, when, profile, context, exclude_romance, mode)
            if score is not None:
                recs.append(Recommendation(movie, score))
        recs.sort(key=lambda r: (r.score.final, r.score.predicted_rating, r.score.confidence), reverse=True)
        self._assert_no_blocked_leak(recs)
        return recs

    def _save_persisted_decision_pool(self, when: date, mode: str, pool) -> None:
        try:
            self.db.set_setting(self._persistent_key(when, mode), {
                "engine": ENGINE_VERSION,
                "state_token": list(self._state_token()),
                "movie_ids": [int(r.movie.id) for r in list(pool)[:self.PERSISTED_POOL_LIMIT]],
            })
        except Exception:
            # Cache persistence must never break recommendations.
            pass

    def decision_pick(self, when: date | None = None, exclude_ids: set[int] | None = None,
                      mode: str = "decide"):
        when = when or date.today()
        exclude_ids = set(exclude_ids or set())
        key = (when.isoformat(), mode, self._state_token())

        pool = self._decision_cache.get(key)
        if pool is None:
            pool = self._load_persisted_decision_pool(when, mode)
            if pool:
                self._decision_cache = {key: list(pool)}

        if pool is None:
            # Let the proven v3-v8 decision path build the pool using v9's HDD-friendly
            # _candidate_rows override, then persist that small pool for later restarts.
            result = super().decision_pick(when, exclude_ids, mode)
            pool = self._decision_cache.get(key)
            if pool:
                self._save_persisted_decision_pool(when, mode, pool)
            return result

        available = [r for r in pool if int(r.movie.id) not in exclude_ids]
        if len(available) < 3:
            # Session skips can exhaust a persisted pool; rebuild normally in that rare case.
            self._decision_cache.pop(key, None)
            result = super().decision_pick(when, exclude_ids, mode)
            rebuilt = self._decision_cache.get(key)
            if rebuilt:
                self._save_persisted_decision_pool(when, mode, rebuilt)
            return result

        self._assert_no_blocked_leak(available[:3])
        return available[0], available[1:3]
