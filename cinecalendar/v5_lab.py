from __future__ import annotations

from datetime import date
import threading

from .availability_guard_v37 import AvailabilityGuardMixinV37
from .recommender_v16 import FastRecommendationEngineV16
from .v5_pipeline import V5RecommendationPipeline, V5_PIPELINE_VERSION
from .v5_retrieval import V5_RETRIEVAL_VERSION
from .v5_personal_ranker import V5_RANKER_VERSION


V5_LAB_ENGINE_VERSION = "v5-lab-alpha2-retrieval"
DISCOVERY_ENGINE_VERSION = "discovery-v2-sweep"


class V5LabRecommendationEngine(AvailabilityGuardMixinV37, FastRecommendationEngineV16):
    """Legacy-compatible laboratory adapter for the composition-first V5 pipeline.

    Alpha 1 exposes only V5 retrieval to the existing scorer. The new personal utility model is
    trained and reported but intentionally does not reorder results yet: the first naive blend
    failed the external replay gate, so it remains a measured component rather than another patch.
    """

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self.v5 = V5RecommendationPipeline(db, self)

    def _state_token(self) -> tuple:
        v5 = getattr(self, "v5", None)
        retrieval = getattr(v5, "retrieval", None)
        discovery = getattr(retrieval, "online_discovery", None)
        discovery_token = (
            discovery.state_token()
            if discovery is not None
            else ("v5-online-discovery:init",)
        )
        return super()._state_token() + (
            V5_LAB_ENGINE_VERSION,
            V5_PIPELINE_VERSION,
            V5_RETRIEVAL_VERSION,
            V5_RANKER_VERSION,
            discovery_token,
        )

    def _persistent_key(self, when, mode: str) -> str:
        return f"v5_lab_alpha1:{when.isoformat()}:{mode}"

    def _balanced_candidate_ids(self, when: date, limit: int) -> list[int]:
        baseline = list(AvailabilityGuardMixinV37._balanced_candidate_ids(self, when, limit))
        return self.v5.retrieval.expand(baseline, when=when)

    def candidate_generation_status(self) -> dict:
        status = dict(super().candidate_generation_status())
        status["v5"] = self.v5.status()
        return status


class DiscoveryRecommendationEngine(V5LabRecommendationEngine):
    """Production-shaped V16 scorer with conservative multi-source candidate discovery.

    It preserves every baseline candidate and changes only retrieval. Ranking/scoring remains
    the proven V16 surface. Subclasses vary only the bounded discovery frontier so replay can
    choose evidence-backed settings instead of hard-coding a guess.
    """

    DISCOVERY_VARIANT = "balanced"
    DISCOVERY_EXTRA_SHARE = 0.10
    DISCOVERY_MIN_SUPPORT = 2
    DISCOVERY_TRUSTED_SINGLE_SOURCES = ("als", "favorites")
    DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT = 40

    def _state_token(self) -> tuple:
        return super()._state_token() + (
            DISCOVERY_ENGINE_VERSION,
            self.DISCOVERY_VARIANT,
            self.DISCOVERY_EXTRA_SHARE,
            self.DISCOVERY_MIN_SUPPORT,
            self.DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT,
        )

    def _persistent_key(self, when, mode: str) -> str:
        return f"discovery_v2:{self.DISCOVERY_VARIANT}:{when.isoformat()}:{mode}"

    def _balanced_candidate_ids(self, when: date, limit: int) -> list[int]:
        baseline = list(AvailabilityGuardMixinV37._balanced_candidate_ids(self, when, limit))
        return self.v5.retrieval.expand(
            baseline,
            when=when,
            extra_share=self.DISCOVERY_EXTRA_SHARE,
            policy="consensus",
            minimum_support=self.DISCOVERY_MIN_SUPPORT,
            trusted_single_sources=self.DISCOVERY_TRUSTED_SINGLE_SOURCES,
            trusted_single_rank_limit=self.DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT,
        )

    def candidate_generation_status(self) -> dict:
        status = dict(super().candidate_generation_status())
        status["discovery"] = {
            "version": DISCOVERY_ENGINE_VERSION,
            "variant": self.DISCOVERY_VARIANT,
            "base": "V16",
            "ranking_changed": False,
            "extra_share": self.DISCOVERY_EXTRA_SHARE,
            "minimum_support": self.DISCOVERY_MIN_SUPPORT,
            "trusted_single_rank_limit": self.DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT,
            "policy": "consensus",
        }
        return status


class DiscoveryStrictRecommendationEngine(DiscoveryRecommendationEngine):
    DISCOVERY_VARIANT = "strict"
    DISCOVERY_EXTRA_SHARE = 0.06
    DISCOVERY_TRUSTED_SINGLE_SOURCES = ()
    DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT = 0


class DiscoveryWideRecommendationEngine(DiscoveryRecommendationEngine):
    DISCOVERY_VARIANT = "wide"
    DISCOVERY_EXTRA_SHARE = 0.14
    DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT = 80


_DISCOVERY_RUNTIME_CACHE: dict[tuple[type, str], type] = {}
_DISCOVERY_RUNTIME_LOCK = threading.RLock()


def discovery_engine_class(base_cls: type, variant: str = "balanced") -> type:
    """Apply the Discovery retrieval frontier over the exact Stable taste engine class."""
    if not isinstance(base_cls, type) or not issubclass(base_cls, FastRecommendationEngineV16):
        raise TypeError("Discovery requires a V16-compatible Stable engine")

    name = str(variant or "balanced").strip().lower()
    config = {
        "strict": {
            "extra_share": 0.06,
            "minimum_support": 2,
            "trusted_sources": (),
            "trusted_rank": 0,
        },
        "balanced": {
            "extra_share": 0.10,
            "minimum_support": 2,
            "trusted_sources": ("als", "favorites"),
            "trusted_rank": 40,
        },
        "wide": {
            "extra_share": 0.14,
            "minimum_support": 2,
            "trusted_sources": ("als", "favorites"),
            "trusted_rank": 80,
        },
    }
    if name not in config:
        name = "balanced"
    key = (base_cls, name)
    with _DISCOVERY_RUNTIME_LOCK:
        cached = _DISCOVERY_RUNTIME_CACHE.get(key)
        if cached is not None:
            return cached
        cfg = dict(config[name])

        class RuntimeDiscovery(base_cls):
            DISCOVERY_VARIANT = name
            DISCOVERY_EXTRA_SHARE = float(cfg["extra_share"])
            DISCOVERY_MIN_SUPPORT = int(cfg["minimum_support"])
            DISCOVERY_TRUSTED_SINGLE_SOURCES = tuple(cfg["trusted_sources"])
            DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT = int(cfg["trusted_rank"])

            def __init__(self, db, calendar=None):
                super().__init__(db, calendar)
                self.v5 = V5RecommendationPipeline(db, self)

            def _state_token(self) -> tuple:
                return super()._state_token() + (
                    DISCOVERY_ENGINE_VERSION,
                    self.DISCOVERY_VARIANT,
                    self.DISCOVERY_EXTRA_SHARE,
                    self.DISCOVERY_MIN_SUPPORT,
                    self.DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT,
                )

            def _persistent_key(self, when, mode: str) -> str:
                return (
                    super()._persistent_key(when, mode)
                    + f":discovery-{self.DISCOVERY_VARIANT}"
                )

            def _balanced_candidate_ids(self, when: date, limit: int) -> list[int]:
                baseline = list(super()._balanced_candidate_ids(when, limit))
                return self.v5.retrieval.expand(
                    baseline,
                    when=when,
                    extra_share=self.DISCOVERY_EXTRA_SHARE,
                    policy="consensus",
                    minimum_support=self.DISCOVERY_MIN_SUPPORT,
                    trusted_single_sources=self.DISCOVERY_TRUSTED_SINGLE_SOURCES,
                    trusted_single_rank_limit=self.DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT,
                )

            def candidate_generation_status(self) -> dict:
                status = dict(super().candidate_generation_status())
                status["discovery"] = {
                    "version": DISCOVERY_ENGINE_VERSION,
                    "variant": self.DISCOVERY_VARIANT,
                    "base": base_cls.__name__,
                    "ranking_changed": False,
                    "extra_share": self.DISCOVERY_EXTRA_SHARE,
                    "minimum_support": self.DISCOVERY_MIN_SUPPORT,
                    "trusted_single_rank_limit": self.DISCOVERY_TRUSTED_SINGLE_RANK_LIMIT,
                    "policy": "consensus",
                }
                return status

        RuntimeDiscovery.__name__ = (
            f"{base_cls.__name__}Discovery{name.title()}"
        )
        RuntimeDiscovery.__qualname__ = RuntimeDiscovery.__name__
        _DISCOVERY_RUNTIME_CACHE[key] = RuntimeDiscovery
        return RuntimeDiscovery
