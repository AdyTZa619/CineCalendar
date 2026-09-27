from __future__ import annotations

from datetime import date
import uuid

from .util import utcnow_iso
from .v5_shadow_ranker import V5ShadowRankedEngine20


V5_VISIBLE_TRIAL_VERSION = "v5-visible-trial-alpha1"
MODE_V16 = "v16"
MODE_V5_20 = "v5_20"


class V5VisibleTrialEngine20(V5ShadowRankedEngine20):
    """The 20% V5 ranker variant approved only for an isolated Alpha trial."""

    V5_VISIBLE_TRIAL = True

    def _adaptive_rerank(self, recs, count: int):
        selected = list(super()._adaptive_rerank(recs, count))
        for rec in selected:
            factors = getattr(rec.score, "score_factors", {}) or {}
            if "v5_shadow_blend_weight" not in factors:
                continue
            rec.score.contributions = [
                (
                    "V5 trial utility" if name == "V5 shadow utility" else name,
                    points,
                    (
                        "Trial Alpha: P(8+) minus riscul P(1–4), cu pondere 20%. "
                        "Stable nu este modificat."
                        if name == "V5 shadow utility" else reason
                    ),
                )
                for name, points, reason in rec.score.contributions
            ]
            factors["v5_visible_trial"] = 1.0
            rec.score.score_factors = factors
        return selected

    def candidate_generation_status(self) -> dict:
        status = dict(super().candidate_generation_status())
        status["v5_visible_trial"] = {
            "version": V5_VISIBLE_TRIAL_VERSION,
            "blend_weight": 0.20,
            "visible_ranking_changed": True,
            "alpha_only": True,
        }
        return status


class AlphaTrialRecommender:
    """Instant V16/V5 switch with side-by-side audit for visible Alpha recommendations."""

    def __init__(self, db, v16_engine, v5_engine):
        self.db = db
        self.v16 = v16_engine
        self.v5 = v5_engine
        self._ensure_audit_table()
        saved = str(self.db.get_setting("v5_visible_trial_mode", "") or "")
        if saved not in {MODE_V16, MODE_V5_20}:
            saved = MODE_V5_20 if self._eligible() else MODE_V16
            self.db.set_setting("v5_visible_trial_mode", saved)
        if saved == MODE_V5_20 and not self._eligible():
            saved = MODE_V16
            self.db.set_setting("v5_visible_trial_mode", saved)
        self._mode = saved

    def _ensure_audit_table(self) -> None:
        with self.db.tx() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS v5_trial_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    context_date TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    active_mode TEXT NOT NULL,
                    movie_id INTEGER NOT NULL,
                    imdb_id TEXT,
                    active_rank INTEGER,
                    v16_rank INTEGER,
                    v5_rank INTEGER,
                    v16_final REAL,
                    v5_final REAL,
                    v5_delta REAL
                )"""
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_v5_trial_audit_date ON v5_trial_audit(context_date,generated_at)"
            )

    def _eligible(self) -> bool:
        report = self.db.get_setting("v5_evaluation_report", {}) or {}
        if not isinstance(report, dict):
            return False
        decision = report.get("decision") or {}
        return bool(
            isinstance(decision, dict)
            and decision.get("eligible_for_visible_alpha_trial")
            and str(decision.get("selected_variant") or "") == "20%"
        )

    @property
    def mode(self) -> str:
        if self._mode == MODE_V5_20 and not self._eligible():
            self._mode = MODE_V16
            self.db.set_setting("v5_visible_trial_mode", MODE_V16)
        return self._mode

    @property
    def active(self):
        return self.v5 if self.mode == MODE_V5_20 else self.v16

    @property
    def other(self):
        return self.v16 if self.mode == MODE_V5_20 else self.v5

    def set_mode(self, mode: str) -> dict:
        wanted = str(mode or "").strip().lower()
        if wanted not in {MODE_V16, MODE_V5_20}:
            raise ValueError("Mod de trial necunoscut.")
        if wanted == MODE_V5_20 and not self._eligible():
            raise RuntimeError("V5 20% nu are un raport eligibil pentru trialul vizibil.")
        self._mode = wanted
        self.db.set_setting("v5_visible_trial_mode", wanted)
        self.db.set_setting("v5_visible_trial_changed_at", utcnow_iso())
        return self.trial_status()

    def trial_status(self) -> dict:
        report = self.db.get_setting("v5_evaluation_report", {}) or {}
        decision = report.get("decision") if isinstance(report, dict) else {}
        return {
            "version": V5_VISIBLE_TRIAL_VERSION,
            "mode": self.mode,
            "eligible": self._eligible(),
            "selected_variant": str((decision or {}).get("selected_variant") or ""),
            "blend_weight": 0.20 if self.mode == MODE_V5_20 else 0.0,
            "dual_audit": True,
            "stable_untouched": True,
        }

    def _annotate_and_persist(self, active_recs, v16_recs, v5_recs, *, when, slot: str) -> None:
        v16_by_id = {
            int(rec.movie.id): (idx, rec)
            for idx, rec in enumerate(v16_recs, start=1)
            if rec.movie.id is not None
        }
        v5_by_id = {
            int(rec.movie.id): (idx, rec)
            for idx, rec in enumerate(v5_recs, start=1)
            if rec.movie.id is not None
        }
        active_mode = self.mode
        round_id = uuid.uuid4().hex
        generated_at = utcnow_iso()
        context_date = (when or date.today()).isoformat()

        rows = []
        for active_rank, rec in enumerate(active_recs, start=1):
            mid = int(rec.movie.id or 0)
            v16_item = v16_by_id.get(mid)
            v5_item = v5_by_id.get(mid)
            v16_rank = int(v16_item[0]) if v16_item else -1
            v5_rank = int(v5_item[0]) if v5_item else -1
            v16_final = float(v16_item[1].score.final) if v16_item else None
            v5_final = float(v5_item[1].score.final) if v5_item else None
            delta = (
                float(v5_final - v16_final)
                if v16_final is not None and v5_final is not None
                else None
            )

            factors = dict(getattr(rec.score, "score_factors", {}) or {})
            factors["v5_trial_active"] = 1.0 if active_mode == MODE_V5_20 else 0.0
            factors["v5_trial_v16_rank"] = float(v16_rank)
            factors["v5_trial_v5_rank"] = float(v5_rank)
            if v16_final is not None:
                factors["v5_trial_v16_final"] = v16_final
            if v5_final is not None:
                factors["v5_trial_v5_final"] = v5_final
            if delta is not None:
                factors["v5_trial_delta"] = delta
            rec.score.score_factors = factors

            rows.append(
                (
                    round_id,
                    generated_at,
                    context_date,
                    str(slot or "browse"),
                    active_mode,
                    mid,
                    str(rec.movie.imdb_id or ""),
                    int(active_rank),
                    v16_rank,
                    v5_rank,
                    v16_final,
                    v5_final,
                    delta,
                )
            )

        if rows:
            with self.db.tx() as con:
                con.executemany(
                    """INSERT INTO v5_trial_audit(
                        round_id,generated_at,context_date,slot,active_mode,movie_id,imdb_id,
                        active_rank,v16_rank,v5_rank,v16_final,v5_final,v5_delta
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
                # Keep the Alpha DB bounded. 2,000 visible audit rows are enough for the trial.
                con.execute(
                    """DELETE FROM v5_trial_audit
                       WHERE id NOT IN (
                           SELECT id FROM v5_trial_audit ORDER BY id DESC LIMIT 2000
                       )"""
                )

    def recommend(
        self, when=None, count=3, exclude_ids=None, record=False, slot="today",
        candidate_limit=100000, mode="decide", runtime_max=None, runtime_min=None,
    ):
        when = when or date.today()
        kwargs = dict(
            when=when,
            count=count,
            exclude_ids=exclude_ids,
            record=False,
            slot=slot,
            candidate_limit=candidate_limit,
            mode=mode,
            runtime_max=runtime_max,
            runtime_min=runtime_min,
        )
        v16_recs = list(self.v16.recommend(**kwargs))
        v5_recs = list(self.v5.recommend(**kwargs))
        active_recs = v5_recs if self.mode == MODE_V5_20 else v16_recs
        self._annotate_and_persist(
            active_recs, v16_recs, v5_recs, when=when, slot=slot
        )
        if record and active_recs:
            recorder = getattr(self.active, "_record_selected", None)
            if callable(recorder):
                recorder(active_recs, when, slot, len(active_recs))
        return active_recs

    def decision_pick(self, when=None, exclude_ids=None, mode="decide", **kwargs):
        when = when or date.today()
        v16_primary, v16_backups = self.v16.decision_pick(
            when, exclude_ids, mode, **kwargs
        )
        v5_primary, v5_backups = self.v5.decision_pick(
            when, exclude_ids, mode, **kwargs
        )
        v16_recs = ([v16_primary] if v16_primary else []) + list(v16_backups or [])
        v5_recs = ([v5_primary] if v5_primary else []) + list(v5_backups or [])
        active_recs = v5_recs if self.mode == MODE_V5_20 else v16_recs
        self._annotate_and_persist(
            active_recs, v16_recs, v5_recs, when=when, slot="decision"
        )
        return (
            active_recs[0] if active_recs else None,
            active_recs[1:3] if len(active_recs) > 1 else [],
        )

    def __getattr__(self, name):
        # All non-ranking APIs (calendar, status, feedback helpers, etc.) follow the currently
        # selected engine so the UI can switch instantly without rebuilding the service.
        return getattr(self.active, name)
