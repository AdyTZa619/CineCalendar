from __future__ import annotations

from .watch_success import WatchSuccessIntentLearner


MODEL_VERSION = "watch-success-v3.3-exposure"


class WatchSuccessIntentLearnerV33(WatchSuccessIntentLearner):
    """Watch Success v3 with exposure-level funnel identity.

    The inherited learner still performs the mature weighting/decay logic. Only funnel identity is
    replaced: new events collapse by exposure_history_id, while genuinely old unlinked rows fall
    back to movie/day. This prevents two recommendations of the same film on one day from becoming
    one artificial training example.
    """

    def __init__(self, db):
        super().__init__(db)
        self._funnel_map_token = None
        self._funnel_map: dict[int, int | None] = {}
        self._last_state_token_value = None

    def state_token(self) -> tuple:
        base = super().state_token()
        token = (MODEL_VERSION,) + tuple(base[1:])
        self._last_state_token_value = token
        return token

    def _ensure_funnel_map(self) -> None:
        token = self._last_state_token_value
        if token is None:
            token = self.state_token()
        if token == self._funnel_map_token:
            return
        marks = ",".join("?" for _ in self.ACTION_NAMES)
        with self.db.connect() as con:
            rows = con.execute(
                f"""SELECT h.id,h.exposure_history_id,
                           EXISTS(
                               SELECT 1 FROM recommendation_trust_audit t
                               WHERE t.history_id=h.id
                           ) AS is_trust_exposure
                    FROM recommendation_history h
                    WHERE h.action IN ({marks})
                    ORDER BY h.id DESC LIMIT 900""",
                self.ACTION_NAMES,
            ).fetchall()
        mapping: dict[int, int | None] = {}
        for row in rows:
            event_id = int(row["id"])
            parent = row["exposure_history_id"]
            if parent is not None:
                mapping[event_id] = int(parent)
            elif int(row["is_trust_exposure"] or 0):
                # v3.2 compatibility: chosen/skip could mutate the root exposure itself.
                mapping[event_id] = event_id
            else:
                mapping[event_id] = None
        self._funnel_map = mapping
        self._funnel_map_token = token

    def _funnel_key(self, row):
        self._ensure_funnel_map()
        event_id = int(row["intent_event_id"])
        exposure_id = self._funnel_map.get(event_id)
        if exposure_id is not None:
            return ("exposure", int(exposure_id))
        # Genuine legacy data only. New v3.3 UI events always carry exposure_history_id.
        return super()._funnel_key(row)
