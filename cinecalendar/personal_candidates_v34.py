from __future__ import annotations

import numpy as np

from .personal_candidates import PersonalCandidateGenerator


RETRIEVAL_VERSION = "personal-candidates-v3-liked-consensus"


class PersonalCandidateGeneratorV34(PersonalCandidateGenerator):
    """Broader contrastive retrieval for quality evaluation in CineCalendar 3.4.

    V2 used only explicit 9/10-10/10 films as positive anchors. That is intentionally precise,
    but for users with a large history it can under-represent stable 8/10 taste clusters. V3 keeps
    the same negative safeguards and global ALS lane, while allowing 8/10 titles to contribute at
    a lower weight. The challenger is enabled in production only if the local temporal backtest
    beats V16 without a material recall/dislike regression.
    """

    FAVORITE_SEEDS = 96
    NEGATIVE_SEEDS = 64
    SEED_QUERY_LIMIT = 256
    POSITIVE_CONSENSUS_TOPK = 4
    NEGATIVE_CONSENSUS_TOPK = 2

    def _anchor_seed_items(
        self,
        mapping: dict[str, int],
        *,
        positive: bool,
        limit: int,
    ) -> list[tuple[int, int]]:
        if limit <= 0:
            return []
        condition = "r.rating>=8" if positive else "r.rating<=4"
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
            if positive and rating < 8:
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
    def _consensus_affinity(
        factors: np.ndarray,
        item_norms: np.ndarray,
        seed_pairs: list[tuple[int, int]],
        *,
        positive: bool,
        topk: int,
    ) -> np.ndarray:
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
            # 8/10 broadens recall without being treated as equal to an elite 9/10-10/10 anchor.
            weights = np.where(ratings >= 10, 1.14, np.where(ratings >= 9, 1.0, 0.78))
        else:
            weights = 1.0 + 0.06 * np.maximum(0.0, 4.0 - ratings)
        weighted = similarities * weights[None, :]

        k = max(1, min(int(topk), int(weighted.shape[1])))
        if k == 1:
            consensus = np.max(weighted, axis=1)
        else:
            top = np.partition(weighted, -k, axis=1)[:, -k:]
            best = np.max(top, axis=1)
            mean_top = np.mean(top, axis=1)
            # Keep niche favourites alive while rewarding repeated support from distinct liked films.
            consensus = 0.58 * best + 0.42 * mean_top
        return np.asarray(consensus, dtype=np.float32)

    def status(self) -> dict:
        payload = super().status()
        payload["retrieval_version"] = RETRIEVAL_VERSION
        payload["positive_anchor_floor"] = 8
        return payload

    def groups(self, als_limit: int = 1000, favorite_limit: int = 450) -> dict[str, list[str]]:
        # A V34 generator instance has its own cache, so the parent's cache mechanics remain safe.
        # Engine V17's state token carries the challenger version; only the reported retrieval label
        # needs to differ from V2 here.
        payload = super().groups(als_limit=als_limit, favorite_limit=favorite_limit)
        with self._lock:
            self._last_stats["retrieval_version"] = RETRIEVAL_VERSION
            self._last_stats["positive_anchor_floor"] = 8
        return payload
