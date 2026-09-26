from __future__ import annotations

import math
import threading

from .adaptive_preferences_v2 import AdaptivePreferenceLearnerV2, HASH_DIM
from .util import clamp


V5_RANKER_VERSION = "v5-personal-utility-alpha1"
MIN_RATINGS = 180
MIN_HOLDOUT = 60
EPOCHS = 7
L2 = 0.0007


class PersonalUtilityRankerV5:
    """Local binary utility model optimized for the recommendation decision itself.

    It learns two independent questions from the user's own timestamped ratings:
      * probability-like score for rating >= 8;
      * risk score for rating <= 4.

    The model is allowed to influence ranking only when a temporal holdout shows useful ranking
    skill. It is a laboratory ranker: its output is not presented as a calibrated probability.
    """

    def __init__(self, db):
        self.db = db
        self.encoder = AdaptivePreferenceLearnerV2(db)
        self._lock = threading.RLock()
        self._token = None
        self._like_weights = [0.0] * HASH_DIM
        self._dislike_weights = [0.0] * HASH_DIM
        self._like_bias = 0.0
        self._dislike_bias = 0.0
        self._status = {
            "version": V5_RANKER_VERSION,
            "state": "not_trained",
            "validated": False,
            "blend_weight": 0.0,
        }

    def state_token(self):
        with self.db.connect() as con:
            row = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),''),COALESCE(MAX(date_rated),'') FROM ratings"
            ).fetchone()
        return V5_RANKER_VERSION, int(row[0] or 0), str(row[1] or ""), str(row[2] or "")

    @staticmethod
    def _sigmoid(z: float) -> float:
        z = max(-12.0, min(12.0, float(z)))
        return 1.0 / (1.0 + math.exp(-z))

    def _fit_binary(self, encoded, targets, sample_weights):
        weights = [0.0] * HASH_DIM
        if not encoded:
            return weights, 0.0
        total_w = sum(sample_weights) or 1.0
        rate = sum(float(y) * w for y, w in zip(targets, sample_weights)) / total_w
        rate = min(0.95, max(0.05, rate))
        bias = math.log(rate / (1.0 - rate))
        for epoch in range(EPOCHS):
            lr = 0.045 / (1.0 + 0.28 * epoch)
            order = range(len(encoded)) if epoch % 2 == 0 else range(len(encoded) - 1, -1, -1)
            for i in order:
                vector = encoded[i]
                target = float(targets[i])
                sw = float(sample_weights[i])
                z = bias + sum(weights[idx] * value for idx, value in vector.items())
                pred = self._sigmoid(z)
                grad = (target - pred) * sw
                bias += lr * grad * 0.12
                for idx, value in vector.items():
                    weights[idx] += lr * (grad * value - L2 * weights[idx])
        return weights, bias

    @staticmethod
    def _auc(actual: list[int], predicted: list[float]) -> float | None:
        positives = [p for y, p in zip(actual, predicted) if int(y) == 1]
        negatives = [p for y, p in zip(actual, predicted) if int(y) == 0]
        if not positives or not negatives:
            return None
        wins = ties = 0.0
        total = len(positives) * len(negatives)
        for pos in positives:
            for neg in negatives:
                if pos > neg:
                    wins += 1.0
                elif pos == neg:
                    ties += 1.0
        return (wins + 0.5 * ties) / total

    @staticmethod
    def _ndcg25(ratings: list[int], predicted: list[float]) -> float:
        order = sorted(range(len(predicted)), key=lambda i: predicted[i], reverse=True)[:25]
        ideal = sorted(range(len(ratings)), key=lambda i: ratings[i], reverse=True)[:25]

        def gain(rating: int) -> float:
            return max(0.0, float(rating) - 5.0)

        def dcg(indices):
            total = 0.0
            for rank, idx in enumerate(indices, 1):
                rel = gain(ratings[idx])
                if rel > 0:
                    total += (2.0 ** rel - 1.0) / math.log2(rank + 1.0)
            return total

        denom = dcg(ideal)
        return dcg(order) / denom if denom > 0 else 0.0

    @staticmethod
    def _top_like_rate(actual_like: list[int], predicted: list[float], share: float = 0.20) -> float:
        if not actual_like:
            return 0.0
        take = max(1, int(round(len(actual_like) * float(share))))
        order = sorted(range(len(predicted)), key=lambda i: predicted[i], reverse=True)[:take]
        return sum(actual_like[i] for i in order) / float(len(order))

    def _train(self):
        samples = self.encoder._rating_samples()
        if len(samples) < MIN_RATINGS:
            return None
        cut = max(MIN_RATINGS, int(len(samples) * 0.80))
        if len(samples) - cut < MIN_HOLDOUT:
            cut = len(samples) - MIN_HOLDOUT
        train = samples[:cut]
        holdout = samples[cut:]
        if len(train) < MIN_RATINGS or len(holdout) < MIN_HOLDOUT:
            return None

        train_vectors = [self.encoder._vector(sample.movie)[0] for sample in train]
        train_ratings = [int(round(self.encoder._target_to_rating(sample.target))) for sample in train]
        train_weights = [max(0.25, float(sample.weight)) for sample in train]
        like_targets = [1 if rating >= 8 else 0 for rating in train_ratings]
        dislike_targets = [1 if rating <= 4 else 0 for rating in train_ratings]
        like_weights, like_bias = self._fit_binary(train_vectors, like_targets, train_weights)
        dislike_weights, dislike_bias = self._fit_binary(train_vectors, dislike_targets, train_weights)

        hold_vectors = [self.encoder._vector(sample.movie)[0] for sample in holdout]
        hold_ratings = [int(round(self.encoder._target_to_rating(sample.target))) for sample in holdout]
        like_actual = [1 if rating >= 8 else 0 for rating in hold_ratings]
        dislike_actual = [1 if rating <= 4 else 0 for rating in hold_ratings]
        like_pred = [self._predict(v, like_weights, like_bias) for v in hold_vectors]
        dislike_pred = [self._predict(v, dislike_weights, dislike_bias) for v in hold_vectors]
        utility = [clamp(lp - 0.70 * dp) for lp, dp in zip(like_pred, dislike_pred)]
        public = [float(sample.movie.imdb_rating or 0.0) for sample in holdout]

        like_auc = self._auc(like_actual, like_pred)
        dislike_auc = self._auc(dislike_actual, dislike_pred)
        model_ndcg = self._ndcg25(hold_ratings, utility)
        public_ndcg = self._ndcg25(hold_ratings, public)
        base_rate = sum(like_actual) / len(like_actual)
        top_rate = self._top_like_rate(like_actual, utility)
        lift = (top_rate / base_rate) if base_rate > 0 else 0.0
        validated = bool(
            like_auc is not None and like_auc >= 0.58
            and model_ndcg >= public_ndcg - 0.01
            and lift >= 1.18
            and (dislike_auc is None or dislike_auc >= 0.54)
        )
        quality = max(0.0, min(1.0, ((like_auc or 0.5) - 0.5) / 0.25))
        blend = min(0.07, 0.035 + 0.035 * quality) if validated else 0.0
        return {
            "like_weights": like_weights,
            "dislike_weights": dislike_weights,
            "like_bias": like_bias,
            "dislike_bias": dislike_bias,
            "status": {
                "version": V5_RANKER_VERSION,
                "state": "ready",
                "training_count": len(train),
                "holdout_count": len(holdout),
                "holdout_like_count": sum(like_actual),
                "holdout_dislike_count": sum(dislike_actual),
                "like_auc": round(like_auc, 6) if like_auc is not None else None,
                "dislike_auc": round(dislike_auc, 6) if dislike_auc is not None else None,
                "model_ndcg25": round(model_ndcg, 6),
                "public_ndcg25": round(public_ndcg, 6),
                "top20_like_rate": round(top_rate, 6),
                "holdout_like_rate": round(base_rate, 6),
                "top20_lift": round(lift, 6),
                "validated": validated,
                "blend_weight": round(blend, 6),
            },
        }

    @classmethod
    def _predict(cls, vector, weights, bias):
        return cls._sigmoid(bias + sum(weights[idx] * value for idx, value in vector.items()))

    def _ensure(self):
        token = self.state_token()
        with self._lock:
            if token == self._token and self._status.get("state") == "ready":
                return
        trained = self._train()
        with self._lock:
            self._token = token
            if trained is None:
                self._status = {
                    "version": V5_RANKER_VERSION,
                    "state": "insufficient",
                    "validated": False,
                    "blend_weight": 0.0,
                }
                return
            self._like_weights = trained["like_weights"]
            self._dislike_weights = trained["dislike_weights"]
            self._like_bias = trained["like_bias"]
            self._dislike_bias = trained["dislike_bias"]
            self._status = trained["status"]

    def score(self, movie) -> dict:
        self._ensure()
        with self._lock:
            status = dict(self._status)
            if not bool(status.get("validated")):
                return {"active": False, **status}
            like_weights = self._like_weights
            dislike_weights = self._dislike_weights
            like_bias = self._like_bias
            dislike_bias = self._dislike_bias
        vector = self.encoder._vector(movie)[0]
        like = self._predict(vector, like_weights, like_bias)
        dislike = self._predict(vector, dislike_weights, dislike_bias)
        raw = like - 0.70 * dislike
        normalized = clamp((raw + 0.70) / 1.70)
        return {
            "active": True,
            "like_score": like,
            "dislike_risk": dislike,
            "utility": normalized,
            "blend_weight": float(status.get("blend_weight", 0.0) or 0.0),
        }

    def status(self) -> dict:
        self._ensure()
        with self._lock:
            return dict(self._status)
