from __future__ import annotations

from datetime import date
import math

from .models import Recommendation
from .recommender_v12 import FastRecommendationEngineV12
from .recommender_v15 import FastRecommendationEngineV15
from .trust_audit import ensure_trust_audit_schema, record_trust_snapshot
from .util import clamp, utcnow_iso


ENGINE_VERSION = "16.0.0-top3-trust-gate"


class FastRecommendationEngineV16(FastRecommendationEngineV15):
    """V15 + conservative trust gate for the small visible recommendation set.

    The gate is intentionally late in the pipeline. Retrieval, ALS/content scoring, adaptive
    preferences and Watch Success first build a broad finalist pool. Only then do we decide which
    1-9 titles are trustworthy enough to expose. The gate never rescues a clearly weaker movie and
    never rewrites the estimated 1-10 rating; it only chooses among already-competitive finalists.
    """

    QUALITY_GATE_MAX_VISIBLE = 9
    QUALITY_GATE_POOL_MIN = 18
    QUALITY_GATE_POOL_MAX = 36
    TRUST_RELATIVE_MARGIN = 0.14
    TRUST_MIN_SCORE = 0.60
    TRUST_MIN_SUPPORTS = 3

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self._quality_gate_stats = {
            "pool": 0,
            "trusted": 0,
            "red_flags": 0,
            "fallback": 0,
            "returned": 0,
            "bypassed": False,
        }

    def _state_token(self) -> tuple:
        return super()._state_token() + (ENGINE_VERSION,)

    def _persistent_key(self, when, mode: str) -> str:
        return f"decision_pool_v16:{when.isoformat()}:{mode}"

    @staticmethod
    def _public_bayesian_rating(rec: Recommendation) -> float | None:
        movie = rec.movie
        if movie.imdb_rating is None:
            return None
        votes = max(0, int(movie.num_votes or 0))
        prior_votes = 2500.0
        prior = 6.5
        return (votes / (votes + prior_votes)) * float(movie.imdb_rating) + (
            prior_votes / (votes + prior_votes)
        ) * prior

    @staticmethod
    def _evidence_norm(rec: Recommendation) -> float:
        evidence = max(0.0, float(rec.score.evidence or 0.0))
        return clamp(1.0 - math.exp(-evidence / 2.5))

    @staticmethod
    def _red_flag(rec: Recommendation) -> tuple[bool, str]:
        predicted = float(rec.score.predicted_rating or 0.0)
        confidence = clamp(float(rec.score.confidence or 0.0))
        movie = rec.movie
        votes = max(0, int(movie.num_votes or 0))
        public = float(movie.imdb_rating or 0.0)

        if confidence >= 0.72 and predicted > 0 and predicted < 5.8:
            return True, "modelul personal are încredere mare într-o estimare sub 5.8/10"
        if votes >= 5000 and public > 0 and public <= 5.2:
            return True, "ratingul public este foarte slab pe un eșantion mare"
        return False, ""

    def _als_scores(self, recs: list[Recommendation]) -> dict[str, float]:
        provider = self.collaborative
        if not provider.is_ready():
            return {}
        imdb_ids = [str(rec.movie.imdb_id or "") for rec in recs if rec.movie.imdb_id]
        if not imdb_ids:
            return {}
        normalized, _raw, mapped_ratings = provider.score_candidates(imdb_ids)
        if int(mapped_ratings) < 20:
            return {}
        return {str(key): float(value) for key, value in normalized.items()}

    def _trust_payload(
        self,
        rec: Recommendation,
        *,
        best_final: float,
        als_score: float | None,
    ) -> dict:
        score = rec.score
        movie = rec.movie
        final = clamp(float(score.final))
        predicted = float(score.predicted_rating or 0.0)
        confidence = clamp(float(score.confidence or 0.0))
        evidence = self._evidence_norm(rec)
        startability = clamp(float(score.startability or 0.5))
        public_bayes = self._public_bayesian_rating(rec)

        predicted_norm = clamp((predicted - 5.5) / 3.5) if predicted > 0 else 0.45
        public_norm = clamp((public_bayes - 5.5) / 2.5) if public_bayes is not None else 0.50
        als = clamp(float(als_score)) if als_score is not None else 0.50

        trust = clamp(
            0.34 * final
            + 0.18 * predicted_norm
            + 0.15 * confidence
            + 0.08 * evidence
            + 0.12 * als
            + 0.08 * public_norm
            + 0.05 * startability
        )

        supports: list[str] = []
        if predicted >= 6.8:
            supports.append("rating personal")
        if confidence >= 0.55:
            supports.append("încredere personală")
        if evidence >= 0.45:
            supports.append("istoric suficient")
        if als_score is not None and als >= 0.80:
            supports.append("ALS")
        if public_bayes is not None and int(movie.num_votes or 0) >= 250 and public_bayes >= 6.5:
            supports.append("calitate publică")
        if startability >= 0.60:
            supports.append("ușurință de pornire")

        red_flag, red_reason = self._red_flag(rec)
        gap = max(0.0, float(best_final) - final)
        trusted = (
            not red_flag
            and gap <= self.TRUST_RELATIVE_MARGIN
            and trust >= self.TRUST_MIN_SCORE
            and len(supports) >= self.TRUST_MIN_SUPPORTS
        )

        # Trust only breaks close decisions. The maximum ordering nudge is +/-0.03 score points.
        gate_score = clamp(final + 0.06 * (trust - 0.5))
        return {
            "trust": trust,
            "gate_score": gate_score,
            "supports": supports,
            "trusted": trusted,
            "red_flag": red_flag,
            "red_reason": red_reason,
            "gap": gap,
            "als": als_score,
            "public_bayes": public_bayes,
        }

    def _quality_gate(self, recs: list[Recommendation], count: int) -> list[Recommendation]:
        recs = list(recs)
        requested = max(1, int(count))
        if not recs:
            self._quality_gate_stats = {
                "pool": 0,
                "trusted": 0,
                "red_flags": 0,
                "fallback": 0,
                "returned": 0,
                "bypassed": False,
            }
            return []

        best_final = max(float(rec.score.final) for rec in recs)
        als_scores = self._als_scores(recs)
        evaluated: list[tuple[Recommendation, dict]] = []
        for rec in recs:
            payload = self._trust_payload(
                rec,
                best_final=best_final,
                als_score=als_scores.get(str(rec.movie.imdb_id or "")),
            )
            evaluated.append((rec, payload))

        def order_key(item: tuple[Recommendation, dict]):
            rec, payload = item
            return (
                float(payload["gate_score"]),
                float(rec.score.final),
                float(payload["trust"]),
                float(rec.score.predicted_rating),
                float(rec.score.confidence),
            )

        trusted = sorted((item for item in evaluated if item[1]["trusted"]), key=order_key, reverse=True)
        safe_fallback = sorted(
            (item for item in evaluated if not item[1]["trusted"] and not item[1]["red_flag"]),
            key=order_key,
            reverse=True,
        )
        red_flags = sorted((item for item in evaluated if item[1]["red_flag"]), key=order_key, reverse=True)

        chosen: list[tuple[Recommendation, dict]] = []
        seen: set[int] = set()
        for group in (trusted, safe_fallback, red_flags):
            for item in group:
                rec = item[0]
                mid = int(rec.movie.id or 0)
                if mid in seen:
                    continue
                seen.add(mid)
                chosen.append(item)
                if len(chosen) >= requested:
                    break
            if len(chosen) >= requested:
                break

        trusted_ids = {id(item[0]) for item in trusted}
        for rec, payload in chosen:
            supports = list(payload["supports"])
            if payload["red_flag"]:
                status = "red_flag"
                reason = "Rezervă de ultimă instanță: " + str(payload["red_reason"] or "semnale insuficiente") + "."
            elif id(rec) in trusted_ids:
                status = "trusted"
                reason = (
                    f"Poartă Top 3 trecută: {len(supports)} semnale independente"
                    + (f" ({', '.join(supports[:4])})" if supports else "")
                    + "."
                )
            else:
                status = "backfill"
                reason = (
                    "Backfill conservator: film competitiv, dar nu are încă suficiente semnale independente "
                    "pentru statutul de recomandare cu încredere ridicată."
                )
            audit_payload = dict(payload)
            audit_payload["status"] = status
            rec.score.trust_audit = audit_payload
            rec.score.contributions.insert(0, ("Poartă de încredere Top 3", 0.0, reason))
            if reason not in (rec.score.personal_reason or ""):
                rec.score.personal_reason = reason + " " + (rec.score.personal_reason or "")

        trusted_returned = sum(1 for rec, _payload in chosen if id(rec) in trusted_ids)
        self._quality_gate_stats = {
            "pool": len(recs),
            "trusted": len(trusted),
            "red_flags": len(red_flags),
            "fallback": max(0, len(chosen) - trusted_returned),
            "returned": len(chosen),
            "bypassed": False,
        }
        return [rec for rec, _payload in chosen]

    def quality_gate_status(self) -> dict:
        return dict(self._quality_gate_stats)

    def _adaptive_rerank(self, recs: list[Recommendation], count: int) -> list[Recommendation]:
        requested = max(1, int(count))
        # Large result lists are diagnostics/browsing surfaces, not a Top-3 decision. Preserve the
        # established ranking there so backtests can still inspect 25/50/100 positions.
        if requested > self.QUALITY_GATE_MAX_VISIBLE:
            selected = super()._adaptive_rerank(recs, requested)
            for rec in selected:
                rec.score.trust_audit = {
                    "status": "bypassed",
                    "trust": None,
                    "gate_score": None,
                    "supports": [],
                    "trusted": False,
                    "red_flag": False,
                    "red_reason": "",
                    "gap": None,
                    "als": None,
                    "public_bayes": None,
                }
            self._quality_gate_stats = {
                "pool": len(recs),
                "trusted": 0,
                "red_flags": 0,
                "fallback": 0,
                "returned": len(selected),
                "bypassed": True,
            }
            return selected

        gate_pool_size = min(
            len(recs),
            self.QUALITY_GATE_POOL_MAX,
            max(self.QUALITY_GATE_POOL_MIN, requested * 8),
        )
        mature_pool = super()._adaptive_rerank(recs, gate_pool_size)
        return self._quality_gate(mature_pool, requested)

    def _record_selected(self, selected, when: date, slot: str, candidate_count: int) -> None:
        """Persist the real V16 engine version and the trust decision behind every visible result."""
        if not selected:
            return
        now = utcnow_iso()
        with self.db.tx() as con:
            ensure_trust_audit_schema(con)
            run = con.execute(
                """INSERT INTO recommendation_runs(
                       context_date,slot,generated_at,candidate_count,result_count,engine_version
                   ) VALUES(?,?,?,?,?,?)""",
                (when.isoformat(), slot, now, int(candidate_count), len(selected), ENGINE_VERSION),
            )
            run_id = int(run.lastrowid)
            for rank_position, rec in enumerate(selected, start=1):
                history = con.execute(
                    """INSERT INTO recommendation_history(
                           movie_id,recommended_at,context_date,slot,final_score
                       ) VALUES(?,?,?,?,?)""",
                    (rec.movie.id, now, when.isoformat(), slot, rec.score.final),
                )
                payload = getattr(rec.score, "trust_audit", None)
                record_trust_snapshot(
                    con,
                    history_id=int(history.lastrowid),
                    run_id=run_id,
                    movie_id=int(rec.movie.id),
                    context_date=when.isoformat(),
                    slot=slot,
                    rank_position=rank_position,
                    engine_version=ENGINE_VERSION,
                    payload=payload if isinstance(payload, dict) else {"status": "unclassified"},
                    created_at=now,
                )

    def recommend(self, when: date | None = None, count: int = 3, exclude_ids: set[int] | None = None,
                  record: bool = False, slot: str = "today", candidate_limit: int = 100000,
                  mode: str = "decide", runtime_max: int | None = None, runtime_min: int | None = None):
        """Run the established V13-V16 pipeline, but record V16 rather than V13 telemetry."""
        when = when or date.today()
        requested = max(1, int(count))
        expanded = min(
            self.ADAPTIVE_POOL_MAX,
            max(self.ADAPTIVE_POOL_MIN, requested * 12),
        )
        # This is deliberately the same base call used by V13's recommend(). Calling V13's public
        # method directly would make it own the persistence step and stamp the old V13 module
        # constant into recommendation_runs.
        base = FastRecommendationEngineV12.recommend(
            self,
            when=when,
            count=expanded,
            exclude_ids=exclude_ids,
            record=False,
            slot=slot,
            candidate_limit=candidate_limit,
            mode=mode,
            runtime_max=runtime_max,
            runtime_min=runtime_min,
        )
        selected = self._adaptive_rerank(list(base), requested)
        self._annotate_final_als(selected)
        if record and selected:
            self._record_selected(selected, when, slot, len(base))
        return selected
