from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
import zipfile

import numpy as np
import pandas as pd
import requests
from scipy.sparse import csr_matrix
from implicit.cpu.als import AlternatingLeastSquares


DATASET_URL = "https://files.grouplens.org/datasets/movielens/ml-32m.zip"
DATASET_NAME = "MovieLens 32M"
DATASET_RATINGS = 32_000_204
MODEL_VERSION = "ml32m-als-v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        with target.open("wb") as fh:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    fh.write(chunk)


def confidence_from_stars(stars: np.ndarray) -> np.ndarray:
    """Map MovieLens 0.5-5.0 explicit ratings to signed ALS confidence.

    3.0 is neutral and omitted. 3.5-5.0 are positive preferences; 0.5-2.5 are
    explicit dislikes. implicit ALS natively supports negative confidence values.
    """
    stars = stars.astype(np.float32, copy=False)
    out = np.zeros(stars.shape, dtype=np.float32)
    positive = stars >= 3.5
    negative = stars <= 2.5
    out[positive] = 1.0 + 2.0 * (stars[positive] - 3.5)  # 1..4
    out[negative] = -(1.0 + 2.0 * (2.5 - stars[negative]))  # -1..-5
    return out


def train(zip_path: Path, output: Path, metadata_path: Path, factors: int, regularization: float, iterations: int) -> dict:
    t0 = time.perf_counter()
    with zipfile.ZipFile(zip_path) as archive:
        with archive.open("ml-32m/links.csv") as fh:
            links = pd.read_csv(
                fh,
                usecols=["movieId", "imdbId"],
                dtype={"movieId": "int32", "imdbId": "float64"},
            )

        movie_ids = links["movieId"].to_numpy(dtype=np.int32, copy=True)
        imdb_ids = links["imdbId"].fillna(0).to_numpy(dtype=np.int64, copy=True)
        item_count = int(movie_ids.size)
        if item_count < 80_000:
            raise RuntimeError(f"Unexpected MovieLens item count: {item_count}")

        lookup = np.full(int(movie_ids.max()) + 1, -1, dtype=np.int32)
        lookup[movie_ids] = np.arange(item_count, dtype=np.int32)

        # Preallocate compact primitive arrays. This keeps the 32M-row conversion well below
        # the memory footprint of holding the complete CSV in a DataFrame.
        user_idx = np.empty(DATASET_RATINGS, dtype=np.int32)
        item_idx = np.empty(DATASET_RATINGS, dtype=np.int32)
        confidence = np.empty(DATASET_RATINGS, dtype=np.float32)
        written = 0
        raw_rows = 0
        max_user_id = 0
        positive_count = 0
        negative_count = 0

        with archive.open("ml-32m/ratings.csv") as fh:
            chunks = pd.read_csv(
                fh,
                usecols=["userId", "movieId", "rating"],
                dtype={"userId": "int32", "movieId": "int32", "rating": "float32"},
                chunksize=1_000_000,
            )
            for chunk in chunks:
                users = chunk["userId"].to_numpy(dtype=np.int32, copy=False)
                mids = chunk["movieId"].to_numpy(dtype=np.int32, copy=False)
                stars = chunk["rating"].to_numpy(dtype=np.float32, copy=False)
                raw_rows += int(users.size)
                if users.size:
                    max_user_id = max(max_user_id, int(users.max()))

                cols = lookup[mids]
                values = confidence_from_stars(stars)
                keep = (cols >= 0) & (values != 0)
                count = int(np.count_nonzero(keep))
                if not count:
                    continue
                end = written + count
                if end > DATASET_RATINGS:
                    raise RuntimeError("Interaction preallocation was too small")
                selected = values[keep]
                user_idx[written:end] = users[keep] - 1
                item_idx[written:end] = cols[keep]
                confidence[written:end] = selected
                positive_count += int(np.count_nonzero(selected > 0))
                negative_count += int(np.count_nonzero(selected < 0))
                written = end

    if raw_rows != DATASET_RATINGS:
        raise RuntimeError(f"Unexpected MovieLens rating count: {raw_rows:,}")
    if max_user_id < 190_000 or written < 25_000_000:
        raise RuntimeError(f"Unexpected training matrix size: users={max_user_id:,}, interactions={written:,}")

    matrix = csr_matrix(
        (confidence[:written], (user_idx[:written], item_idx[:written])),
        shape=(max_user_id, item_count),
        dtype=np.float32,
    )
    matrix.sum_duplicates()
    matrix.sort_indices()

    model = AlternatingLeastSquares(
        factors=int(factors),
        regularization=float(regularization),
        alpha=1.0,
        dtype=np.float32,
        use_native=True,
        use_cg=True,
        iterations=int(iterations),
        calculate_training_loss=False,
        num_threads=0,
        random_state=42,
    )
    fit_started = time.perf_counter()
    model.fit(matrix, show_progress=True)
    fit_seconds = time.perf_counter() - fit_started

    item_factors = np.asarray(model.item_factors, dtype=np.float32)
    if item_factors.shape != (item_count, int(factors)):
        raise RuntimeError(f"Unexpected factor shape: {item_factors.shape}")
    if not np.isfinite(item_factors).all():
        raise RuntimeError("ALS item factors contain NaN/Inf")

    # Sanity-check real recommendations from trained users before publishing the artifact.
    checked = 0
    unique_top = set()
    for user_id in np.linspace(0, max_user_id - 1, num=24, dtype=np.int32):
        row = matrix[int(user_id)]
        if row.nnz < 8:
            continue
        ids, scores = model.recommend(
            int(user_id), row, N=10, filter_already_liked_items=True, recalculate_user=False
        )
        if len(ids) != 10 or not np.isfinite(scores).all():
            raise RuntimeError("ALS recommendation sanity-check failed")
        unique_top.update(int(x) for x in ids[:3])
        checked += 1
    if checked < 10 or len(unique_top) < 10:
        raise RuntimeError("ALS sanity-check produced insufficient recommendation diversity")

    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output,
        item_factors=item_factors,
        movielens_movie_ids=movie_ids,
        imdb_ids=imdb_ids,
        factors=np.asarray([int(factors)], dtype=np.int32),
        regularization=np.asarray([float(regularization)], dtype=np.float32),
        iterations=np.asarray([int(iterations)], dtype=np.int32),
        training_users=np.asarray([max_user_id], dtype=np.int32),
        training_interactions=np.asarray([written], dtype=np.int64),
    )

    meta = {
        "model_version": MODEL_VERSION,
        "algorithm": "implicit Alternating Least Squares (Hu-Koren-Volinsky family)",
        "implementation": "implicit 0.7.3",
        "dataset": DATASET_NAME,
        "dataset_url": DATASET_URL,
        "raw_ratings": raw_rows,
        "training_users": max_user_id,
        "training_items": item_count,
        "training_interactions": written,
        "positive_interactions": positive_count,
        "negative_interactions": negative_count,
        "factors": int(factors),
        "regularization": float(regularization),
        "iterations": int(iterations),
        "fit_seconds": round(fit_seconds, 3),
        "total_seconds": round(time.perf_counter() - t0, 3),
        "artifact_sha256": sha256_file(output),
        "artifact_bytes": output.stat().st_size,
    }
    metadata_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--factors", type=int, default=64)
    parser.add_argument("--regularization", type=float, default=0.08)
    parser.add_argument("--iterations", type=int, default=15)
    args = parser.parse_args()

    output = Path(args.output).resolve()
    metadata = Path(args.metadata).resolve()
    with tempfile.TemporaryDirectory(prefix="cinecalendar-ml32m-") as td:
        zip_path = Path(td) / "ml-32m.zip"
        print(f"Downloading {DATASET_URL}", flush=True)
        download(DATASET_URL, zip_path)
        print(f"Downloaded {zip_path.stat().st_size:,} bytes", flush=True)
        meta = train(zip_path, output, metadata, args.factors, args.regularization, args.iterations)
    print(json.dumps(meta, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
