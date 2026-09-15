from __future__ import annotations
from collections import defaultdict
from datetime import date, datetime
import math
from .db import Database
from .models import Movie
from .semantic import extract_semantic, feature_vector
from .util import json_dumps, json_loads, utcnow_iso

RATING_SIGNAL = {1:-1.0,2:-.82,3:-.62,4:-.38,5:-.14,6:.08,7:.34,8:.62,9:.84,10:1.0}
PROFILE_VERSION = 2


def _recency_weight(date_rated: str | None, today: date | None = None) -> float:
    today = today or date.today()
    if not date_rated:
        return 1.0
    try:
        d = datetime.strptime(date_rated[:10], "%Y-%m-%d").date()
    except ValueError:
        return 1.0
    days = max(0, (today - d).days)
    return 1.0 + 0.15 * math.exp(-days / 730.0)


def _shrink(sum_signal: float, weight: float, prior_strength: float = 4.0) -> float:
    return sum_signal / (weight + prior_strength) if weight > 0 else 0.0


def _normalize_vector(values: dict[str, float], floor: float = .0015) -> dict[str, float]:
    norm = math.sqrt(sum(v*v for v in values.values())) or 1.0
    return {k: round(v/norm, 6) for k, v in values.items() if abs(v/norm) >= floor}


def _movie_from_row(row) -> Movie:
    return Movie(
        id=row["id"], imdb_id=row["imdb_id"], title=row["title"], original_title=row["original_title"] or "",
        year=row["year"], title_type=row["title_type"] or "Movie", runtime_min=row["runtime_min"],
        genres=json_loads(row["genres_json"], []), directors=json_loads(row["directors_json"], []),
        countries=json_loads(row["countries_json"], []), overview=row["overview"] or "", keywords=json_loads(row["keywords_json"], []),
        imdb_rating=row["imdb_rating"], num_votes=row["num_votes"], release_date=row["release_date"], poster_url=row["poster_url"],
        source=row["source"], semantic=json_loads(row["semantic_json"], {}) or {},
    )


def build_profile(db: Database) -> dict:
    aggregates: dict[str, list[float]] = defaultdict(lambda: [0.0,0.0,0.0,0.0,0.0])
    signed_vector: dict[str,float] = defaultdict(float)
    positive_vector: dict[str,float] = defaultdict(float)
    elite_vector: dict[str,float] = defaultdict(float)
    negative_vector: dict[str,float] = defaultdict(float)
    rated_count = 0
    rating_sum = rating_weight = 0.0
    rating_distribution = {str(i): 0 for i in range(1,11)}
    imdb_delta_sum = imdb_delta_w = 0.0

    with db.connect() as con:
        rows = con.execute("""SELECT m.*, r.rating AS user_rating, r.date_rated
                              FROM ratings r JOIN movies m ON m.id=r.movie_id""").fetchall()
        feedback = con.execute("SELECT f.kind,f.weight,m.* FROM feedback f JOIN movies m ON m.id=f.movie_id").fetchall()

    for row in rows:
        rated_count += 1
        movie = _movie_from_row(row)
        if not movie.semantic:
            movie.semantic = extract_semantic(movie)
        rating = int(row["user_rating"])
        rating_distribution[str(rating)] += 1
        signal = RATING_SIGNAL[rating]
        rw = _recency_weight(row["date_rated"])
        rating_sum += rating * rw; rating_weight += rw
        features = feature_vector(movie)
        for feature, fweight in features.items():
            w = rw * min(1.0, max(.15, fweight))
            a = aggregates[feature]
            a[0] += signal * w
            a[1] += w
            a[2] += rating * w
            a[3] += rating * rating * w
            a[4] += 1
            signed_vector[feature] += signal * w
            if rating >= 8:
                positive_vector[feature] += ((rating - 7.0) / 3.0) * w
            if rating >= 9:
                elite_vector[feature] += ((rating - 8.0) / 2.0) * w
            if rating <= 5:
                negative_vector[feature] += ((6.0 - rating) / 5.0) * w
        if movie.imdb_rating is not None:
            imdb_delta_sum += (rating - movie.imdb_rating) * rw
            imdb_delta_w += rw

    feedback_adjust: dict[str,float] = defaultdict(float)
    for row in feedback:
        movie = _movie_from_row(row)
        if not movie.semantic:
            movie.semantic = extract_semantic(movie)
        for f, fw in feature_vector(movie).items():
            feedback_adjust[f] += float(row["weight"]) * min(1.0, fw)

    features_out = {}
    for feature, (ss, w, sr, sr2, count) in aggregates.items():
        pref = _shrink(ss, w)
        pref += max(-.22, min(.22, feedback_adjust.get(feature, 0.0) * .12))
        mean = sr / w if w else None
        variance = max(0.0, (sr2 / w) - (mean * mean)) if w and mean is not None else 0.0
        features_out[feature] = {
            "preference": round(max(-1.0,min(1.0,pref)),4),
            "mean_rating": round(mean,3) if mean is not None else None,
            "std_rating": round(math.sqrt(variance),3) if mean is not None else None,
            "count": int(count),
            "weight": round(w,3),
        }

    global_mean = rating_sum / rating_weight if rating_weight else 6.5
    profile = {
        "version": PROFILE_VERSION,
        "rated_count": rated_count,
        "global_mean_rating": round(global_mean,4),
        "rating_distribution": rating_distribution,
        "features": features_out,
        "semantic_vector": _normalize_vector(signed_vector),
        "positive_vector": _normalize_vector(positive_vector),
        "elite_vector": _normalize_vector(elite_vector),
        "negative_vector": _normalize_vector(negative_vector),
        "mean_user_minus_imdb": round(imdb_delta_sum/imdb_delta_w,3) if imdb_delta_w else None,
        "generated_at": utcnow_iso(),
    }
    with db.tx() as con:
        con.execute("""INSERT INTO user_profile(profile_key,value_json,updated_at) VALUES('main',?,?)
                       ON CONFLICT(profile_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (json_dumps(profile), utcnow_iso()))
    return profile


def get_profile(db: Database) -> dict:
    with db.connect() as con:
        row = con.execute("SELECT value_json FROM user_profile WHERE profile_key='main'").fetchone()
    if not row:
        return build_profile(db)
    profile = json_loads(row[0], {})
    if int(profile.get("version", 0) or 0) < PROFILE_VERSION:
        return build_profile(db)
    return profile


def top_profile_features(profile: dict, prefix: str | None = None, positive: bool = True, limit: int = 12):
    items = []
    for name, stats in profile.get("features", {}).items():
        if prefix and not name.startswith(prefix):
            continue
        p = float(stats.get("preference",0))
        if positive and p <= 0: continue
        if not positive and p >= 0: continue
        items.append((name, stats))
    items.sort(key=lambda x: (abs(float(x[1].get("preference",0))), int(x[1].get("count",0))), reverse=True)
    return items[:limit]
