from __future__ import annotations

from datetime import date, datetime, timezone

from .recommendation import row_to_movie
from .watch_intent import WatchIntentLearner


MODEL_VERSION = "watch-success-v2"

# A click on "Aleg" is interest, not proof that the film was actually started.
# Stronger downstream actions therefore carry more weight than the initial choice.
_ACTION_SIGNALS = {
    "chosen": (0.38, 21.0, 0.60),
    "skip_today": (-0.55, 2.5, 1.00),
    "trailer_opened": (0.22, 10.0, 0.35),
    "play_opened": (0.95, 90.0, 1.60),
    "watched": (1.00, 150.0, 2.00),
}

_FEEDBACK_SIGNALS = {
    "want_to_watch": (0.65, 60.0, 0.75),
    "more_like_this": (0.70, 75.0, 0.85),
    "less_like_this": (-0.55, 45.0, 0.85),
    "not_interested": (-0.80, 75.0, 1.00),
    "never_similar": (-1.00, 120.0, 1.20),
}

_RECENT_RATING_SIGNALS = {
    10: 0.18,
    9: 0.14,
    8: 0.08,
    7: 0.03,
    6: 0.00,
    5: 0.00,
    4: -0.03,
    3: -0.06,
    2: -0.08,
    1: -0.10,
}


class WatchSuccessIntentLearner(WatchIntentLearner):
    """Short-horizon learner that distinguishes interest from actually moving toward playback.

    `chosen` remains useful, but it is deliberately weaker than `play_opened` or `watched`.
    This prevents the recommender from congratulating itself merely because the user clicked a
    choice button and then never watched the film.
    """

    def state_token(self) -> tuple:
        with self.db.connect() as con:
            actions = con.execute(
                """SELECT COUNT(*),COALESCE(MAX(id),0)
                   FROM recommendation_history
                   WHERE action IN ('chosen','skip_today','trailer_opened','play_opened','watched')"""
            ).fetchone()
            feedback = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(id),0) FROM feedback"
            ).fetchone()
            ratings = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings"
            ).fetchone()
        return (
            MODEL_VERSION,
            date.today().isoformat(),
            int(actions[0]), int(actions[1]),
            int(feedback[0]), int(feedback[1]),
            int(ratings[0]), str(ratings[1]),
        )

    def _prepare(self) -> None:
        token = self.state_token()
        with self._lock:
            if token == self._token and self._status.get("state") == "ready":
                return

        now = datetime.now(timezone.utc)
        positive: dict[str, float] = {}
        negative: dict[str, float] = {}
        positive_weight = 0.0
        negative_weight = 0.0
        effective_events = 0.0
        raw_events = 0
        play_events = 0
        trailer_events = 0
        recent_rating_signals = 0

        with self.db.connect() as con:
            action_rows = con.execute(
                """SELECT m.*,h.action AS intent_action,h.recommended_at AS intent_at
                   FROM recommendation_history h JOIN movies m ON m.id=h.movie_id
                   WHERE h.action IN ('chosen','skip_today','trailer_opened','play_opened','watched')
                   ORDER BY h.id DESC LIMIT 500"""
            ).fetchall()
            feedback_rows = con.execute(
                """SELECT m.*,f.kind AS intent_kind,f.created_at AS intent_at
                   FROM feedback f JOIN movies m ON m.id=f.movie_id
                   WHERE f.kind IN ('want_to_watch','more_like_this','less_like_this','not_interested','never_similar')
                   ORDER BY f.id DESC LIMIT 400"""
            ).fetchall()
            rating_rows = con.execute(
                """SELECT m.*,r.rating AS intent_rating,
                          COALESCE(r.date_rated,r.updated_at) AS intent_at
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   ORDER BY COALESCE(r.date_rated,r.updated_at) DESC LIMIT 300"""
            ).fetchall()

        for row in action_rows:
            kind = str(row["intent_action"] or "")
            cfg = _ACTION_SIGNALS.get(kind)
            if not cfg:
                continue
            signal, half_life, event_credit = cfg
            decay = self._decay(row["intent_at"], half_life, now)
            strength = abs(signal) * decay
            if strength < 0.015:
                continue
            raw_events += 1
            effective_events += event_credit * decay
            if kind in {"play_opened", "watched"}:
                play_events += 1
            elif kind == "trailer_opened":
                trailer_events += 1
            movie = row_to_movie(row)
            if signal > 0:
                self._accumulate(positive, movie, strength)
                positive_weight += strength
            else:
                self._accumulate(negative, movie, strength)
                negative_weight += strength

        for row in feedback_rows:
            cfg = _FEEDBACK_SIGNALS.get(str(row["intent_kind"] or ""))
            if not cfg:
                continue
            signal, half_life, event_credit = cfg
            decay = self._decay(row["intent_at"], half_life, now)
            strength = abs(signal) * decay
            if strength < 0.015:
                continue
            raw_events += 1
            effective_events += event_credit * decay
            movie = row_to_movie(row)
            if signal > 0:
                self._accumulate(positive, movie, strength)
                positive_weight += strength
            else:
                self._accumulate(negative, movie, strength)
                negative_weight += strength

        # Ratings remain only a weak bootstrap. They say what the user liked, not what they are
        # likely to start in this particular session.
        for row in rating_rows:
            rating = max(1, min(10, int(row["intent_rating"])))
            signal = float(_RECENT_RATING_SIGNALS.get(rating, 0.0))
            if not signal:
                continue
            strength = abs(signal) * self._decay(row["intent_at"], 35.0, now)
            if strength < 0.01:
                continue
            recent_rating_signals += 1
            movie = row_to_movie(row)
            if signal > 0:
                self._accumulate(positive, movie, strength)
                positive_weight += strength
            else:
                self._accumulate(negative, movie, strength)
                negative_weight += strength

        positive = self._normalize(positive, positive_weight)
        negative = self._normalize(negative, negative_weight)
        effective_count = max(0, int(round(effective_events)))
        max_blend = self._blend_cap(effective_count, recent_rating_signals)
        active = bool(max_blend > 0 and (positive or negative))

        with self._lock:
            self._token = token
            self._positive = positive
            self._negative = negative
            self._status = {
                "state": "ready",
                "version": MODEL_VERSION,
                "active": active,
                # The inherited scorer uses explicit_events for confidence. Use effective evidence
                # rather than raw clicks so repeated trailer opens cannot rapidly unlock 28% blend.
                "explicit_events": effective_count,
                "raw_explicit_events": raw_events,
                "play_events": play_events,
                "trailer_events": trailer_events,
                "recent_rating_signals": recent_rating_signals,
                "positive_evidence": round(positive_weight, 4),
                "negative_evidence": round(negative_weight, 4),
                "max_blend_weight": round(max_blend, 4),
            }
