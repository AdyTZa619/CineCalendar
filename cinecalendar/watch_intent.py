from __future__ import annotations

from datetime import date, datetime, timezone
import math
import threading

from .recommendation import row_to_movie
from .semantic import feature_vector
from .util import clamp, cosine_sparse


MODEL_VERSION = "watch-intent-v1"

# These are intentionally contextual signals, not permanent taste labels.
# In particular, skip_today decays very quickly: skipping a film tonight must not become
# "the user dislikes this kind of film forever".
_ACTION_SIGNALS = {
    "chosen": (1.00, 45.0),
    "skip_today": (-0.55, 2.5),
}

_FEEDBACK_SIGNALS = {
    "want_to_watch": (0.85, 60.0),
    "more_like_this": (0.70, 75.0),
    "less_like_this": (-0.55, 45.0),
    "not_interested": (-0.80, 75.0),
    "never_similar": (-1.00, 120.0),
}

# Recent IMDb ratings are only a weak bootstrap for "what feels attractive now". They must
# never overpower explicit choice/skip behaviour or duplicate the long-term taste model.
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


class WatchIntentLearner:
    """Local short-horizon model for "would I actually choose this now?".

    This model is deliberately separate from the long-term taste/rating model. A user can like a
    film in principle and still not want it tonight. Explicit choice/skip feedback therefore gets
    a fast-decaying contextual model, while recent IMDb ratings are used only as weak bootstrap.

    No data is uploaded. The model is a pair of sparse local prototypes built from the same movie
    features already used by CineCalendar, so preparing it is cheap enough to stay on the ranking
    path and is cached until relevant history changes.
    """

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._token = None
        self._positive: dict[str, float] = {}
        self._negative: dict[str, float] = {}
        self._status = {
            "state": "not_ready",
            "version": MODEL_VERSION,
            "active": False,
            "explicit_events": 0,
            "recent_rating_signals": 0,
            "positive_evidence": 0.0,
            "negative_evidence": 0.0,
            "max_blend_weight": 0.0,
        }

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.fromisoformat(raw[:10])
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @classmethod
    def _decay(cls, value: str | None, half_life_days: float, now: datetime | None = None) -> float:
        dt = cls._parse_time(value)
        if dt is None:
            return 0.35
        now = now or datetime.now(timezone.utc)
        age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
        half_life_days = max(0.25, float(half_life_days))
        return math.pow(0.5, age_days / half_life_days)

    def state_token(self) -> tuple:
        """Token used both by this cache and V13's decision-pool invalidation.

        Including chosen/skip actions is important: pressing "Alt film" must be able to affect the
        very next ranking instead of leaving an already-cached pool frozen for the rest of session.
        """
        with self.db.connect() as con:
            actions = con.execute(
                """SELECT COUNT(*),COALESCE(MAX(id),0)
                   FROM recommendation_history
                   WHERE action IN ('chosen','skip_today')"""
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
    def _accumulate(target: dict[str, float], movie, weight: float) -> None:
        if weight <= 0:
            return
        for token, value in feature_vector(movie).items():
            value = float(value)
            if abs(value) < 0.03:
                continue
            target[token] = target.get(token, 0.0) + value * weight

    @staticmethod
    def _normalize(vec: dict[str, float], total_weight: float) -> dict[str, float]:
        if total_weight <= 0:
            return {}
        out = {k: v / total_weight for k, v in vec.items() if abs(v) >= 1e-8}
        # Keep the prototype compact and deterministic; weak long-tail metadata should not create
        # accidental intent swings.
        ordered = sorted(out.items(), key=lambda item: (-abs(item[1]), item[0]))[:180]
        return dict(ordered)

    @staticmethod
    def _blend_cap(explicit_events: int, recent_rating_signals: int) -> float:
        # Do not give click behaviour a large voice from a handful of actions.
        if explicit_events >= 25:
            return 0.28
        if explicit_events >= 12:
            return 0.22
        if explicit_events >= 6:
            return 0.16
        if explicit_events >= 2:
            return 0.10
        if recent_rating_signals >= 15:
            return 0.06
        return 0.0

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
        explicit_events = 0
        recent_rating_signals = 0

        with self.db.connect() as con:
            action_rows = con.execute(
                """SELECT m.*,h.action AS intent_action,h.recommended_at AS intent_at
                   FROM recommendation_history h JOIN movies m ON m.id=h.movie_id
                   WHERE h.action IN ('chosen','skip_today')
                   ORDER BY h.id DESC LIMIT 400"""
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
            cfg = _ACTION_SIGNALS.get(str(row["intent_action"] or ""))
            if not cfg:
                continue
            signal, half_life = cfg
            strength = abs(signal) * self._decay(row["intent_at"], half_life, now)
            if strength < 0.015:
                continue
            explicit_events += 1
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
            signal, half_life = cfg
            strength = abs(signal) * self._decay(row["intent_at"], half_life, now)
            if strength < 0.015:
                continue
            explicit_events += 1
            movie = row_to_movie(row)
            if signal > 0:
                self._accumulate(positive, movie, strength)
                positive_weight += strength
            else:
                self._accumulate(negative, movie, strength)
                negative_weight += strength

        # Weak bootstrap only. It lets the model become mildly useful before enough choose/skip
        # actions exist, but its maximum influence is capped at 6% without explicit interaction.
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
        max_blend = self._blend_cap(explicit_events, recent_rating_signals)
        active = bool(max_blend > 0 and (positive or negative))

        with self._lock:
            self._token = token
            self._positive = positive
            self._negative = negative
            self._status = {
                "state": "ready",
                "version": MODEL_VERSION,
                "active": active,
                "explicit_events": explicit_events,
                "recent_rating_signals": recent_rating_signals,
                "positive_evidence": round(positive_weight, 4),
                "negative_evidence": round(negative_weight, 4),
                "max_blend_weight": round(max_blend, 4),
            }

    def status(self) -> dict:
        self._prepare()
        with self._lock:
            return dict(self._status)

    def score(self, movie) -> dict:
        self._prepare()
        with self._lock:
            status = dict(self._status)
            positive = dict(self._positive)
            negative = dict(self._negative)
        if not status.get("active"):
            return {
                "active": False,
                "score": 0.5,
                "confidence": 0.0,
                "blend_weight": 0.0,
                "positive_similarity": 0.0,
                "negative_similarity": 0.0,
                "reason": "",
            }

        vec = feature_vector(movie)
        if not vec:
            return {
                "active": False,
                "score": 0.5,
                "confidence": 0.0,
                "blend_weight": 0.0,
                "positive_similarity": 0.0,
                "negative_similarity": 0.0,
                "reason": "",
            }

        pos = clamp(cosine_sparse(vec, positive)) if positive else 0.0
        neg = clamp(cosine_sparse(vec, negative)) if negative else 0.0
        if positive and negative:
            probability = clamp(0.5 + 0.5 * math.tanh(2.15 * (pos - neg)))
        elif positive:
            probability = clamp(0.35 + 0.65 * pos)
        else:
            probability = clamp(0.65 - 0.65 * neg)

        explicit = int(status.get("explicit_events", 0) or 0)
        recent = int(status.get("recent_rating_signals", 0) or 0)
        both_sides = 1.0 if positive and negative else 0.62
        sample_conf = 1.0 - math.exp(-(explicit + 0.12 * recent) / 18.0)
        confidence = clamp((0.22 + 0.78 * sample_conf) * both_sides)
        cap = float(status.get("max_blend_weight", 0.0) or 0.0)
        blend = min(0.28, cap * (0.45 + 0.55 * confidence))

        if probability >= 0.68:
            label = "ridicată"
        elif probability >= 0.54:
            label = "bună"
        elif probability <= 0.36:
            label = "scăzută"
        else:
            label = "neutră"
        reason = (
            f"Probabilitate de alegere acum {label}; semnal separat de nota estimată, "
            "învățat local din alegeri, skip-uri și feedback recent."
        )
        return {
            "active": True,
            "score": probability,
            "confidence": confidence,
            "blend_weight": blend,
            "positive_similarity": pos,
            "negative_similarity": neg,
            "reason": reason,
        }
