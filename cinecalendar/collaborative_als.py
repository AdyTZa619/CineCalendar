from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Iterable

import numpy as np
import requests
from scipy.sparse import csr_matrix
from implicit.cpu.als import AlternatingLeastSquares


MODEL_MANIFEST_URL = (
    "https://raw.githubusercontent.com/AdyTZa619/DuplicateDownloadGuard-Releases/"
    "cinecalendar-direct-exe/CineCalendar/collaborative-model.json"
)
MIN_MAPPED_RATINGS = 20


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _rating_confidence(rating: int) -> float:
    """Signed confidence around this user's meaningful like/dislike boundary."""
    rating = int(rating)
    if rating >= 7:
        return 0.75 + (rating - 7) * 1.0  # 7=.75 .. 10=3.75
    if rating <= 5:
        return -(0.75 + (5 - rating) * 0.65)  # 5=-.75 .. 1=-3.35
    return 0.0  # 6/10 is intentionally neutral


_FEEDBACK_CONFIDENCE = {
    "more_like_this": 1.50,
    "less_like_this": -1.25,
    "not_interested": -2.00,
    "never_similar": -3.00,
    "want_to_watch": 0.25,
}


class CollaborativeALSProvider:
    """MovieLens-trained implicit ALS model with local fold-in of private IMDb ratings.

    The shared model contains only MovieLens item factors and public IMDb mappings. CineCalendar
    never uploads personal ratings: the user's factor is recalculated locally from the SQLite
    ratings on every relevant state change.
    """

    def __init__(self, db):
        self.db = db
        self.cache_dir = Path(db.path).resolve().parent.parent / "cache" / "collaborative"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state = "not_started"
        self._error = ""
        self._version = ""
        self._manifest: dict = {}
        self._model: AlternatingLeastSquares | None = None
        self._imdb_to_item: dict[str, int] = {}
        self._item_to_imdb: np.ndarray | None = None
        self._history_token = None
        self._user_items: csr_matrix | None = None
        self._user_factor: np.ndarray | None = None
        self._mapped_ratings = 0

    def start_background(self) -> None:
        with self._lock:
            if self._state in {"loading", "ready"}:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._state = "loading"
            self._error = ""
            self._thread = threading.Thread(target=self._prepare, name="CineCalendar-ALS", daemon=True)
            self._thread.start()

    def state_token(self) -> tuple[str, str]:
        with self._lock:
            return self._state, self._version

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "version": self._version,
                "error": self._error,
                "mapped_ratings": int(self._mapped_ratings),
                "training_users": int(self._manifest.get("training_users", 0) or 0),
                "training_items": int(self._manifest.get("training_items", 0) or 0),
                "algorithm": self._manifest.get("algorithm", "implicit ALS"),
                "dataset": self._manifest.get("dataset", "MovieLens 32M"),
            }

    def is_ready(self) -> bool:
        with self._lock:
            return self._state == "ready" and self._model is not None

    @staticmethod
    def _fetch_manifest() -> dict:
        response = requests.get(
            MODEL_MANIFEST_URL,
            timeout=(10, 30),
            headers={"Cache-Control": "no-cache", "User-Agent": "CineCalendar/2.4"},
        )
        response.raise_for_status()
        payload = response.json()
        required = ("version", "url", "sha256", "factors", "regularization")
        if any(not payload.get(k) for k in required):
            raise RuntimeError("Manifestul modelului colaborativ este incomplet.")
        if payload.get("dataset") != "MovieLens 32M":
            raise RuntimeError("Sursa modelului colaborativ nu este MovieLens 32M.")
        return payload

    @staticmethod
    def _download(url: str, target: Path) -> None:
        partial = target.with_suffix(target.suffix + ".download")
        partial.unlink(missing_ok=True)
        with requests.get(url, stream=True, timeout=(15, 120), headers={"User-Agent": "CineCalendar/2.4"}) as response:
            response.raise_for_status()
            with partial.open("wb") as fh:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        partial.replace(target)

    def _prepare(self) -> None:
        try:
            manifest = self._fetch_manifest()
            version = str(manifest["version"])
            safe_version = "".join(c for c in version if c.isalnum() or c in "-_.")
            model_path = self.cache_dir / f"{safe_version}.npz"
            expected = str(manifest["sha256"]).lower()
            if not model_path.is_file() or _sha256(model_path).lower() != expected:
                model_path.unlink(missing_ok=True)
                self._download(str(manifest["url"]), model_path)
                got = _sha256(model_path).lower()
                if got != expected:
                    model_path.unlink(missing_ok=True)
                    raise RuntimeError("SHA-256 invalid pentru modelul colaborativ descărcat.")

            with np.load(model_path, allow_pickle=False) as payload:
                item_factors = np.asarray(payload["item_factors"], dtype=np.float32)
                imdb_ids = np.asarray(payload["imdb_ids"], dtype=np.int64)
                factors = int(np.asarray(payload["factors"]).reshape(-1)[0])
                regularization = float(np.asarray(payload["regularization"]).reshape(-1)[0])

            if item_factors.ndim != 2 or item_factors.shape[1] != factors:
                raise RuntimeError("Dimensiunile modelului ALS sunt invalide.")
            if item_factors.shape[0] != imdb_ids.shape[0] or item_factors.shape[0] < 80_000:
                raise RuntimeError("Maparea MovieLens/IMDb este invalidă.")
            if not np.isfinite(item_factors).all():
                raise RuntimeError("Modelul ALS conține valori invalide.")

            model = AlternatingLeastSquares(
                factors=factors,
                regularization=regularization,
                alpha=1.0,
                dtype=np.float32,
                use_native=True,
                use_cg=True,
                iterations=0,
                calculate_training_loss=False,
                num_threads=1,
                random_state=42,
            )
            model.item_factors = item_factors
            # recommend(..., recalculate_user=True) and explain() need only item factors/YtY,
            # but a tiny placeholder keeps the model object complete for library internals.
            model.user_factors = np.zeros((1, factors), dtype=np.float32)

            mapping = {
                f"tt{int(imdb_num):07d}": int(index)
                for index, imdb_num in enumerate(imdb_ids)
                if int(imdb_num) > 0
            }
            with self._lock:
                self._manifest = manifest
                self._version = version
                self._model = model
                self._imdb_to_item = mapping
                self._item_to_imdb = imdb_ids
                self._history_token = None
                self._user_items = None
                self._user_factor = None
                self._state = "ready"
                self._error = ""
        except Exception as exc:
            with self._lock:
                self._state = "error"
                self._error = str(exc)

    def _db_history_token(self):
        with self.db.connect() as con:
            rating = con.execute("SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM ratings").fetchone()
            feedback = con.execute("SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback").fetchone()
        return int(rating[0]), str(rating[1]), int(feedback[0]), str(feedback[1])

    def _build_user_representation(self) -> tuple[csr_matrix | None, np.ndarray | None, int]:
        with self._lock:
            model = self._model
            mapping = self._imdb_to_item
            if self._state != "ready" or model is None or not mapping:
                return None, None, 0

        token = self._db_history_token()
        with self._lock:
            if token == self._history_token and self._user_items is not None and self._user_factor is not None:
                return self._user_items, self._user_factor, self._mapped_ratings

        with self.db.connect() as con:
            ratings = con.execute(
                """SELECT m.imdb_id,r.rating
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   WHERE m.imdb_id IS NOT NULL"""
            ).fetchall()
            feedback = con.execute(
                """SELECT m.imdb_id,f.kind
                   FROM feedback f JOIN movies m ON m.id=f.movie_id
                   WHERE m.imdb_id IS NOT NULL"""
            ).fetchall()

        values: dict[int, float] = {}
        mapped_ratings = 0
        for row in ratings:
            item = mapping.get(str(row["imdb_id"] or ""))
            if item is None:
                continue
            mapped_ratings += 1
            conf = _rating_confidence(int(row["rating"]))
            if conf:
                values[item] = conf

        for row in feedback:
            item = mapping.get(str(row["imdb_id"] or ""))
            if item is None:
                continue
            adjustment = _FEEDBACK_CONFIDENCE.get(str(row["kind"] or ""), 0.0)
            if adjustment:
                values[item] = max(-5.0, min(5.0, values.get(item, 0.0) + adjustment))

        if mapped_ratings < MIN_MAPPED_RATINGS or not values:
            with self._lock:
                self._history_token = token
                self._user_items = None
                self._user_factor = None
                self._mapped_ratings = mapped_ratings
            return None, None, mapped_ratings

        indices = np.fromiter(values.keys(), dtype=np.int32, count=len(values))
        data = np.fromiter((values[int(i)] for i in indices), dtype=np.float32, count=len(values))
        order = np.argsort(indices)
        indices = indices[order]
        data = data[order]
        indptr = np.asarray([0, len(indices)], dtype=np.int32)
        user_items = csr_matrix((data, indices, indptr), shape=(1, model.item_factors.shape[0]), dtype=np.float32)
        user_factor = np.asarray(model.recalculate_user(0, user_items), dtype=np.float32)
        if not np.isfinite(user_factor).all():
            return None, None, mapped_ratings

        with self._lock:
            self._history_token = token
            self._user_items = user_items
            self._user_factor = user_factor
            self._mapped_ratings = mapped_ratings
        return user_items, user_factor, mapped_ratings

    def score_candidates(self, imdb_ids: Iterable[str]) -> tuple[dict[str, float], dict[str, float], int]:
        """Return percentile-normalized ALS scores, raw scores and mapped personal-rating count."""
        if not self.is_ready():
            return {}, {}, 0
        user_items, user_factor, mapped_ratings = self._build_user_representation()
        if user_items is None or user_factor is None:
            return {}, {}, mapped_ratings

        with self._lock:
            model = self._model
            mapping = self._imdb_to_item
        if model is None:
            return {}, {}, mapped_ratings

        pairs: list[tuple[str, int]] = []
        seen_items: set[int] = set()
        for imdb_id in imdb_ids:
            iid = str(imdb_id or "")
            item = mapping.get(iid)
            if item is None or item in seen_items:
                continue
            seen_items.add(item)
            pairs.append((iid, item))
        if not pairs:
            return {}, {}, mapped_ratings

        item_indices = np.asarray([item for _iid, item in pairs], dtype=np.int32)
        raw_values = np.asarray(model.item_factors[item_indices].dot(user_factor), dtype=np.float64)
        finite = np.isfinite(raw_values)
        if not finite.any():
            return {}, {}, mapped_ratings

        valid_positions = np.flatnonzero(finite)
        valid_raw = raw_values[valid_positions]
        order = np.argsort(valid_raw, kind="mergesort")
        ranks = np.empty(order.size, dtype=np.float64)
        ranks[order] = np.arange(order.size, dtype=np.float64)
        percentiles = (ranks + 1.0) / (order.size + 1.0)

        normalized: dict[str, float] = {}
        raw: dict[str, float] = {}
        for local_pos, source_pos in enumerate(valid_positions):
            iid = pairs[int(source_pos)][0]
            normalized[iid] = float(percentiles[local_pos])
            raw[iid] = float(raw_values[int(source_pos)])
        return normalized, raw, mapped_ratings

    def explain(self, imdb_id: str, limit: int = 3) -> list[tuple[str, int, float]]:
        """Return the user's rated movies contributing most to an ALS recommendation."""
        if not self.is_ready():
            return []
        user_items, _factor, _mapped = self._build_user_representation()
        if user_items is None:
            return []
        with self._lock:
            model = self._model
            item = self._imdb_to_item.get(str(imdb_id or ""))
            item_to_imdb = self._item_to_imdb
        if model is None or item is None or item_to_imdb is None:
            return []
        try:
            _total, contributions, _weights = model.explain(0, user_items, int(item), N=max(1, int(limit)))
        except Exception:
            return []

        source_ids = []
        score_by_imdb: dict[str, float] = {}
        for source_item, contribution in contributions:
            imdb_num = int(item_to_imdb[int(source_item)])
            if imdb_num <= 0:
                continue
            iid = f"tt{imdb_num:07d}"
            source_ids.append(iid)
            score_by_imdb[iid] = float(contribution)
        if not source_ids:
            return []

        marks = ",".join("?" for _ in source_ids)
        with self.db.connect() as con:
            rows = con.execute(
                f"""SELECT m.imdb_id,m.title,r.rating
                    FROM movies m JOIN ratings r ON r.movie_id=m.id
                    WHERE m.imdb_id IN ({marks})""",
                tuple(source_ids),
            ).fetchall()
        row_map = {str(row["imdb_id"]): (str(row["title"]), int(row["rating"])) for row in rows}
        out = []
        for iid in source_ids:
            if iid in row_map:
                title, rating = row_map[iid]
                out.append((title, rating, score_by_imdb.get(iid, 0.0)))
        return out[:limit]
