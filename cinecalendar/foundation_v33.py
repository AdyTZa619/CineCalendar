from __future__ import annotations

from datetime import date

from .recommender_v16 import ENGINE_VERSION
from .recommendation_history_v43 import record_explanation_snapshot
from .trust_audit import ensure_trust_audit_schema, record_trust_snapshot, validate_exposure_history_id
from .util import utcnow_iso


def _candidate_count(window, visible_count: int) -> int:
    try:
        status = window.s.recommender.quality_gate_status()
        pool = int(status.get("pool") or 0)
        return max(visible_count, pool)
    except Exception:
        return max(visible_count, 0)


def _record_exposures(window, recs, ctx: date, slot: str) -> list[int]:
    """Persist the concrete visible recommendation objects exactly once.

    The Recommendation object receives its immutable root history id. Repainting the same object
    does not create another exposure; generating a new object later does, which is the correct
    representation of a new recommendation exposure.
    """
    recs = [rec for rec in list(recs or []) if rec is not None and getattr(rec.movie, "id", None)]
    if not recs:
        return []

    context_date = ctx.isoformat()
    mapping = getattr(window, "_visible_exposures", None)
    if not isinstance(mapping, dict):
        mapping = {}
        window._visible_exposures = mapping

    new_recs = []
    kept_ids: list[int] = []
    with window.db.connect() as con:
        ensure_trust_audit_schema(con)
        for rec in recs:
            existing = getattr(rec, "exposure_history_id", None)
            row = validate_exposure_history_id(
                con,
                existing,
                movie_id=int(rec.movie.id),
                context_date=context_date,
            )
            if row is not None:
                eid = int(row["id"])
                rec.exposure_history_id = eid
                rec.exposure_context_date = context_date
                rec.exposure_slot = str(row["slot"] or slot)
                mapping[int(rec.movie.id)] = eid
                kept_ids.append(eid)
            else:
                rec.exposure_history_id = None
                new_recs.append(rec)

    if not new_recs:
        return kept_ids

    now = utcnow_iso()
    engine_version = str(
        getattr(window.s.recommender, "LEARNING_INSIGHT_VERSION", ENGINE_VERSION) or ENGINE_VERSION
    )
    with window.db.tx() as con:
        ensure_trust_audit_schema(con)
        run = con.execute(
            """INSERT INTO recommendation_runs(
                   context_date,slot,generated_at,candidate_count,result_count,engine_version
               ) VALUES(?,?,?,?,?,?)""",
            (
                context_date,
                str(slot),
                now,
                _candidate_count(window, len(recs)),
                len(new_recs),
                engine_version,
            ),
        )
        run_id = int(run.lastrowid)

        for rank_position, rec in enumerate(new_recs, start=1):
            history = con.execute(
                """INSERT INTO recommendation_history(
                       movie_id,recommended_at,context_date,slot,final_score,ignored,action,exposure_history_id,
                       predicted_rating,confidence
                   ) VALUES(?,?,?,?,?,0,NULL,NULL,?,?)""",
                (
                    int(rec.movie.id),
                    now,
                    context_date,
                    str(slot),
                    float(rec.score.final),
                    float(rec.score.predicted_rating) if rec.score.predicted_rating is not None else None,
                    float(rec.score.confidence) if rec.score.confidence is not None else None,
                ),
            )
            history_id = int(history.lastrowid)
            payload = getattr(rec.score, "trust_audit", None)
            record_trust_snapshot(
                con,
                history_id=history_id,
                run_id=run_id,
                movie_id=int(rec.movie.id),
                context_date=context_date,
                slot=str(slot),
                rank_position=rank_position,
                engine_version=engine_version,
                payload=payload if isinstance(payload, dict) else {"status": "unclassified"},
                created_at=now,
            )
            record_explanation_snapshot(con, history_id, rec.score, created_at=now)
            rec.exposure_history_id = history_id
            rec.exposure_context_date = context_date
            rec.exposure_slot = str(slot)
            mapping[int(rec.movie.id)] = history_id
            kept_ids.append(history_id)

    return kept_ids


def install_foundation_v33(window_cls) -> None:
    """Install the v3.3 exposure identity layer after the legacy UI patches are composed."""
    original_render_today = getattr(window_cls, "_render_today", None)
    original_render_browse = getattr(window_cls, "_render_browse", None)

    def record_once(self, recs, ctx, slot):
        return _record_exposures(self, recs, ctx, slot)

    window_cls.record_once = record_once

    if callable(original_render_today):
        def _render_today(self, primary, backups):
            visible = ([primary] if primary is not None else []) + list(backups or [])[:2]
            # Persist every card the user can act on before any buttons are created.
            self.record_once(visible, date.today(), "decision")
            return original_render_today(self, primary, backups)
        window_cls._render_today = _render_today

    if callable(original_render_browse):
        def _render_browse(self, recs):
            self.record_once(list(recs or []), date.today(), "browse")
            return original_render_browse(self, recs)
        window_cls._render_browse = _render_browse
