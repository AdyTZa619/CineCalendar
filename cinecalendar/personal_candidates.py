from __future__ import annotations

import threading

import numpy as np


RETRIEVAL_VERSION = "personal-candidates-v2-contrastive-consensus"


class PersonalCandidateGenerator:
    """Generate personal candidates before the expensive final reranker.

    Retrieval v2 fixes two weaknesses of the first personal-candidate lane:
      * a profile with many 9/10-10/10 films was represented by only 12 favourite anchors;
      * the favourite-neighbour lane had no explicit protection against candidates that were also
        very close to films the user rated 1-4.

    The ALS lane remains the broad long-term collaborative signal.  The favourite lane is now a
    contrastive consensus retrieval: many positive anchors vote for a candidate, while strong
    similarity to explicit low-rated anchors applies a conservative penalty.  This changes only
    which good candidates reach the scorer; downstream quality gates still decide the final order.

    Personal ratings never leave the machine.  All-catalog matrix work is cached until ratings,
    feedback, the public ALS model, the retrieval version or requested limits change.
    """

    FAVORITE_SEEDS = 64
    NEGATIVE_SEEDS = 64
    SEED_QUERY_LIMIT = 192
    POSITIVE_CONSENSUS_TOPK = 3
    NEGATIVE_CONSENSUS_TOPK = 2
    NEGATIVE_PENALTY_THRESHOLD = 0.58
    NEGATIVE_PENALTY_MAX = 0.18

    def __init__(self, db, collaborative):
        self.db = db
        self.collaborative = collaborative
        self._lock = threading.RLock()
        self._cache_token = None
        self._cache: dict[str, list[str]] = {"als": [], "favorites": []}
        self._last_stats = {
            "retrieval_version": RETRIEVAL_VERSION,
            "mapped_ratings": 0,
            "positive_anchors": 0,
            "negative_anchors": 0,
            "als_candidates": 0,
            "favorite_candidates": 0,
        }

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

    def _anchor_seed_items(
        self,
        mapping: dict[str, int],
        *,
        positive: bool,
        limit: int,
    ) -> list[tuple[int, int]]:
        """Return mapped explicit anchors, strongest and newest first.

        Filtering is repeated in Python as a defensive measure.  Besides making the method robust
        to odd SQLite adapters/test doubles, it ensures a malformed query result can never turn a
        high-rated film into a negative anchor or vice versa.
        """
        if limit <= 0:
            return []
        condition = "r.rating>=9" if positive else "r.rating<=4"
        direction = "DESC" if positive else "ASC"
        with self.db.connect() as con:
            rows = con.execute(
                f"""SELECT m.imdb_id,r.rating
                    FROM ratings r JOIN movies m ON m.id=r.movie_id
                    WHERE {condition} AND m.imdb_id IS NOT NULL
                    ORDER BY r.rating {direction}, COALESCE(r.date_rated,r.updated_at) DESC
                    LIMIT ?""",
                (max(int(limit), self.SEED_QUERY_LIMIT),),
            ).fetchall()

        out: list[tuple[int, int]] = []
        seen: set[int] = set()
        for row in rows:
            rating = int(row["rating"])
            if positive and rating < 9:
                continue
            if not positive and rating > 4:
                continue
            item = mapping.get(str(row["imdb_id"] or ""))
            if item is None or int(item) in seen:
                continue
            seen.add(int(item))
            out.append((int(item), rating))
            if len(out) >= int(limit):
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

    @staticmethod
    def _consensus_affinity(
        factors: np.ndarray,
        item_norms: np.ndarray,
        seed_pairs: list[tuple[int, int]],
        *,
        positive: bool,
        topk: int,
    ) -> np.ndarray:
        """Score every item against several explicit anchors without one seed dominating.

        The max similarity preserves niche favourites; the top-k mean requires some agreement from
        multiple anchors.  Rating magnitude only provides a small multiplier, because the geometry
        of the public model should remain the main signal.
        """
        if not seed_pairs:
            return np.zeros(factors.shape[0], dtype=np.float32)

        seed_indices = np.asarray([item for item, _rating in seed_pairs], dtype=np.int32)
        seed_factors = factors[seed_indices]
        seed_norms = np.linalg.norm(seed_factors, axis=1)
        valid = seed_norms > 1e-8
        if not bool(valid.any()):
            return np.zeros(factors.shape[0], dtype=np.float32)

        seed_factors = seed_factors[valid]
        seed_norms = seed_norms[valid]
        ratings = np.asarray(
            [rating for (_item, rating), keep in zip(seed_pairs, valid) if keep],
            dtype=np.float32,
        )
        normalized = seed_factors / seed_norms[:, None]
        similarities = factors.dot(normalized.T)
        similarities = similarities / np.maximum(item_norms[:, None], 1e-8)

        if positive:
            # 10/10 receives a modest extra vote over 9/10.
            weights = 1.0 + 0.10 * np.maximum(0.0, ratings - 9.0)
        else:
            # 1/10 is a slightly stronger veto anchor than 4/10, but never a hard exclusion.
            weights = 1.0 + 0.06 * np.maximum(0.0, 4.0 - ratings)
        weighted = similarities * weights[None, :]

        k = max(1, min(int(topk), int(weighted.shape[1])))
        if k == 1:
            best = np.max(weighted, axis=1)
            consensus = best
        else:
            top = np.partition(weighted, -k, axis=1)[:, -k:]
            best = np.max(top, axis=1)
            mean_top = np.mean(top, axis=1)
            consensus = 0.62 * best + 0.38 * mean_top
        return np.asarray(consensus, dtype=np.float32)

    @classmethod
    def _contrastive_favorite_scores(
        cls,
        factors: np.ndarray,
        positive_pairs: list[tuple[int, int]],
        negative_pairs: list[tuple[int, int]],
    ) -> np.ndarray:
        item_norms = np.linalg.norm(factors, axis=1).astype(np.float32, copy=False)
        positive = cls._consensus_affinity(
            factors,
            item_norms,
            positive_pairs,
            positive=True,
            topk=cls.POSITIVE_CONSENSUS_TOPK,
        )
        if not negative_pairs:
            return np.asarray(positive, dtype=np.float64)

        negative = cls._consensus_affinity(
            factors,
            item_norms,
            negative_pairs,
            positive=False,
            topk=cls.NEGATIVE_CONSENSUS_TOPK,
        )
        # Penalize only strong negative-neighbour evidence.  The cap is intentionally small: low
        # ratings are a veto signal, not permission to erase a candidate with overwhelming positive
        # evidence from several favourites.
        threshold = float(cls.NEGATIVE_PENALTY_THRESHOLD)
        strength = np.clip((negative - threshold) / max(1e-6, 1.0 - threshold), 0.0, 1.0)
        penalty = float(cls.NEGATIVE_PENALTY_MAX) * strength
        return np.asarray(positive - penalty, dtype=np.float64)

    def status(self) -> dict:
        with self._lock:
            return dict(self._last_stats)

    def groups(self, als_limit: int = 1000, favorite_limit: int = 450) -> dict[str, list[str]]:
        """Return globally personalized IMDb candidate groups, best first.

        The method is fail-open: while ALS is warming, or if the model cannot be folded for this
        user, the caller keeps the established generic candidate path.
        """
        if not self.collaborative.is_ready():
            return {"als": [], "favorites": []}

        user_items, user_factor, mapped_ratings = self.collaborative._build_user_representation()
        if user_items is None or user_factor is None or int(mapped_ratings) < 20:
            with self._lock:
                self._last_stats = {
                    "retrieval_version": RETRIEVAL_VERSION,
                    "mapped_ratings": int(mapped_ratings),
                    "positive_anchors": 0,
                    "negative_anchors": 0,
                    "als_candidates": 0,
                    "favorite_candidates": 0,
                }
            return {"als": [], "favorites": []}

        with self.collaborative._lock:
            model = self.collaborative._model
            item_to_imdb = self.collaborative._item_to_imdb
            mapping = dict(self.collaborative._imdb_to_item)
            model_version = str(self.collaborative._version or "")
            history_token = self.collaborative._history_token
        if model is None or item_to_imdb is None:
            return {"als": [], "favorites": []}

        cache_token = (
            RETRIEVAL_VERSION,
            history_token,
            model_version,
            int(als_limit),
            int(favorite_limit),
        )
        with self._lock:
            if cache_token == self._cache_token:
                return {key: list(value) for key, value in self._cache.items()}

        factors = np.asarray(model.item_factors, dtype=np.float32)
        factor = np.asarray(user_factor, dtype=np.float32).reshape(-1)
        if factors.ndim != 2 or factor.ndim != 1 or factors.shape[1] != factor.shape[0]:
            return {"als": [], "favorites": []}

        interacted = {int(x) for x in np.asarray(user_items.indices, dtype=np.int64).tolist()}

        # 1) Global ALS retrieval across every mapped MovieLens item.
        als_raw = np.asarray(factors.dot(factor), dtype=np.float64)
        als_indices = self._top_indices(als_raw, als_limit, interacted)
        als_ids = self._indices_to_imdb(als_indices, item_to_imdb)

        # 2) Contrastive favourite retrieval.  More anchors cover more of a long rating history,
        # consensus stops a single eccentric favourite from dominating, and explicit 1-4 ratings
        # can demote candidates living very close to known dislikes.
        positive_pairs = self._anchor_seed_items(
            mapping,
            positive=True,
            limit=self.FAVORITE_SEEDS,
        )
        negative_pairs = self._anchor_seed_items(
            mapping,
            positive=False,
            limit=self.NEGATIVE_SEEDS,
        )
        favorite_indices: list[int] = []
        if positive_pairs and favorite_limit > 0:
            favorite_scores = self._contrastive_favorite_scores(
                factors,
                positive_pairs,
                negative_pairs,
            )
            excluded = interacted | set(als_indices)
            favorite_indices = self._top_indices(favorite_scores, favorite_limit, excluded)
        favorite_ids = self._indices_to_imdb(favorite_indices, item_to_imdb)

        payload = {"als": als_ids, "favorites": favorite_ids}
        stats = {
            "retrieval_version": RETRIEVAL_VERSION,
            "mapped_ratings": int(mapped_ratings),
            "positive_anchors": len(positive_pairs),
            "negative_anchors": len(negative_pairs),
            "als_candidates": len(als_ids),
            "favorite_candidates": len(favorite_ids),
        }
        with self._lock:
            self._cache_token = cache_token
            self._cache = {key: list(value) for key, value in payload.items()}
            self._last_stats = stats
        return payload
