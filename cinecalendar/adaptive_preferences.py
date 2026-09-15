from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import math
import threading

from .recommendation import row_to_movie
from .semantic import feature_vector
from .util import clamp


MODEL_VERSION = "adaptive-personal-v1"
HASH_DIM = 4096
MIN_RATINGS = 80
VALIDATION_MIN = 30
EPOCHS = 9
L2 = 0.0008


@dataclass
class _Sample:
    movie: object
    target: float
    weight: float
    date_key: str


class AdaptivePreferenceLearner:
    """Local, continuously adapting model trained only from the user's own taste signals.

    The shared MovieLens ALS model answers "people with a similar pattern often liked this".
    This model answers a different question: "given this user's *actual* ratings and explicit
    feedback, what characteristics are increasingly associated with a high or low rating?".

    It deliberately stays local. No rating, feature vector, feedback event or learned parameter
    is uploaded anywhere. The model is rebuilt automatically after a rating/feedback change.
    """

    _PAIR_KINDS = {
        ("genre", "director"),
        ("genre", "theme"),
        ("genre", "country"),
        ("genre", "decade"),
        ("director", "decade"),
        ("theme", "decade"),
    }

    _FEEDBACK_TARGETS = {
        "more_like_this": (0.82, 1.00),
        "less_like_this": (-0.35, 0.75),
        "not_interested": (-0.72, 0.95),
        "never_similar": (-0.95, 1.15),
    }

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._token = None
        self._weights = [0.0] * HASH_DIM
        self._support = [0.0] * HASH_DIM
        self._bias = 0.0
        self._status = {
            "state": "not_trained",
            "version": MODEL_VERSION,
            "training_ratings": 0,
            "training_feedback": 0,
            "holdout_count": 0,
            "model_mae": None,
            "baseline_mae": None,
            "validation_gain": 0.0,
            "validated": False,
            "max_blend_weight": 0.0,
        }

    @staticmethod
    def _hash(token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8", "ignore"), digest_size=8).digest()
        return int.from_bytes(digest, "little", signed=False) % HASH_DIM

    @staticmethod
    def _kind(token: str) -> str:
        return token.split(":", 1)[0] if ":" in token else "theme"

    @staticmethod
    def _rating_target(rating: int) -> float:
        # 1 -> -1, 5 -> slightly negative, 6 -> slightly positive, 10 -> +1.
        rating = max(1, min(10, int(rating)))
        return (rating - 5.5) / 4.5

    @staticmethod
    def _target_to_rating(target: float) -> float:
        return max(1.0, min(10.0, 5.5 + 4.5 * float(target)))

    @staticmethod
    def _recency_weight(value: str | None, today: date | None = None) -> float:
        """Allow taste to drift without erasing old favourites.

        New ratings have full weight. Very old ratings retain a 55% floor, so long-term taste
        remains useful while recent behaviour can gradually move the model.
        """
        if not value:
            return 0.78
        today = today or date.today()
        try:
            d = datetime.fromisoformat(str(value)[:10]).date()
        except ValueError:
            return 0.78
        days = max(0, (today - d).days)
        return 0.55 + 0.45 * math.exp(-days / (365.25 * 4.0))

    def _token_values(self, movie) -> dict[str, float]:
        base = {
            str(k): float(v)
            for k, v in feature_vector(movie).items()
            if abs(float(v)) >= 0.03
        }
        # Keep the strongest evidence so one heavily enriched title cannot create thousands of
        # pair terms. Deterministic ordering also makes tests and rebuilds reproducible.
        ordered = sorted(base.items(), key=lambda item: (-abs(item[1]), item[0]))[:36]
        out = dict(ordered)

        by_kind: dict[str, list[tuple[str, float]]] = {}
        for token, value in ordered:
            by_kind.setdefault(self._kind(token), []).append((token, value))

        kinds = sorted(by_kind)
        for i, left_kind in enumerate(kinds):
            for right_kind in kinds[i + 1:]:
                if (left_kind, right_kind) not in self._PAIR_KINDS and (right_kind, left_kind) not in self._PAIR_KINDS:
                    continue
                for left, lv in by_kind[left_kind][:5]:
                    for right, rv in by_kind[right_kind][:5]:
                        a, b = sorted((left, right))
                        out[f"pair:{a}|{b}"] = max(-1.0, min(1.0, lv * rv))

        if getattr(movie, "imdb_rating", None) is not None:
            out["meta:imdb_quality"] = max(-1.0, min(1.0, (float(movie.imdb_rating) - 6.5) / 3.5))
        votes = max(0, int(getattr(movie, "num_votes", 0) or 0))
        if votes:
            out["meta:vote_evidence"] = min(1.0, math.log1p(votes) / math.log1p(500_000))
        return out

    def _vector(self, movie) -> tuple[dict[int, float], dict[int, list[tuple[str, float]]]]:
        values: dict[int, float] = {}
        labels: dict[int, list[tuple[str, float]]] = {}
        for token, value in self._token_values(movie).items():
            idx = self._hash(token)
            values[idx] = values.get(idx, 0.0) + float(value)
            labels.setdefault(idx, []).append((token, float(value)))
        # Hash collisions are possible by design; cap accumulated values so a collision does not
        # become an accidental high-amplitude feature.
        values = {idx: max(-1.5, min(1.5, value)) for idx, value in values.items()}
        return values, labels

    def state_token(self):
        with self.db.connect() as con:
            rating = con.execute("SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings").fetchone()
            feedback = con.execute("SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback").fetchone()
        return MODEL_VERSION, int(rating[0]), str(rating[1]), int(feedback[0]), str(feedback[1])

    def _rating_samples(self) -> list[_Sample]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.*,r.rating AS user_rating,r.date_rated,r.updated_at AS rating_updated_at
                   FROM ratings r JOIN movies m ON m.id=r.movie_id"""
            ).fetchall()
        samples: list[_Sample] = []
        for row in rows:
            movie = row_to_movie(row)
            target = self._rating_target(int(row["user_rating"]))
            recency = self._recency_weight(row["date_rated"] or row["rating_updated_at"])
            # Extremes carry more information than a 5/6 near-neutral rating, but neutral ratings
            # still matter for calibration.
            extremity = 0.72 + 0.48 * abs(target)
            samples.append(
                _Sample(
                    movie=movie,
                    target=target,
                    weight=recency * extremity,
                    date_key=str(row["date_rated"] or row["rating_updated_at"] or ""),
                )
            )
        samples.sort(key=lambda s: s.date_key)
        return samples

    def _feedback_samples(self) -> list[_Sample]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.*,f.kind,f.weight AS feedback_weight,f.created_at AS feedback_created_at
                   FROM feedback f JOIN movies m ON m.id=f.movie_id
                   WHERE f.kind IN ('more_like_this','less_like_this','not_interested','never_similar')"""
            ).fetchall()
        out: list[_Sample] = []
        for row in rows:
            kind = str(row["kind"] or "")
            cfg = self._FEEDBACK_TARGETS.get(kind)
            if not cfg:
                continue
            target, base_weight = cfg
            explicit = max(0.35, min(1.5, abs(float(row["feedback_weight"] or 1.0))))
            out.append(
                _Sample(
                    movie=row_to_movie(row),
                    target=float(target),
                    weight=base_weight * explicit * self._recency_weight(row["feedback_created_at"]),
                    date_key=str(row["feedback_created_at"] or ""),
                )
            )
        return out

    def _fit(self, samples: list[_Sample], epochs: int = EPOCHS):
        weights = [0.0] * HASH_DIM
        support = [0.0] * HASH_DIM
        if not samples:
            return weights, support, 0.0

        total_w = sum(max(0.01, s.weight) for s in samples)
        mean_target = sum(s.target * max(0.01, s.weight) for s in samples) / max(0.01, total_w)
        mean_target = max(-0.85, min(0.85, mean_target))
        bias = math.atanh(mean_target)
        encoded = [(self._vector(s.movie)[0], s.target, max(0.05, s.weight)) for s in samples]

        for epoch in range(max(1, int(epochs))):
            lr = 0.055 / (1.0 + 0.24 * epoch)
            sequence = encoded if epoch % 2 == 0 else reversed(encoded)
            for vector, target, sample_weight in sequence:
                z = bias + sum(weights[idx] * value for idx, value in vector.items())
                z = max(-4.0, min(4.0, z))
                pred = math.tanh(z)
                grad = (target - pred) * (1.0 - pred * pred) * sample_weight
                bias += lr * grad * 0.20
                for idx, value in vector.items():
                    weights[idx] += lr * (grad * value - L2 * weights[idx])
                    if epoch == 0:
                        support[idx] += abs(value) * sample_weight
        return weights, support, bias

    @staticmethod
    def _predict_vector(vector: dict[int, float], weights: list[float], bias: float) -> float:
        z = bias + sum(weights[idx] * value for idx, value in vector.items())
        return math.tanh(max(-4.0, min(4.0, z)))

    def _validate(self, ratings: list[_Sample]) -> tuple[float | None, float | None, int, float]:
        if len(ratings) < max(MIN_RATINGS, VALIDATION_MIN * 3):
            return None, None, 0, 0.0
        cut = max(MIN_RATINGS, int(len(ratings) * 0.80))
        if len(ratings) - cut < VALIDATION_MIN:
            cut = len(ratings) - VALIDATION_MIN
        train = ratings[:cut]
        holdout = ratings[cut:]
        if len(train) < MIN_RATINGS or len(holdout) < VALIDATION_MIN:
            return None, None, 0, 0.0

        weights, _support, bias = self._fit(train, epochs=max(6, EPOCHS - 2))
        train_weight = sum(s.weight for s in train) or 1.0
        baseline_target = sum(s.target * s.weight for s in train) / train_weight
        model_abs = baseline_abs = 0.0
        holdout_weight = 0.0
        for sample in holdout:
            vector, _labels = self._vector(sample.movie)
            pred = self._predict_vector(vector, weights, bias)
            scale = max(0.05, sample.weight)
            model_abs += abs(self._target_to_rating(sample.target) - self._target_to_rating(pred)) * scale
            baseline_abs += abs(self._target_to_rating(sample.target) - self._target_to_rating(baseline_target)) * scale
            holdout_weight += scale
        if holdout_weight <= 0:
            return None, None, 0, 0.0
        model_mae = model_abs / holdout_weight
        baseline_mae = baseline_abs / holdout_weight
        gain = (baseline_mae - model_mae) / max(0.25, baseline_mae)
        return model_mae, baseline_mae, len(holdout), gain

    def _ensure(self) -> None:
        token = self.state_token()
        with self._lock:
            if token == self._token and self._status.get("state") == "ready":
                return

        ratings = self._rating_samples()
        feedback = self._feedback_samples()
        model_mae, baseline_mae, holdout_count, gain = self._validate(ratings)
        full = ratings + feedback
        if len(ratings) < MIN_RATINGS:
            with self._lock:
                self._token = token
                self._status = {
                    "state": "insufficient",
                    "version": MODEL_VERSION,
                    "training_ratings": len(ratings),
                    "training_feedback": len(feedback),
                    "holdout_count": holdout_count,
                    "model_mae": model_mae,
                    "baseline_mae": baseline_mae,
                    "validation_gain": gain,
                    "validated": False,
                    "max_blend_weight": 0.0,
                }
            return

        weights, support, bias = self._fit(full)
        validated = bool(model_mae is not None and baseline_mae is not None and model_mae <= baseline_mae * 1.02)
        # If validation is excellent, the personal model can become a major reranker. If it is
        # merely comparable to the baseline it still gets a small voice, never blind control.
        if model_mae is None:
            max_blend = 0.18
        elif validated:
            max_blend = max(0.20, min(0.40, 0.25 + max(-0.02, gain) * 0.9))
        else:
            max_blend = 0.08

        with self._lock:
            self._weights = weights
            self._support = support
            self._bias = bias
            self._token = token
            self._status = {
                "state": "ready",
                "version": MODEL_VERSION,
                "training_ratings": len(ratings),
                "training_feedback": len(feedback),
                "holdout_count": holdout_count,
                "model_mae": round(model_mae, 4) if model_mae is not None else None,
                "baseline_mae": round(baseline_mae, 4) if baseline_mae is not None else None,
                "validation_gain": round(gain, 5),
                "validated": validated,
                "max_blend_weight": round(max_blend, 4),
            }

    def status(self) -> dict:
        self._ensure()
        with self._lock:
            return dict(self._status)

    def score(self, movie) -> dict:
        self._ensure()
        with self._lock:
            status = dict(self._status)
            weights = self._weights
            support = self._support
            bias = self._bias
        if status.get("state") != "ready":
            return {
                "active": False,
                "predicted_rating": 0.0,
                "score": 0.0,
                "confidence": 0.0,
                "blend_weight": 0.0,
                "reasons": [],
            }

        vector, labels = self._vector(movie)
        if not vector:
            return {
                "active": False,
                "predicted_rating": 0.0,
                "score": 0.0,
                "confidence": 0.0,
                "blend_weight": 0.0,
                "reasons": [],
            }

        target = self._predict_vector(vector, weights, bias)
        predicted = self._target_to_rating(target)
        support_num = support_den = 0.0
        contributions: list[tuple[float, str]] = []
        for idx, value in vector.items():
            feature_support = 1.0 - math.exp(-support[idx] / 6.0)
            support_num += abs(value) * feature_support
            support_den += abs(value)
            for token, token_value in labels.get(idx, []):
                contributions.append((weights[idx] * token_value, token))
        evidence = support_num / max(0.001, support_den)
        sample_conf = 1.0 - math.exp(-float(status.get("training_ratings", 0)) / 350.0)
        validated = 1.0 if status.get("validated") else 0.55
        confidence = clamp(0.15 + 0.50 * evidence + 0.20 * sample_conf + 0.15 * validated)
        max_blend = float(status.get("max_blend_weight", 0.0) or 0.0)
        blend = max_blend * (0.45 + 0.55 * confidence)

        contributions.sort(key=lambda item: abs(item[0]), reverse=True)
        reasons = []
        seen = set()
        for contribution, token in contributions:
            if token in seen or token.startswith("meta:"):
                continue
            seen.add(token)
            reasons.append({"feature": token, "effect": float(contribution)})
            if len(reasons) >= 4:
                break

        return {
            "active": True,
            "predicted_rating": round(predicted, 4),
            "score": clamp((predicted - 1.0) / 9.0),
            "confidence": confidence,
            "blend_weight": max(0.0, min(0.42, blend)),
            "reasons": reasons,
        }
