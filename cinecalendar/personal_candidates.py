from __future__ import annotations

import threading

import numpy as np


class PersonalCandidateGenerator:
    """Generate candidates from the user's actual taste before the expensive reranker runs.

    The older pipeline first built a mostly generic IMDb shortlist (popular/recent/high-rated)
    and only then asked ALS whether those films fit the user. That can never recover a perfect
    personal match that was absent from the generic shortlist.

    This generator inverts that bottleneck when the MovieLens model is ready:
      * global ALS: rank every mapped MovieLens/IMDb item against the locally folded user factor;
      * favourite neighbours: independently retrieve titles close to the user's explicit 9/10
        and 10/10 anchors in item-factor space;
      * the caller mixes those groups with the established generic/content pools, which preserve
        coverage for new films and titles not present in MovieLens.

    Personal ratings never leave the machine. The expensive all-catalog calculations are cached
    until ratings/feedback or the public model version changes.
    """

    FAVORITE_SEEDS = 12

    def __init__(self, db, collaborative):
        self.db = db
        self.collaborative = collaborative
        self._lock = threading.RLock()
        self._cache_token = None
        self._cache: dict[str, list[str]] = {"als": [], "favorites": []}

    @staticmethod
    def _top_indices(scores: np.ndarray, limit: int, excluded: set[int] | None = None) -> list[int]:
        limit = max(0, int(limit))
        if limit <= 0:
            return []
        values = np.asarray(scores, dtype=np.float64).reshape(-1)
        valid = np.isfinite(values)
        for idx in excluded or set():
            if 0 <= int(idx) < valid.size:
                valid[int(idx)] = False
        available = np.flatnonzero(valid)
        if available.size == 0:
            return []
        take = min(limit, int(available.size))
        if take == int(available.size):
            chosen = available
        else:
            local = np.argpartition(values[available], -take)[-take:]
            chosen = available[local]
        ordered = chosen[np.argsort(values[chosen], kind="stable")[::-1]]
        return [int(x) for x in ordered]

    def _favorite_seed_items(self, mapping: dict[str, int]) -> list[tuple[int, int]]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.imdb_id,r.rating
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   WHERE r.rating>=9 AND m.imdb_id IS NOT NULL
                   ORDER BY r.rating DESC, COALESCE(r.date_rated,r.updated_at) DESC
                   LIMIT 48"""
            ).fetchall()
        out: list[tuple[int, int]] = []
        seen: set[int] = set()
        for row in rows:
            item = mapping.get(str(row["imdb_id"] or ""))
            if item is None or int(item) in seen:
                continue
            seen.add(int(item))
            out.append((int(item), int(row["rating"])))
            if len(out) >= self.FAVORITE_SEEDS:
                break
        return out

    @staticmethod
    def _indices_to_imdb(indices: list[int], item_to_imdb: np.ndarray) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in indices:
            if item < 0 or item >= len(item_to_imdb):
                continue
            imdb_num = int(item_to_imdb[item])
            if imdb_num <= 0:
                continue
            iid = f"tt{imdb_num:07d}"
            if iid in seen:
                continue
            seen.add(iid)
            out.append(iid)
        return out

    def groups(self, als_limit: int = 1000, favorite_limit: int = 450) -> dict[str, list[str]]:
        """Return globally personalized IMDb candidate groups, best first.

        The method is intentionally fail-open: while ALS is still warming, or if the model cannot
        be folded for this user, the caller simply keeps the established generic candidate path.
        """
        if not self.collaborative.is_ready():
            return {"als": [], "favorites": []}

        user_items, user_factor, mapped_ratings = self.collaborative._build_user_representation()
        if user_items is None or user_factor is None or int(mapped_ratings) < 20:
            return {"als": [], "favorites": []}

        with self.collaborative._lock:
            model = self.collaborative._model
            item_to_imdb = self.collaborative._item_to_imdb
            mapping = dict(self.collaborative._imdb_to_item)
            model_version = str(self.collaborative._version or "")
            history_token = self.collaborative._history_token
        if model is None or item_to_imdb is None:
            return {"als": [], "favorites": []}

        cache_token = (history_token, model_version, int(als_limit), int(favorite_limit))
        with self._lock:
            if cache_token == self._cache_token:
                return {key: list(value) for key, value in self._cache.items()}

        factors = np.asarray(model.item_factors, dtype=np.float32)
        factor = np.asarray(user_factor, dtype=np.float32).reshape(-1)
        if factors.ndim != 2 or factor.ndim != 1 or factors.shape[1] != factor.shape[0]:
            return {"als": [], "favorites": []}

        interacted = {int(x) for x in np.asarray(user_items.indices, dtype=np.int64).tolist()}

        # 1) Global personal retrieval. Unlike the old shortlist-first design this considers every
        # mapped MovieLens item before the IMDb/content engine narrows the final set.
        als_raw = np.asarray(factors.dot(factor), dtype=np.float64)
        als_indices = self._top_indices(als_raw, als_limit, interacted)
        als_ids = self._indices_to_imdb(als_indices, item_to_imdb)

        # 2) Explicit favourite anchors. ALS summarizes the whole profile, which can smooth over a
        # strong niche. This independent lane retrieves films close to the user's 9/10 and 10/10
        # items, then the downstream content/adaptive models decide whether they really belong.
        favorite_pairs = self._favorite_seed_items(mapping)
        favorite_indices: list[int] = []
        if favorite_pairs and favorite_limit > 0:
            seed_indices = np.asarray([item for item, _rating in favorite_pairs], dtype=np.int32)
            seed_factors = factors[seed_indices]
            seed_norms = np.linalg.norm(seed_factors, axis=1)
            valid_seed = seed_norms > 1e-8
            if bool(valid_seed.any()):
                seed_factors = seed_factors[valid_seed]
                seed_norms = seed_norms[valid_seed]
                seed_ratings = np.asarray(
                    [rating for (_item, rating), keep in zip(favorite_pairs, valid_seed) if keep],
                    dtype=np.float32,
                )
                normalized_seeds = seed_factors / seed_norms[:, None]
                item_norms = np.linalg.norm(factors, axis=1)
                similarities = factors.dot(normalized_seeds.T)
                similarities = similarities / np.maximum(item_norms[:, None], 1e-8)
                # A 10/10 anchor gets a small extra vote over a 9/10 anchor without overwhelming
                # the geometry of the public model.
                seed_weights = 1.0 + 0.10 * np.maximum(0.0, seed_ratings - 9.0)
                favorite_scores = np.max(similarities * seed_weights[None, :], axis=1)
                excluded = interacted | set(als_indices)
                favorite_indices = self._top_indices(favorite_scores, favorite_limit, excluded)
        favorite_ids = self._indices_to_imdb(favorite_indices, item_to_imdb)

        payload = {"als": als_ids, "favorites": favorite_ids}
        with self._lock:
            self._cache_token = cache_token
            self._cache = {key: list(value) for key, value in payload.items()}
        return payload
