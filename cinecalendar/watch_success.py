from __future__ import annotations

from datetime import date, datetime, timezone
import math

from .recommendation import row_to_movie
from .semantic import feature_vector
from .util import clamp, cosine_sparse
from .watch_intent import WatchIntentLearner


MODEL_VERSION = "watch-success-v3"

# Watch Success is a funnel, not a bag of independent clicks. Opening Stremio only proves that
# CineCalendar handed the title off to the app; it does NOT prove playback actually started.
# Strong success therefore requires an explicit playback confirmation or a later watched signal.
# `play_opened` is retained as a legacy 2.7.0 action and intentionally downgraded to the same
# strength as `stremio_opened`.
_ACTION_SIGNALS = {
    "chosen": (0.32, 18.0, 0.45),
    "skip_today": (-0.62, 2.5, 1.00),
    "trailer_opened": (0.16, 7.0, 0.25),
    "stremio_opened": (0.44, 18.0, 0.55),
    "play_opened": (0.44, 18.0, 0.55),  # legacy 2.7.0: URL dispatch, not verified playback
    "playback_confirmed": (0.95, 75.0, 1.55),
    "watched": (1.00, 150.0, 1.90),
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
    """Short-horizon learner for what actually progresses toward watching.

    The learner intentionally distinguishes five concepts:
      * choosing/keeping a recommendation;
      * checking a trailer;
      * handing the title to Stremio;
      * explicitly confirming that playback started;
      * confirming that the film was watched.

    Actions for the same movie on the same date are collapsed to the latest funnel outcome before
    learning. This prevents `chosen -> trailer -> Stremio -> watched` from counting as four positive
    examples for one film, and makes `chosen -> skip` correctly end as a skip for that day.
    """

    ACTION_NAMES = tuple(_ACTION_SIGNALS)

    def state_token(self) -> tuple:
        marks = ",".join("?" for _ in self.ACTION_NAMES)
        with self.db.connect() as con:
            actions = con.execute(
                f"""SELECT COUNT(*),COALESCE(MAX(id),0)
                    FROM recommendation_history
                    WHERE action IN ({marks})""",
                self.ACTION_NAMES,
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

    @staticmethod
    def _funnel_key(row) -> tuple[int, str]:
        movie_id = int(row["intent_movie_id"])
        day = str(row["intent_context_date"] or "").strip()
        if not day:
            raw = str(row["intent_at"] or "")
            day = raw[:10] if len(raw) >= 10 else "unknown"
        return movie_id, day

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
        raw_action_events = 0
        collapsed_action_events = 0
        launch_events = 0
        playback_events = 0
        trailer_events = 0
        recent_rating_signals = 0

        marks = ",".join("?" for _ in self.ACTION_NAMES)
        with self.db.connect() as con:
            action_rows = con.execute(
                f"""SELECT m.*,m.id AS intent_movie_id,
                           h.id AS intent_event_id,h.action AS intent_action,
                           h.recommended_at AS intent_at,h.context_date AS intent_context_date
                    FROM recommendation_history h JOIN movies m ON m.id=h.movie_id
                    WHERE h.action IN ({marks})
                    ORDER BY h.id DESC LIMIT 900""",
                self.ACTION_NAMES,
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

        # Rows arrive newest-first. Keep only the latest outcome for one movie/day funnel.
        latest_by_funnel: dict[tuple[int, str], object] = {}
        raw_action_events = len(action_rows)
        for row in action_rows:
            key = self._funnel_key(row)
            if key not in latest_by_funnel:
                latest_by_funnel[key] = row

        collapsed_rows = sorted(
            latest_by_funnel.values(),
            key=lambda row: int(row["intent_event_id"]),
            reverse=True,
        )

        for row in collapsed_rows:
            kind = str(row["intent_action"] or "")
            cfg = _ACTION_SIGNALS.get(kind)
            if not cfg:
                continue
            signal, half_life, event_credit = cfg
            decay = self._decay(row["intent_at"], half_life, now)
            strength = abs(signal) * decay
            if strength < 0.015:
                continue
            collapsed_action_events += 1
            effective_events += event_credit * decay
            if kind in {"stremio_opened", "play_opened"}:
                launch_events += 1
            elif kind in {"playback_confirmed", "watched"}:
                playback_events += 1
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
            effective_events += event_credit * decay
            movie = row_to_movie(row)
            if signal > 0:
                self._accumulate(positive, movie, strength)
                positive_weight += strength
            else:
                self._accumulate(negative, movie, strength)
                negative_weight += strength

        # Ratings remain only a weak bootstrap. They say what the user liked, not whether the
        # recommendation succeeded in getting the film started in this session.
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
                # The scorer uses explicit_events for confidence. Effective evidence is based on
                # collapsed funnels, so repeatedly clicking the same film cannot unlock 28% blend.
                "explicit_events": effective_count,
                "raw_action_events": raw_action_events,
                "collapsed_action_events": collapsed_action_events,
                "launch_events": launch_events,
                "playback_events": playback_events,
                "trailer_events": trailer_events,
                "recent_rating_signals": recent_rating_signals,
                "positive_evidence": round(positive_weight, 4),
                "negative_evidence": round(negative_weight, 4),
                "max_blend_weight": round(max_blend, 4),
            }

    @staticmethod
    def _inactive_score() -> dict:
        return {
            "active": False,
            "score": 0.5,
            "confidence": 0.0,
            "blend_weight": 0.0,
            "positive_similarity": 0.0,
            "negative_similarity": 0.0,
            "reason": "",
        }

    @classmethod
    def _score_snapshot(cls, movie, status: dict, positive: dict[str, float], negative: dict[str, float]) -> dict:
        if not status.get("active"):
            return cls._inactive_score()
        vec = feature_vector(movie)
        if not vec:
            return cls._inactive_score()

        pos = clamp(cosine_sparse(vec, positive)) if positive else 0.0
        neg = clamp(cosine_sparse(vec, negative)) if negative else 0.0
        if positive and negative:
            intent_score = clamp(0.5 + 0.5 * math.tanh(2.15 * (pos - neg)))
        elif positive:
            intent_score = clamp(0.35 + 0.65 * pos)
        else:
            intent_score = clamp(0.65 - 0.65 * neg)

        explicit = int(status.get("explicit_events", 0) or 0)
        recent = int(status.get("recent_rating_signals", 0) or 0)
        both_sides = 1.0 if positive and negative else 0.62
        sample_conf = 1.0 - math.exp(-(explicit + 0.12 * recent) / 18.0)
        confidence = clamp((0.22 + 0.78 * sample_conf) * both_sides)
        cap = float(status.get("max_blend_weight", 0.0) or 0.0)
        blend = min(0.28, cap * (0.45 + 0.55 * confidence))

        if intent_score >= 0.68:
            label = "ridicat"
        elif intent_score >= 0.54:
            label = "bun"
        elif intent_score <= 0.36:
            label = "scăzut"
        else:
            label = "neutru"
        reason = (
            f"Semnal de intenție de vizionare {label}; nu este o probabilitate calibrată și nu "
            "înlocuiește nota estimată. Este învățat local din rezultate recente ale traseului "
            "alegere → trailer/Stremio → pornire confirmată → vizionat, plus skip-uri și feedback."
        )
        return {
            "active": True,
            "score": intent_score,
            "confidence": confidence,
            "blend_weight": blend,
            "positive_similarity": pos,
            "negative_similarity": neg,
            "reason": reason,
        }

    def score_many(self, movies) -> list[dict]:
        """Prepare once and score a whole finalist pool without one SQLite token query per film."""
        movies = list(movies)
        if not movies:
            return []
        self._prepare()
        with self._lock:
            status = dict(self._status)
            positive = dict(self._positive)
            negative = dict(self._negative)
        return [self._score_snapshot(movie, status, positive, negative) for movie in movies]
