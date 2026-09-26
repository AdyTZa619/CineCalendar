from __future__ import annotations

import threading

from .recommender_v16 import FastRecommendationEngineV16


AVAILABILITY_VERSION = "availability-guard-v3.7.0"
_CLASS_CACHE: dict[type, type] = {}
_CLASS_LOCK = threading.RLock()


class AvailabilityGuardMixinV37:
    """Keep known future releases out of candidate discovery.

    The guard does not score or reorder eligible films. It oversamples the existing candidate
    order slightly, removes titles whose known year/release date is after the requested day, then
    returns the original order truncated to the requested limit. Unknown release dates remain
    eligible so incomplete metadata cannot empty the pool.
    """

    AVAILABILITY_OVERSAMPLE = 1.15
    AVAILABILITY_MIN_EXTRA = 120

    def _state_token(self) -> tuple:
        return super()._state_token() + (AVAILABILITY_VERSION,)

    def _persistent_key(self, when, mode: str) -> str:
        return super()._persistent_key(when, mode) + ":avail37"

    def _balanced_candidate_ids(self, when, limit: int) -> list[int]:
        requested = max(1, int(limit))
        extra = max(self.AVAILABILITY_MIN_EXTRA, int(round(requested * (self.AVAILABILITY_OVERSAMPLE - 1.0))))
        raw = list(super()._balanced_candidate_ids(when, requested + extra))
        if not raw:
            return []

        metadata: dict[int, tuple[int | None, str]] = {}
        with self.db.connect() as con:
            for start in range(0, len(raw), 700):
                chunk = raw[start:start + 700]
                marks = ",".join("?" for _ in chunk)
                rows = con.execute(
                    f"SELECT id,year,COALESCE(release_date,'') AS release_date FROM movies WHERE id IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    metadata[int(row["id"])] = (
                        int(row["year"]) if row["year"] is not None else None,
                        str(row["release_date"] or "")[:10],
                    )

        cutoff = when.isoformat()
        out: list[int] = []
        seen: set[int] = set()
        for raw_id in raw:
            mid = int(raw_id)
            if mid in seen:
                continue
            seen.add(mid)
            year, release = metadata.get(mid, (None, ""))
            if year is not None and year > when.year:
                continue
            if release and len(release) >= 10 and release > cutoff:
                continue
            out.append(mid)
            if len(out) >= requested:
                break
        return out


def availability_engine_class(base_cls: type) -> type:
    if not issubclass(base_cls, FastRecommendationEngineV16):
        raise TypeError("availability guard requires a V16-compatible engine")
    if issubclass(base_cls, AvailabilityGuardMixinV37):
        return base_cls
    with _CLASS_LOCK:
        cached = _CLASS_CACHE.get(base_cls)
        if cached is not None:
            return cached
        name = f"{base_cls.__name__}Availability37"
        cls = type(
            name,
            (AvailabilityGuardMixinV37, base_cls),
            {
                "__module__": __name__,
                "__doc__": f"3.7 availability guard preserving {base_cls.__name__} ranking behavior.",
            },
        )
        _CLASS_CACHE[base_cls] = cls
        return cls
