from __future__ import annotations

from datetime import date, timedelta
import uuid

from .util import utcnow_iso
from .v5_rating_snapshot import report_rating_freshness
from .v5_shadow_ranker import V5ShadowRankedEngine20


V5_VISIBLE_TRIAL_VERSION = "v5-visible-trial-alpha4"
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
        # A trial comparison must keep the exact same V16/V5 pair while the user
        # toggles between models. Fresh pairs are created only after an explicit
        # invalidation or a meaningful user-state/request change.
        self._round_cache: dict[str, dict] = {}
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
            and decision.get("visible_decision_guard_passed")
            and str(decision.get("selected_variant") or "") == "20%"
            and report_rating_freshness(self.db, report) is True
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

    def invalidate_round(self, slot: str | None = None) -> None:
        """Forget the frozen comparison pair.

        The UI calls this for an explicit Recalculează. User-state changes are also
        detected by the request key, so a rating/feedback/watchlist change cannot
        accidentally reuse an old pair.
        """
        if slot is None:
            self._round_cache.clear()
            return
        key = str(slot or "")
        self._round_cache.pop(key, None)

    def _user_state_token(self) -> tuple:
        try:
            with self.db.connect() as con:
                ratings = con.execute(
                    "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings"
                ).fetchone()
                feedback = con.execute(
                    "SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback"
                ).fetchone()
                watchlist = con.execute(
                    "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM watchlist"
                ).fetchone()
                profile = con.execute(
                    "SELECT COALESCE(MAX(updated_at),'') FROM user_profile"
                ).fetchone()
            return (
                int(ratings[0]), str(ratings[1]),
                int(feedback[0]), str(feedback[1]),
                int(watchlist[0]), str(watchlist[1]),
                str(profile[0]),
            )
        except Exception:
            return ()

    def _recent_decision_exclusions(self, when: date) -> set[int]:
        """Avoid recent exposures and remember today's explicit skips across restarts."""
        from_date = (when - timedelta(days=7)).isoformat()
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT DISTINCT h.movie_id
                   FROM recommendation_history h
                   WHERE (h.context_date>=? AND h.context_date<?
                          AND h.slot IN ('decision','today')
                          AND NOT EXISTS (
                              SELECT 1 FROM watchlist w WHERE w.movie_id=h.movie_id
                          ))
                      OR (h.context_date=? AND h.action='skip_today')""",
                (from_date, when.isoformat(), when.isoformat()),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def _request_key(
        self, *, when, count: int, exclude_ids, slot: str, candidate_limit: int,
        mode: str, runtime_max, runtime_min,
    ) -> tuple:
        context_date = when or date.today()
        genre_setting = self.db.get_setting("daily_genre_filter", {})
        active_genre = (
            str(genre_setting.get("genre") or "").strip()
            if isinstance(genre_setting, dict)
            and str(genre_setting.get("date") or "") == context_date.isoformat()
            else ""
        )
        return (
            context_date.isoformat(),
            int(count),
            tuple(sorted(int(x) for x in (exclude_ids or set()))),
            str(slot or ""),
            int(candidate_limit),
            str(mode or ""),
            None if runtime_max is None else int(runtime_max),
            None if runtime_min is None else int(runtime_min),
            active_genre,
            self._user_state_token(),
        )

    def trial_status(self) -> dict:
        report = self.db.get_setting("v5_evaluation_report", {}) or {}
        decision = report.get("decision") if isinstance(report, dict) else {}
        return {
            "version": V5_VISIBLE_TRIAL_VERSION,
            "mode": self.mode,
            "eligible": self._eligible(),
            "report_current": report_rating_freshness(self.db, report),
            "report_generated_at": str(report.get("generated_at") or "") if isinstance(report, dict) else "",
            "selected_variant": str((decision or {}).get("selected_variant") or ""),
            "blend_weight": 0.20 if self.mode == MODE_V5_20 else 0.0,
            "dual_audit": True,
            "stable_untouched": True,
        }

    def _annotate_and_persist(
        self, active_recs, v16_recs, v5_recs, *, when, slot: str,
        round_id: str | None = None, generated_at: str | None = None,
    ) -> None:
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
        round_id = str(round_id or uuid.uuid4().hex)
        generated_at = str(generated_at or utcnow_iso())
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
        preview=False,
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
        slot_key = str(slot or "today")
        request_key = self._request_key(
            when=when, count=count, exclude_ids=exclude_ids, slot=slot_key,
            candidate_limit=candidate_limit, mode=mode,
            runtime_max=runtime_max, runtime_min=runtime_min,
        )
        cached = self._round_cache.get(slot_key)
        reuse = isinstance(cached, dict) and cached.get("request_key") == request_key
        if reuse:
            v16_recs = list(cached.get("v16") or [])
            v5_recs = list(cached.get("v5") or [])
            round_id = str(cached.get("round_id") or uuid.uuid4().hex)
            generated_at = str(cached.get("generated_at") or utcnow_iso())
        else:
            v16_recs = list(self.v16.recommend(**kwargs))
            v5_recs = list(self.v5.recommend(**kwargs))
            round_id = uuid.uuid4().hex
            generated_at = utcnow_iso()
            self._round_cache[slot_key] = {
                "request_key": request_key,
                "v16": list(v16_recs),
                "v5": list(v5_recs),
                "round_id": round_id,
                "generated_at": generated_at,
            }
        active_recs = v5_recs if self.mode == MODE_V5_20 else v16_recs
        # The metadata preflight needs a candidate pool, but only the final, visible round
        # belongs in the trial audit. Keep the pair frozen for the publishing call.
        if not preview:
            self._annotate_and_persist(
                active_recs, v16_recs, v5_recs, when=when, slot=slot_key,
                round_id=round_id, generated_at=generated_at,
            )
        if record and active_recs and not preview:
            recorder = getattr(self.active, "_record_selected", None)
            if callable(recorder):
                recorder(active_recs, when, slot, len(active_recs))
        return active_recs

    def preview_recommend(self, **kwargs):
        """Compute a frozen candidate pair without recording an unshown Alpha round."""
        return self.recommend(**kwargs, preview=True)

    def recommend_romanian(self, when=None, count=9):
        """Run the Romanian lane as the same frozen V16/V5 comparison used elsewhere."""
        when = when or date.today()
        slot_key = "romanian"
        request_key = (
            when.isoformat(),
            int(count),
            self._user_state_token(),
        )
        cached = self._round_cache.get(slot_key)
        reuse = isinstance(cached, dict) and cached.get("request_key") == request_key
        if reuse:
            v16_recs = list(cached.get("v16") or [])
            v5_recs = list(cached.get("v5") or [])
            round_id = str(cached.get("round_id") or uuid.uuid4().hex)
            generated_at = str(cached.get("generated_at") or utcnow_iso())
        else:
            v16_recs = list(self.v16.recommend_romanian(when=when, count=count))
            v5_recs = list(self.v5.recommend_romanian(when=when, count=count))
            round_id = uuid.uuid4().hex
            generated_at = utcnow_iso()
            self._round_cache[slot_key] = {
                "request_key": request_key,
                "v16": list(v16_recs),
                "v5": list(v5_recs),
                "round_id": round_id,
                "generated_at": generated_at,
            }
        active_recs = v5_recs if self.mode == MODE_V5_20 else v16_recs
        self._annotate_and_persist(
            active_recs, v16_recs, v5_recs, when=when, slot=slot_key,
            round_id=round_id, generated_at=generated_at,
        )
        return active_recs

    def decision_pick(self, when=None, exclude_ids=None, mode="decide", **kwargs):
        when = when or date.today()
        slot_key = "decision"
        # Apply the same prior-day exclusions to both trial engines. Same-day navigation and
        # explicit recalculation still use the frozen comparison pair.
        recent_exclusions = self._recent_decision_exclusions(when)
        effective_exclusions = set(exclude_ids or set()) | recent_exclusions
        request_key = (
            when.isoformat(),
            tuple(sorted(int(x) for x in (exclude_ids or set()))),
            tuple(sorted(recent_exclusions)),
            str(mode or ""),
            tuple(sorted((str(key), repr(value)) for key, value in kwargs.items())),
            str(self.db.get_setting("chooser_runtime_bucket", "all") or "all"),
            str(self.db.get_setting("chooser_mood", "neutral") or "neutral"),
            str(self.db.get_setting("daily_genre_filter", {}) or {}),
            self._user_state_token(),
        )
        cached = self._round_cache.get(slot_key)
        reuse = isinstance(cached, dict) and cached.get("request_key") == request_key
        if reuse:
            v16_recs = list(cached.get("v16") or [])
            v5_recs = list(cached.get("v5") or [])
            round_id = str(cached.get("round_id") or uuid.uuid4().hex)
            generated_at = str(cached.get("generated_at") or utcnow_iso())
        else:
            v16_primary, v16_backups = self.v16.decision_pick(
                when, effective_exclusions, mode, **kwargs
            )
            v5_primary, v5_backups = self.v5.decision_pick(
                when, effective_exclusions, mode, **kwargs
            )
            v16_recs = ([v16_primary] if v16_primary else []) + list(v16_backups or [])
            v5_recs = ([v5_primary] if v5_primary else []) + list(v5_backups or [])
            round_id = uuid.uuid4().hex
            generated_at = utcnow_iso()
            self._round_cache[slot_key] = {
                "request_key": request_key,
                "v16": list(v16_recs),
                "v5": list(v5_recs),
                "round_id": round_id,
                "generated_at": generated_at,
            }
        active_recs = v5_recs if self.mode == MODE_V5_20 else v16_recs
        self._annotate_and_persist(
            active_recs, v16_recs, v5_recs, when=when, slot=slot_key,
            round_id=round_id, generated_at=generated_at,
        )
        return (
            active_recs[0] if active_recs else None,
            active_recs[1:3] if len(active_recs) > 1 else [],
        )

    def __getattr__(self, name):
        # All non-ranking APIs (calendar, status, feedback helpers, etc.) follow the currently
        # selected engine so the UI can switch instantly without rebuilding the service.
        return getattr(self.active, name)
