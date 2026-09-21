from __future__ import annotations

import threading

from .recommender_v16 import FastRecommendationEngineV16


HYBRID_CALIBRATION_VERSION = "hybrid-calibration-v4.6.0"
BASELINE_ALS_WEIGHT = 0.70
_CLASS_CACHE: dict[tuple[type, float], type] = {}
_CLASS_LOCK = threading.RLock()


def allowed_als_weights() -> tuple[float, ...]:
    """Conservative challengers around the established 70/30 hybrid."""
    return (0.50, 0.60, 0.80)


def calibrated_hybrid_engine_class(base_cls: type, als_weight: float) -> type:
    """Return the exact production engine with a locally selected ALS/content balance."""
    if not isinstance(base_cls, type) or not issubclass(base_cls, FastRecommendationEngineV16):
        raise TypeError("Hybrid calibration requires a V16-compatible production engine")
    weight = round(float(als_weight), 2)
    if weight not in allowed_als_weights():
        raise ValueError(f"Unsupported ALS weight: {weight}")
    key = (base_cls, weight)
    with _CLASS_LOCK:
        cached = _CLASS_CACHE.get(key)
        if cached is not None:
            return cached

        class CalibratedHybrid(base_cls):
            ALS_WEIGHT = weight
            CONTENT_WEIGHT = 1.0 - weight
            HYBRID_CALIBRATION_VERSION = HYBRID_CALIBRATION_VERSION

            def _state_token(self):
                return super()._state_token() + (
                    HYBRID_CALIBRATION_VERSION,
                    round(float(self.ALS_WEIGHT), 2),
                )

            def _persistent_key(self, when, mode: str):
                return super()._persistent_key(when, mode) + f":hybrid{int(weight * 100):02d}"

        suffix = int(round(weight * 100))
        CalibratedHybrid.__name__ = f"{base_cls.__name__}Hybrid{suffix:02d}"
        CalibratedHybrid.__qualname__ = CalibratedHybrid.__name__
        _CLASS_CACHE[key] = CalibratedHybrid
        return CalibratedHybrid
