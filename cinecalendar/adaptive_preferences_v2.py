from __future__ import annotations

from datetime import date
import hashlib
import math

from .adaptive_preferences import AdaptivePreferenceLearner
from .util import clamp


MODEL_VERSION = "adaptive-personal-v2-ranking-gated"
HASH_DIM = 65_536
MIN_RATINGS = 80
VALIDATION_MIN = 30
EPOCH_CANDIDATES = (5, 7, 9)
DEFAULT_EPOCHS = 7
L2 = 0.0008
PAIRWISE_MAX_SAMPLES = 700


class AdaptivePreferenceLearnerV2(AdaptivePreferenceLearner):
    """Personal model whose influence is earned on a recent temporal holdout.

    V1 validated only weighted MAE against a constant user-mean baseline. That is useful for
    calibration but insufficient for a recommender: the model can predict ratings reasonably and
    still order the films badly. V2 therefore validates both calibration and ranking before it is
    allowed a large reranking voice.

    Validation is entirely local. The oldest ~80% of ratings train candidate models and the newest
    ~20% are held out. The baseline is deliberately harder than a constant mean: when enough IMDb
    ratings are present it uses IMDb + this user's historical user-minus-IMDb offset. Candidate
    epoch counts are selected by a composite of weighted MAE, NDCG@10 and pairwise ordering.
    """

    def __init__(self, db):
        self.db = db
        import threading
        self._lock = threading.RLock()
        self._token = None
        self._weights = [0.0] * HASH_DIM
        self._support = [0.0] * HASH_DIM
        self._bias = 0.0
        self._status = self._empty_status("not_trained")

    @staticmethod
    def _empty_status(state: str) -> dict:
        return {
            "state": state,
            "version": MODEL_VERSION,
            "hash_dim": HASH_DIM,
            "selected_epochs": DEFAULT_EPOCHS,
            "training_ratings": 0,
            "training_feedback": 0,
            "holdout_count": 0,
            "baseline_kind": "unavailable",
            "model_mae": None,
            "baseline_mae": None,
            "validation_gain": 0.0,
            "model_ndcg10": None,
            "baseline_ndcg10": None,
            "ndcg_gain": 0.0,
            "model_pairwise": None,
            "baseline_pairwise": None,
            "pairwise_gain": 0.0,
            "validation_quality": 0.0,
            "validated": False,
            "max_blend_weight": 0.0,
        }

    @staticmethod
    def _hash(token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8", "ignore"), digest_size=8).digest()
        return int.from_bytes(digest, "little", signed=False) % HASH_DIM

    def state_token(self):
        with self.db.connect() as con:
            rating = con.execute("SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings").fetchone()
            feedback = con.execute("SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback").fetchone()
        return MODEL_VERSION, int(rating[0]), str(rating[1]), int(feedback[0]), str(feedback[1])

    def _fit(self, samples, epochs: int = DEFAULT_EPOCHS):
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
    def _weighted_mae(actual: list[float], predicted: list[float], weights: list[float]) -> float | None:
        if not actual or len(actual) != len(predicted) or len(actual) != len(weights):
            return None
        total = sum(max(0.05, float(w)) for w in weights)
        if total <= 0:
            return None
        return sum(
            abs(float(a) - float(p)) * max(0.05, float(w))
            for a, p, w in zip(actual, predicted, weights)
        ) / total

    @staticmethod
    def _ndcg_at_10(actual: list[float], predicted: list[float]) -> float | None:
        if not actual or len(actual) != len(predicted):
            return None

        def relevance(rating: float) -> float:
            # A recommendation metric should care mostly about films the user would be happy to
            # receive. 5/10 and below therefore have zero gain; 6-10 rise smoothly.
            return max(0.0, min(5.0, float(rating) - 5.0))

        order = sorted(range(len(predicted)), key=lambda i: (float(predicted[i]), -i), reverse=True)[:10]
        ideal = sorted(range(len(actual)), key=lambda i: (float(actual[i]), -i), reverse=True)[:10]

        def dcg(indices: list[int]) -> float:
            return sum(relevance(actual[idx]) / math.log2(rank + 2.0) for rank, idx in enumerate(indices))

        ideal_dcg = dcg(ideal)
        if ideal_dcg <= 1e-12:
            return None
        return clamp(dcg(order) / ideal_dcg)

    @staticmethod
    def _pairwise_accuracy(actual: list[float], predicted: list[float]) -> float | None:
        if not actual or len(actual) != len(predicted):
            return None
        # Bound quadratic validation cost on very large personal histories while preserving the
        # most recent holdout evidence.
        if len(actual) > PAIRWISE_MAX_SAMPLES:
            actual = actual[-PAIRWISE_MAX_SAMPLES:]
            predicted = predicted[-PAIRWISE_MAX_SAMPLES:]

        correct = 0.0
        compared = 0
        for i in range(len(actual)):
            for j in range(i + 1, len(actual)):
                delta = float(actual[i]) - float(actual[j])
                if abs(delta) < 2.0:
                    continue
                pred_delta = float(predicted[i]) - float(predicted[j])
                compared += 1
                if pred_delta == 0:
                    correct += 0.5
                elif (delta > 0) == (pred_delta > 0):
                    correct += 1.0
        if compared <= 0:
            return None
        return correct / compared

    def _baseline(self, train, holdout) -> tuple[list[float], str]:
        train_weights = [max(0.05, float(s.weight)) for s in train]
        total = sum(train_weights) or 1.0
        mean_rating = sum(self._target_to_rating(s.target) * w for s, w in zip(train, train_weights)) / total

        imdb_diffs: list[tuple[float, float]] = []
        for sample, weight in zip(train, train_weights):
            public = getattr(sample.movie, "imdb_rating", None)
            if public is None:
                continue
            imdb_diffs.append((self._target_to_rating(sample.target) - float(public), weight))

        if len(imdb_diffs) >= 20:
            diff_weight = sum(weight for _diff, weight in imdb_diffs) or 1.0
            offset = sum(diff * weight for diff, weight in imdb_diffs) / diff_weight
            predictions = []
            for sample in holdout:
                public = getattr(sample.movie, "imdb_rating", None)
                if public is None:
                    predictions.append(max(1.0, min(10.0, mean_rating)))
                else:
                    predictions.append(max(1.0, min(10.0, float(public) + offset)))
            return predictions, "calibrated_imdb"

        return [max(1.0, min(10.0, mean_rating)) for _sample in holdout], "user_mean"

    @staticmethod
    def _gain(baseline: float | None, model: float | None, lower_is_better: bool = False) -> float:
        if baseline is None or model is None:
            return 0.0
        if lower_is_better:
            return (float(baseline) - float(model)) / max(0.25, abs(float(baseline)))
        return float(model) - float(baseline)

    def _validate(self, ratings) -> dict:
        empty = {
            "available": False,
            "holdout_count": 0,
            "selected_epochs": DEFAULT_EPOCHS,
            "baseline_kind": "unavailable",
            "model_mae": None,
            "baseline_mae": None,
            "validation_gain": 0.0,
            "model_ndcg10": None,
            "baseline_ndcg10": None,
            "ndcg_gain": 0.0,
            "model_pairwise": None,
            "baseline_pairwise": None,
            "pairwise_gain": 0.0,
            "validation_quality": 0.0,
            "validated": False,
        }
        if len(ratings) < max(MIN_RATINGS, VALIDATION_MIN * 3):
            return empty

        cut = max(MIN_RATINGS, int(len(ratings) * 0.80))
        if len(ratings) - cut < VALIDATION_MIN:
            cut = len(ratings) - VALIDATION_MIN
        train = ratings[:cut]
        holdout = ratings[cut:]
        if len(train) < MIN_RATINGS or len(holdout) < VALIDATION_MIN:
            return empty

        actual = [self._target_to_rating(s.target) for s in holdout]
        scales = [max(0.05, float(s.weight)) for s in holdout]
        baseline_pred, baseline_kind = self._baseline(train, holdout)
        baseline_mae = self._weighted_mae(actual, baseline_pred, scales)
        baseline_ndcg = self._ndcg_at_10(actual, baseline_pred)
        baseline_pair = self._pairwise_accuracy(actual, baseline_pred)

        candidates: list[dict] = []
        for epochs in EPOCH_CANDIDATES:
            weights, _support, bias = self._fit(train, epochs=epochs)
            model_pred = [
                self._target_to_rating(self._predict_vector(self._vector(sample.movie)[0], weights, bias))
                for sample in holdout
            ]
            model_mae = self._weighted_mae(actual, model_pred, scales)
            model_ndcg = self._ndcg_at_10(actual, model_pred)
            model_pair = self._pairwise_accuracy(actual, model_pred)
            mae_gain = self._gain(baseline_mae, model_mae, lower_is_better=True)
            ndcg_gain = self._gain(baseline_ndcg, model_ndcg)
            pair_gain = self._gain(baseline_pair, model_pair)
            composite = 0.45 * mae_gain + 0.35 * ndcg_gain + 0.20 * pair_gain
            candidates.append({
                "epochs": epochs,
                "model_mae": model_mae,
                "model_ndcg10": model_ndcg,
                "model_pairwise": model_pair,
                "validation_gain": mae_gain,
                "ndcg_gain": ndcg_gain,
                "pairwise_gain": pair_gain,
                "composite": composite,
            })

        # Prefer the simpler model if two epoch counts are effectively tied.
        best = max(candidates, key=lambda item: (round(float(item["composite"]), 6), -int(item["epochs"])))
        model_mae = best["model_mae"]
        model_ndcg = best["model_ndcg10"]
        model_pair = best["model_pairwise"]
        mae_gain = float(best["validation_gain"])
        ndcg_gain = float(best["ndcg_gain"])
        pair_gain = float(best["pairwise_gain"])

        mae_safe = model_mae is not None and baseline_mae is not None and model_mae <= baseline_mae * 1.015
        ndcg_safe = (
            True if model_ndcg is None or baseline_ndcg is None
            else model_ndcg >= baseline_ndcg - 0.015
        )
        pair_safe = (
            True if model_pair is None or baseline_pair is None
            else model_pair >= baseline_pair - 0.020
        )
        meaningful = mae_gain >= 0.020 or ndcg_gain >= 0.020 or pair_gain >= 0.015
        validated = bool(mae_safe and ndcg_safe and pair_safe and meaningful)
        composite = float(best["composite"])
        quality = clamp(0.50 + 1.50 * composite) if validated else clamp(0.35 + max(0.0, composite))

        return {
            "available": True,
            "holdout_count": len(holdout),
            "selected_epochs": int(best["epochs"]),
            "baseline_kind": baseline_kind,
            "model_mae": model_mae,
            "baseline_mae": baseline_mae,
            "validation_gain": mae_gain,
            "model_ndcg10": model_ndcg,
            "baseline_ndcg10": baseline_ndcg,
            "ndcg_gain": ndcg_gain,
            "model_pairwise": model_pair,
            "baseline_pairwise": baseline_pair,
            "pairwise_gain": pair_gain,
            "validation_quality": quality,
            "validated": validated,
            "composite": composite,
        }

    def _ensure(self) -> None:
        token = self.state_token()
        with self._lock:
            if token == self._token and self._status.get("state") == "ready":
                return

        ratings = self._rating_samples()
        feedback = self._feedback_samples()
        validation = self._validate(ratings)
        full = ratings + feedback

        if len(ratings) < MIN_RATINGS:
            status = self._empty_status("insufficient")
            status["training_ratings"] = len(ratings)
            status["training_feedback"] = len(feedback)
            with self._lock:
                self._token = token
                self._status = status
            return

        selected_epochs = int(validation.get("selected_epochs", DEFAULT_EPOCHS) or DEFAULT_EPOCHS)
        weights, support, bias = self._fit(full, epochs=selected_epochs)

        if not validation.get("available"):
            max_blend = 0.12
        elif validation.get("validated"):
            composite = max(0.0, float(validation.get("composite", 0.0) or 0.0))
            max_blend = min(0.36, 0.22 + 0.70 * composite)
        else:
            max_blend = 0.06

        status = self._empty_status("ready")
        status.update({
            "training_ratings": len(ratings),
            "training_feedback": len(feedback),
            "holdout_count": int(validation.get("holdout_count", 0) or 0),
            "selected_epochs": selected_epochs,
            "baseline_kind": str(validation.get("baseline_kind", "unavailable")),
            "model_mae": self._rounded(validation.get("model_mae"), 4),
            "baseline_mae": self._rounded(validation.get("baseline_mae"), 4),
            "validation_gain": round(float(validation.get("validation_gain", 0.0) or 0.0), 5),
            "model_ndcg10": self._rounded(validation.get("model_ndcg10"), 5),
            "baseline_ndcg10": self._rounded(validation.get("baseline_ndcg10"), 5),
            "ndcg_gain": round(float(validation.get("ndcg_gain", 0.0) or 0.0), 5),
            "model_pairwise": self._rounded(validation.get("model_pairwise"), 5),
            "baseline_pairwise": self._rounded(validation.get("baseline_pairwise"), 5),
            "pairwise_gain": round(float(validation.get("pairwise_gain", 0.0) or 0.0), 5),
            "validation_quality": round(float(validation.get("validation_quality", 0.0) or 0.0), 5),
            "validated": bool(validation.get("validated")),
            "max_blend_weight": round(float(max_blend), 4),
        })

        with self._lock:
            self._weights = weights
            self._support = support
            self._bias = bias
            self._token = token
            self._status = status

    @staticmethod
    def _rounded(value, digits: int):
        return round(float(value), digits) if value is not None else None
