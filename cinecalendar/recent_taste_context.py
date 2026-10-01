from __future__ import annotations

import math
import threading
from datetime import date, datetime

from .recommendation import row_to_movie
from .semantic import feature_vector
from .util import clamp, cosine_sparse


RECENT_TASTE_VERSION = "recent-taste-v1"
MAX_SIGNALS = 160
WINDOW_DAYS = 540
HALF_LIFE_DAYS = 180.0


class RecentTasteContext:
    """Short-horizon taste signal that complements, never replaces, long-term taste.

    The anchor date is the newest rating present in the database. This makes historical replay
    deterministic because temporary backtest databases already remove future ratings.
    """

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._token = None
        self._status = {"version": RECENT_TASTE_VERSION, "active": False, "signals": 0}
        self._positive: dict[str, float] = {}
        self._negative: dict[str, float] = {}

    @staticmethod
    def _parse_day(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)[:10]).date()
        except ValueError:
            return None

    @staticmethod
    def _normalize(values: dict[str, float], total: float) -> dict[str, float]:
        if total <= 0:
            return {}
        norm = math.sqrt(sum(v * v for v in values.values())) or 1.0
        return {k: v / norm for k, v in values.items() if abs(v) >= 1e-8}

    def state_token(self) -> tuple:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT COUNT(*),COALESCE(MAX(r.updated_at),''),
                          COALESCE(MAX(r.date_rated),''),
                          COALESCE(MAX(m.updated_at),'')
                   FROM ratings r JOIN movies m ON m.id=r.movie_id"""
            ).fetchone()
        return (
            RECENT_TASTE_VERSION,
            int(row[0] or 0),
            str(row[1] or ""),
            str(row[2] or ""),
            str(row[3] or ""),
        )

    def _ensure(self) -> None:
        token = self.state_token()
        with self._lock:
            if token == self._token:
                return

        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.*,r.rating AS user_rating,
                          COALESCE(NULLIF(r.date_rated,''),r.updated_at,r.imported_at,'') AS signal_date
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   WHERE r.rating>=8 OR r.rating<=4
                   ORDER BY signal_date DESC,r.id DESC
                   LIMIT ?""",
                (MAX_SIGNALS,),
            ).fetchall()

        parsed = [(row, self._parse_day(row["signal_date"])) for row in rows]
        anchor = next((d for _row, d in parsed if d is not None), None)
        positive: dict[str, float] = {}
        negative: dict[str, float] = {}
        pos_w = neg_w = 0.0
        used = 0

        for row, signal_day in parsed:
            if anchor is not None and signal_day is not None:
                age = max(0, (anchor - signal_day).days)
                if age > WINDOW_DAYS:
                    continue
            else:
                age = WINDOW_DAYS // 2
            rating = int(row["user_rating"])
            recency = 0.25 + 0.75 * math.exp(-age / HALF_LIFE_DAYS)
            extremity = 1.0 + 0.12 * abs(rating - 6)
            weight = recency * extremity
            vec = feature_vector(row_to_movie(row))
            if not vec:
                continue
            target = positive if rating >= 8 else negative
            for key, value in vec.items():
                target[str(key)] = target.get(str(key), 0.0) + float(value) * weight
            if rating >= 8:
                pos_w += weight
            else:
                neg_w += weight
            used += 1

        positive = self._normalize(positive, pos_w)
        negative = self._normalize(negative, neg_w)
        active = bool(used >= 8 and (positive or negative))
        confidence = min(1.0, used / 32.0) if active else 0.0

        with self._lock:
            self._token = token
            self._positive = positive
            self._negative = negative
            self._status = {
                "version": RECENT_TASTE_VERSION,
                "active": active,
                "signals": used,
                "positive_evidence": round(pos_w, 4),
                "negative_evidence": round(neg_w, 4),
                "confidence": round(confidence, 4),
                "window_days": WINDOW_DAYS,
                "anchor_date": anchor.isoformat() if anchor else "",
            }

    @staticmethod
    def _score_snapshot(movie, status: dict, positive: dict[str, float], negative: dict[str, float]) -> dict:
        if not status.get("active"):
            return {"active": False, **status, "score": 0.5, "nudge": 0.0}

        vec = feature_vector(movie)
        if not vec:
            return {"active": False, **status, "score": 0.5, "nudge": 0.0}
        pos = clamp(cosine_sparse(vec, positive)) if positive else 0.0
        neg = clamp(cosine_sparse(vec, negative)) if negative else 0.0
        if positive and negative:
            raw = math.tanh(2.0 * (pos - neg))
            score = clamp(0.5 + 0.5 * raw)
        elif positive:
            score = clamp(0.35 + 0.65 * pos)
        else:
            score = clamp(0.65 - 0.65 * neg)
        confidence = float(status.get("confidence", 0.0) or 0.0)
        nudge = max(-0.035, min(0.035, (score - 0.5) * 0.07 * confidence))
        return {
            "active": True,
            **status,
            "score": score,
            "positive_similarity": pos,
            "negative_similarity": neg,
            "nudge": nudge,
        }

    def score_many(self, movies) -> list[dict]:
        self._ensure()
        with self._lock:
            status = dict(self._status)
            positive = dict(self._positive)
            negative = dict(self._negative)
        return [self._score_snapshot(movie, status, positive, negative) for movie in movies]

    def score(self, movie) -> dict:
        return self.score_many([movie])[0]

    def status(self) -> dict:
        self._ensure()
        with self._lock:
            return dict(self._status)
