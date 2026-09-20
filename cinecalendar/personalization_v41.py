from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import math
import threading
from typing import Iterable

from .recommendation import row_to_movie
from .util import clamp, json_loads

PERSONALIZATION_V41_VERSION = "personalization-v4.1.0"
QUALITY_SETTING = "personalization_v41_quality"


@dataclass(frozen=True)
class _RatedExample:
    movie_id: int
    title: str
    rating: int
    date_key: str
    genres: tuple[str, ...]
    directors: tuple[str, ...]
    countries: tuple[str, ...]
    year: int | None
    content_type: str


def _content_type(movie) -> str:
    genres = {str(x).casefold() for x in (getattr(movie, "genres", None) or [])}
    typ = str(getattr(movie, "title_type", "") or "").casefold()
    if typ == "short":
        return "short"
    if "documentary" in genres:
        return "documentary"
    if "animation" in genres:
        return "animation"
    return "movie"


def _date_from(value: str) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


class PersonalizationBrainV41:
    """Bounded, locally validated personalization layer.

    It never replaces the production recommender. It adds explainability, temporal taste drift,
    content-type profiles and explicit anti-repetition. Score-changing influence is enabled only
    after a local chronological holdout does not regress calibration/ranking.
    """

    MAX_SCORE_SHIFT = 0.055
    MAX_TREND_SHIFT = 0.040
    MAX_MOOD_SHIFT = 0.020
    MAX_CONTEXT_SHIFT = 0.018

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._token = None
        self._examples: list[_RatedExample] = []
        self._profile: dict = {}
        self._quality: dict = {}

    def state_token(self) -> tuple:
        with self.db.connect() as con:
            row = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(updated_at),''),COALESCE(MAX(date_rated),'') FROM ratings"
            ).fetchone()
            feedback = con.execute(
                "SELECT COUNT(*),COALESCE(MAX(created_at),'') FROM feedback"
            ).fetchone()
        return (
            PERSONALIZATION_V41_VERSION,
            int(row[0] or 0),
            str(row[1] or ""),
            str(row[2] or ""),
            int(feedback[0] or 0),
            str(feedback[1] or ""),
        )

    @staticmethod
    def _features(movie) -> list[str]:
        out: list[str] = [f"content:{_content_type(movie)}"]
        out.extend(f"genre:{str(x).casefold()}" for x in (getattr(movie, "genres", None) or [])[:5])
        out.extend(f"director:{str(x).casefold()}" for x in (getattr(movie, "directors", None) or [])[:3])
        out.extend(f"country:{str(x).casefold()}" for x in (getattr(movie, "countries", None) or [])[:3])
        year = getattr(movie, "year", None)
        if year:
            out.append(f"decade:{int(year)//10*10}")
        return out

    @staticmethod
    def _recency_weight(date_key: str, today: date) -> float:
        d = _date_from(date_key)
        if d is None:
            return 0.62
        days = max(0, (today - d).days)
        # Recent taste matters more, but old history never disappears.
        return 0.35 + 0.65 * math.exp(-days / (365.25 * 2.6))

    def _load_examples(self) -> list[_RatedExample]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.id,m.title,m.original_title,m.year,m.title_type,
                          m.genres_json,m.directors_json,m.countries_json,
                          r.rating,COALESCE(r.date_rated,r.updated_at,'') AS date_key
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   ORDER BY COALESCE(r.date_rated,r.updated_at,'') ASC,r.id ASC"""
            ).fetchall()
        out: list[_RatedExample] = []
        for row in rows:
            genres = tuple(str(x) for x in (json_loads(row["genres_json"], []) or []))
            directors = tuple(str(x) for x in (json_loads(row["directors_json"], []) or []))
            countries = tuple(str(x) for x in (json_loads(row["countries_json"], []) or []))
            typ = str(row["title_type"] or "Movie").casefold()
            if typ == "short":
                ctype = "short"
            elif any(x.casefold() == "documentary" for x in genres):
                ctype = "documentary"
            elif any(x.casefold() == "animation" for x in genres):
                ctype = "animation"
            else:
                ctype = "movie"
            out.append(
                _RatedExample(
                    movie_id=int(row["id"]),
                    title=str(row["original_title"] or row["title"] or ""),
                    rating=int(row["rating"]),
                    date_key=str(row["date_key"] or ""),
                    genres=genres,
                    directors=directors,
                    countries=countries,
                    year=int(row["year"]) if row["year"] is not None else None,
                    content_type=ctype,
                )
            )
        return out

    @staticmethod
    def _example_features(ex: _RatedExample) -> list[str]:
        out = [f"content:{ex.content_type}"]
        out.extend(f"genre:{x.casefold()}" for x in ex.genres[:5])
        out.extend(f"director:{x.casefold()}" for x in ex.directors[:3])
        out.extend(f"country:{x.casefold()}" for x in ex.countries[:3])
        if ex.year:
            out.append(f"decade:{ex.year//10*10}")
        return out

    def _build_profile(self, examples: list[_RatedExample], *, today: date | None = None) -> dict:
        today = today or date.today()
        if not examples:
            return {"global_mean": 0.0, "features": {}, "content": {}, "recent_count": 0}

        total_w = 0.0
        total_score = 0.0
        buckets: dict[str, dict[str, float]] = defaultdict(lambda: {
            "w": 0.0, "sum": 0.0, "recent_w": 0.0, "recent_sum": 0.0,
            "old_w": 0.0, "old_sum": 0.0, "count": 0.0, "recent_count": 0.0, "old_count": 0.0,
        })
        recent_cut = today.toordinal() - 730
        recent_count = 0

        for ex in examples:
            w = self._recency_weight(ex.date_key, today)
            extremity = 0.82 + 0.18 * abs(ex.rating - 5.5) / 4.5
            w *= extremity
            total_w += w
            total_score += ex.rating * w
            d = _date_from(ex.date_key)
            is_recent = bool(d and d.toordinal() >= recent_cut)
            if is_recent:
                recent_count += 1
            for feature in self._example_features(ex):
                b = buckets[feature]
                b["w"] += w
                b["sum"] += ex.rating * w
                b["count"] += 1
                if is_recent:
                    b["recent_w"] += w
                    b["recent_sum"] += ex.rating * w
                    b["recent_count"] += 1
                else:
                    b["old_w"] += w
                    b["old_sum"] += ex.rating * w
                    b["old_count"] += 1

        global_mean = total_score / max(0.001, total_w)
        features: dict[str, dict] = {}
        for name, b in buckets.items():
            mean = b["sum"] / max(0.001, b["w"])
            recent_mean = b["recent_sum"] / b["recent_w"] if b["recent_w"] > 0 else None
            old_mean = b["old_sum"] / b["old_w"] if b["old_w"] > 0 else None
            trend = 0.0
            support = min(1.0, b["count"] / 12.0)
            if (
                recent_mean is not None and old_mean is not None
                and b["recent_count"] >= 3 and b["old_count"] >= 3
            ):
                trend = max(-2.5, min(2.5, recent_mean - old_mean))
            elif recent_mean is not None and b["recent_count"] >= 4:
                trend = max(-1.25, min(1.25, recent_mean - global_mean)) * 0.45
            features[name] = {
                "mean": mean,
                "count": int(b["count"]),
                "recent_mean": recent_mean,
                "old_mean": old_mean,
                "trend": trend,
                "support": support,
            }

        content = {
            key.split(":", 1)[1]: value
            for key, value in features.items()
            if key.startswith("content:")
        }
        return {
            "global_mean": global_mean,
            "features": features,
            "content": content,
            "recent_count": recent_count,
            "rating_count": len(examples),
        }

    @staticmethod
    def _pairwise(actual: list[float], predicted: list[float]) -> float | None:
        correct = compared = 0
        for i in range(len(actual)):
            for j in range(i + 1, len(actual)):
                delta = actual[i] - actual[j]
                if abs(delta) < 2.0:
                    continue
                compared += 1
                pd = predicted[i] - predicted[j]
                if pd == 0:
                    correct += 0.5
                elif (delta > 0) == (pd > 0):
                    correct += 1
        return (correct / compared) if compared else None

    def _predict_from_profile(self, ex: _RatedExample, profile: dict) -> float:
        global_mean = float(profile.get("global_mean", 5.5) or 5.5)
        values = []
        weights = []
        for feature in self._example_features(ex):
            st = (profile.get("features") or {}).get(feature)
            if not st or int(st.get("count", 0)) < 2:
                continue
            kind = feature.split(":", 1)[0]
            kind_w = {"director": 1.35, "genre": 1.0, "content": 1.1, "country": .55, "decade": .35}.get(kind, .5)
            support = float(st.get("support", 0.0) or 0.0)
            values.append(float(st.get("mean", global_mean)) - global_mean)
            weights.append(kind_w * (0.35 + 0.65 * support))
        delta = sum(v * w for v, w in zip(values, weights)) / max(1.0, sum(weights))
        return max(1.0, min(10.0, global_mean + delta * 0.85))

    def _quality_gate(self, examples: list[_RatedExample]) -> dict:
        cached = self.db.get_setting(QUALITY_SETTING, {})
        token = list(self.state_token())
        if isinstance(cached, dict) and cached.get("state_token") == token:
            return dict(cached)

        if len(examples) < 120:
            payload = {
                "version": PERSONALIZATION_V41_VERSION,
                "state_token": token,
                "approved": False,
                "reason": "insufficient_ratings",
                "rating_count": len(examples),
            }
            self.db.set_setting(QUALITY_SETTING, payload)
            return payload

        cut = max(90, int(len(examples) * 0.80))
        holdout = examples[cut:]
        train = examples[:cut]
        if len(holdout) < 30:
            payload = {
                "version": PERSONALIZATION_V41_VERSION,
                "state_token": token,
                "approved": False,
                "reason": "insufficient_holdout",
                "rating_count": len(examples),
            }
            self.db.set_setting(QUALITY_SETTING, payload)
            return payload

        ref_date = _date_from(train[-1].date_key) or date.today()
        profile = self._build_profile(train, today=ref_date)
        baseline = float(profile.get("global_mean", 5.5) or 5.5)
        actual = [float(x.rating) for x in holdout]
        base_pred = [baseline for _ in holdout]
        model_pred = [self._predict_from_profile(x, profile) for x in holdout]
        base_mae = sum(abs(a - p) for a, p in zip(actual, base_pred)) / len(actual)
        model_mae = sum(abs(a - p) for a, p in zip(actual, model_pred)) / len(actual)
        base_pair = self._pairwise(actual, base_pred)
        model_pair = self._pairwise(actual, model_pred)
        mae_gain = (base_mae - model_mae) / max(.25, base_mae)
        pair_gain = (
            (model_pair - base_pair)
            if model_pair is not None and base_pair is not None else 0.0
        )
        safe = model_mae <= base_mae * 1.01
        pair_safe = (
            True if model_pair is None or base_pair is None
            else model_pair >= base_pair - .01
        )
        meaningful = mae_gain >= .005 or pair_gain >= .01
        approved = bool(safe and pair_safe and meaningful)

        payload = {
            "version": PERSONALIZATION_V41_VERSION,
            "state_token": token,
            "approved": approved,
            "reason": "validated" if approved else "no_measured_gain",
            "rating_count": len(examples),
            "training_count": len(train),
            "holdout_count": len(holdout),
            "baseline_mae": round(base_mae, 5),
            "model_mae": round(model_mae, 5),
            "mae_gain": round(mae_gain, 6),
            "baseline_pairwise": None if base_pair is None else round(base_pair, 5),
            "model_pairwise": None if model_pair is None else round(model_pair, 5),
            "pairwise_gain": round(pair_gain, 6),
        }
        self.db.set_setting(QUALITY_SETTING, payload)
        return payload

    def _ensure(self) -> None:
        token = self.state_token()
        with self._lock:
            if token == self._token:
                return
        examples = self._load_examples()
        profile = self._build_profile(examples)
        quality = self._quality_gate(examples)
        with self._lock:
            self._examples = examples
            self._profile = profile
            self._quality = quality
            self._token = token

    def status(self) -> dict:
        self._ensure()
        with self._lock:
            profile = dict(self._profile)
            quality = dict(self._quality)
        return {
            "version": PERSONALIZATION_V41_VERSION,
            "quality_gate": quality,
            "rating_count": int(profile.get("rating_count", 0) or 0),
            "recent_count": int(profile.get("recent_count", 0) or 0),
            "global_mean": round(float(profile.get("global_mean", 0.0) or 0.0), 3),
            "content_profiles": {
                key: {
                    "mean": round(float(value.get("mean", 0.0) or 0.0), 3),
                    "trend": round(float(value.get("trend", 0.0) or 0.0), 3),
                    "count": int(value.get("count", 0) or 0),
                }
                for key, value in (profile.get("content") or {}).items()
            },
        }

    def taste_evolution(self, limit: int = 10) -> list[dict]:
        self._ensure()
        rows = []
        for feature, st in (self._profile.get("features") or {}).items():
            trend = float(st.get("trend", 0.0) or 0.0)
            if abs(trend) < .18 or int(st.get("count", 0)) < 5:
                continue
            rows.append({
                "feature": feature,
                "trend": trend,
                "mean": float(st.get("mean", 0.0) or 0.0),
                "recent_mean": st.get("recent_mean"),
                "old_mean": st.get("old_mean"),
                "count": int(st.get("count", 0) or 0),
            })
        rows.sort(key=lambda x: abs(float(x["trend"])), reverse=True)
        return rows[:max(1, int(limit))]

    def _temporal_adjustment(self, movie) -> tuple[float, list[str]]:
        self._ensure()
        features = self._profile.get("features") or {}
        global_mean = float(self._profile.get("global_mean", 5.5) or 5.5)
        values: list[tuple[float, float, str]] = []
        for feature in self._features(movie):
            st = features.get(feature)
            if not st or int(st.get("count", 0)) < 3:
                continue
            kind = feature.split(":", 1)[0]
            kind_w = {"director": 1.35, "genre": 1.0, "content": 1.2, "country": .6, "decade": .35}.get(kind, .5)
            support = float(st.get("support", 0.0) or 0.0)
            preference = (float(st.get("mean", global_mean)) - global_mean) / 4.5
            trend = float(st.get("trend", 0.0) or 0.0) / 2.5
            raw = .55 * preference + .45 * trend
            weight = kind_w * (0.25 + .75 * support)
            values.append((raw, weight, feature))
        if not values:
            return 0.0, []
        raw = sum(v * w for v, w, _ in values) / max(0.01, sum(w for _, w, _ in values))
        shift = max(-self.MAX_TREND_SHIFT, min(self.MAX_TREND_SHIFT, raw * .045))
        reasons = [
            feature for _v, _w, feature in
            sorted(values, key=lambda item: abs(item[0] * item[1]), reverse=True)[:3]
        ]
        return shift, reasons

    @staticmethod
    def _feature_label(token: str) -> str:
        kind, value = token.split(":", 1) if ":" in token else ("", token)
        labels = {
            "genre": "gen",
            "director": "regizor",
            "country": "țară",
            "content": "tip",
            "decade": "perioadă",
        }
        return f"{labels.get(kind, kind)} {value.replace('_', ' ')}".strip()

    def _similar_examples(self, movie, *, positive: bool, limit: int = 3) -> list[tuple[_RatedExample, float]]:
        self._ensure()
        target_genres = {x.casefold() for x in (getattr(movie, "genres", None) or [])}
        target_directors = {x.casefold() for x in (getattr(movie, "directors", None) or [])}
        target_countries = {x.casefold() for x in (getattr(movie, "countries", None) or [])}
        target_type = _content_type(movie)
        target_year = getattr(movie, "year", None)
        scored = []
        for ex in self._examples:
            if positive and ex.rating < 7:
                continue
            if not positive and ex.rating > 4:
                continue
            score = 0.0
            directors = {x.casefold() for x in ex.directors}
            genres = {x.casefold() for x in ex.genres}
            countries = {x.casefold() for x in ex.countries}
            if target_directors and directors:
                score += 3.4 * len(target_directors & directors)
            score += 1.15 * len(target_genres & genres)
            score += .45 * len(target_countries & countries)
            if ex.content_type == target_type:
                score += .45
            if target_year and ex.year and abs(int(target_year) - int(ex.year)) <= 8:
                score += .20
            if score <= .45:
                continue
            score *= (1.0 + .045 * abs(ex.rating - 5.5))
            scored.append((ex, score))
        scored.sort(key=lambda item: (item[1], item[0].rating), reverse=True)
        return scored[:max(1, int(limit))]

    def _mood_shift(self, movie, mood: str) -> tuple[float, str]:
        mood = str(mood or "neutral")
        if mood == "neutral":
            return 0.0, ""
        genres = {x.casefold() for x in (getattr(movie, "genres", None) or [])}
        semantic = {str(k).casefold(): float(v) for k, v in (getattr(movie, "semantic", None) or {}).items()}
        val = 0.0
        if mood == "light":
            val += .6 if genres & {"comedy", "animation", "family"} else 0.0
            val -= .4 if genres & {"horror", "war"} else 0.0
        elif mood == "intense":
            val += .6 if genres & {"thriller", "crime", "horror", "war", "mystery"} else 0.0
        elif mood == "contemplative":
            val += .45 if genres & {"drama", "documentary", "history"} else 0.0
            val += .55 * max(semantic.get("contemplative", 0.0), semantic.get("faith", 0.0))
        elif mood == "easy":
            runtime = getattr(movie, "runtime_min", None)
            if runtime and int(runtime) <= 105:
                val += .45
            val += .35 if genres & {"comedy", "animation", "family"} else 0.0
        shift = max(-self.MAX_MOOD_SHIFT, min(self.MAX_MOOD_SHIFT, val * .02))
        return shift, mood

    def _context_shift(self, score) -> float:
        calendar = clamp(float(getattr(score, "calendar", 0.0) or 0.0))
        predicted = float(getattr(score, "predicted_rating", 0.0) or 0.0)
        kind = str(getattr(score, "calendar_kind", "") or "").casefold()
        if calendar < .16 or predicted < 6.35:
            return 0.0
        multiplier = 1.0
        if "atmosfer" in kind:
            multiplier = .35
        elif "spiritual" in kind:
            multiplier = .72
        elif "istor" in kind:
            multiplier = .82
        return min(self.MAX_CONTEXT_SHIFT, calendar * multiplier * .018)

    def enhance_score(self, movie, score, when: date | None = None, *, mood: str = "neutral") -> dict:
        self._ensure()
        when = when or date.today()
        approved = bool(self._quality.get("approved"))
        temporal_shift, trend_features = self._temporal_adjustment(movie)
        mood_shift, mood_label = self._mood_shift(movie, mood)
        context_shift = self._context_shift(score)
        applied_temporal = temporal_shift if approved else 0.0
        total_shift = max(
            -self.MAX_SCORE_SHIFT,
            min(self.MAX_SCORE_SHIFT, applied_temporal + mood_shift + context_shift),
        )
        if total_shift:
            score.final = clamp(float(score.final) + total_shift)

        positives = self._similar_examples(movie, positive=True, limit=3)
        negatives = self._similar_examples(movie, positive=False, limit=2)
        evidence_text = ""
        if positives:
            evidence_text = " Repere concrete: " + "; ".join(
                f"{ex.title} {ex.rating}/10" for ex, _sim in positives
            ) + "."
            if evidence_text not in (score.personal_reason or ""):
                score.personal_reason = (score.personal_reason or "").rstrip() + evidence_text

        if trend_features:
            direction = "mai bine" if temporal_shift > 0 else "mai slab"
            reason = (
                "Gustul recent indică o potrivire "
                + direction
                + " pentru "
                + ", ".join(self._feature_label(x) for x in trend_features[:3])
                + "."
            )
            score.contributions.append(
                ("Evoluția gustului", applied_temporal * 100.0, reason + (
                    "" if approved else " Ajustarea de scor este blocată de quality-gate."
                ))
            )

        if context_shift:
            score.contributions.append(
                ("Context strict 4.1", context_shift * 100.0,
                 "Contextul perioadei primește doar un bonus mic și numai peste pragul de potrivire personală.")
            )
        if mood_shift:
            score.contributions.append(
                ("Dispoziția aleasă", mood_shift * 100.0, f"Modul «{mood_label}» rafinează doar ușor ordinea.")
            )

        why_not: list[str] = []
        if float(getattr(score, "predicted_rating", 0.0) or 0.0) < 6.2:
            why_not.append(f"estimarea personală este doar {float(score.predicted_rating):.1f}/10")
        if float(getattr(score, "confidence", 0.0) or 0.0) < .55:
            why_not.append("încrederea modelului este moderată")
        if float(getattr(score, "repeat_penalty", 0.0) or 0.0) > 0:
            why_not.append("a fost recomandat recent")
        if temporal_shift < -.012:
            why_not.append("gustul tău recent s-a răcit pentru tipare similare")
        if negatives:
            why_not.append(
                "ai evaluat slab "
                + ", ".join(f"{ex.title} ({ex.rating}/10)" for ex, _sim in negatives[:2])
            )

        score.why_not = "; ".join(why_not[:4])
        score.taste_shift = float(temporal_shift)
        score.content_profile = _content_type(movie)
        score.score_factors = {
            "gust": float(getattr(score, "taste", 0.0) or 0.0),
            "ALS/context personal": float(getattr(score, "semantic", 0.0) or 0.0),
            "regizor/cinematografie": float(getattr(score, "director_cinema", 0.0) or 0.0),
            "calendar": float(getattr(score, "calendar", 0.0) or 0.0),
            "anotimp": float(getattr(score, "season", 0.0) or 0.0),
            "calitate": float(getattr(score, "quality", 0.0) or 0.0),
            "noutate": float(getattr(score, "novelty", 0.0) or 0.0),
            "evoluția gustului": float(applied_temporal),
            "dispoziție": float(mood_shift),
            "context strict": float(context_shift),
        }
        return {
            "approved": approved,
            "temporal_shift": temporal_shift,
            "applied_temporal": applied_temporal,
            "mood_shift": mood_shift,
            "context_shift": context_shift,
            "total_shift": total_shift,
        }

    def _recent_feature_counts(self, when: date) -> dict[str, int]:
        cutoff = when.toordinal() - 45
        counts: dict[str, int] = defaultdict(int)
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.*,h.context_date
                   FROM recommendation_history h JOIN movies m ON m.id=h.movie_id
                   WHERE h.context_date>=?""",
                ((when.fromordinal(cutoff)).isoformat(),),
            ).fetchall()
        for row in rows:
            movie = row_to_movie(row)
            for feature in self._features(movie):
                if feature.startswith(("genre:", "director:", "country:", "decade:")):
                    counts[feature] += 1
        return counts

    def _diversity_penalty(self, movie, selected, recent_counts: dict[str, int]) -> tuple[float, str]:
        features = set(self._features(movie))
        penalty = 0.0
        reasons = []
        for chosen in selected:
            other = set(self._features(chosen.movie))
            same_director = any(x.startswith("director:") for x in features & other)
            genre_overlap = len([x for x in features & other if x.startswith("genre:")])
            same_country = any(x.startswith("country:") for x in features & other)
            same_decade = any(x.startswith("decade:") for x in features & other)
            if same_director:
                penalty += .026; reasons.append("același regizor")
            if genre_overlap >= 2:
                penalty += .016; reasons.append("genuri foarte similare")
            elif genre_overlap == 1:
                penalty += .007
            if same_country:
                penalty += .005
            if same_decade:
                penalty += .004
        history_penalty = sum(min(3, recent_counts.get(f, 0)) for f in features if f.startswith(("director:", "genre:"))) * .0015
        penalty += min(.018, history_penalty)
        return min(.052, penalty), ", ".join(dict.fromkeys(reasons)) or ""

    def enhance_and_diversify(
        self,
        recs: Iterable,
        when: date,
        count: int,
        *,
        mode: str = "decide",
        mood: str = "neutral",
    ) -> list:
        recs = list(recs)
        if not recs:
            return []
        for rec in recs:
            self.enhance_score(rec.movie, rec.score, when, mood=mood)

        recs.sort(
            key=lambda r: (float(r.score.final), float(r.score.predicted_rating), float(r.score.confidence)),
            reverse=True,
        )
        approved = bool(self._quality.get("approved"))
        if not approved:
            return recs[:max(1, int(count))]

        recent_counts = self._recent_feature_counts(when)
        selected = []
        pool = list(recs)
        while pool and len(selected) < max(1, int(count)):
            best = None
            best_value = -99.0
            for rec in pool:
                penalty, reason = self._diversity_penalty(rec.movie, selected, recent_counts)
                if mode == "surprise":
                    penalty *= .70
                value = float(rec.score.final) - penalty
                if value > best_value:
                    best_value = value
                    best = (rec, penalty, reason)
            if best is None:
                break
            rec, penalty, reason = best
            if penalty:
                rec.score.final = clamp(float(rec.score.final) - penalty)
                rec.score.repeat_penalty = float(getattr(rec.score, "repeat_penalty", 0.0) or 0.0) + penalty
                rec.score.contributions.append(
                    ("Anti-repetiție 4.1", -penalty * 100.0,
                     "Penalizare pentru varietate" + (f": {reason}." if reason else "."))
                )
            selected.append(rec)
            pool.remove(rec)
        return selected

    def dynamic_watchlist_shift(self, row: dict, score, *, pinned: bool, when: date) -> tuple[float, str]:
        movie = row_to_movie(row)
        data = self.enhance_score(movie, score, when, mood="neutral")
        shift = float(data.get("total_shift", 0.0) or 0.0)
        added = _date_from(str(row.get("watchlist_added_at") or ""))
        age_days = max(0, (when - added).days) if added else 0
        # Old items do not float upward just because they are old. Low-fit stale items drift down.
        stale_penalty = .0
        if age_days >= 365 and float(score.predicted_rating or 0.0) < 6.5 and not pinned:
            stale_penalty = min(.018, .006 + (age_days - 365) / 3650.0)
        pin_bonus = .08 if pinned else 0.0
        total = max(-.04, min(.09, shift + pin_bonus - stale_penalty))
        reason = (
            ("prioritate manuală; " if pinned else "")
            + ("gust recalculat după ratingurile noi; " if abs(shift) >= .004 else "")
            + ("film vechi în Watchlist cu potrivire modestă" if stale_penalty else "")
        ).strip("; ")
        return total, reason

    def choose_runtime_bounds(self) -> tuple[int | None, int | None]:
        key = str(self.db.get_setting("chooser_runtime_bucket", "all") or "all")
        if key == "60":
            return 60, None
        if key == "90":
            return 90, None
        if key == "120":
            return 120, None
        if key == "180plus":
            return None, 181
        return None, None


def personalization_engine_class(base_cls: type) -> type:
    if bool(getattr(base_cls, "_cinecalendar_v41_personalization", False)):
        return base_cls

    class PersonalizedV41(base_cls):
        _cinecalendar_v41_personalization = True
        PERSONALIZATION_V41_VERSION = PERSONALIZATION_V41_VERSION

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.personalization_v41 = PersonalizationBrainV41(self.db)

        def _state_token(self):
            base = super()._state_token()
            return base + (PERSONALIZATION_V41_VERSION, self.personalization_v41.state_token())

        def recommend(
            self, when=None, count=3, exclude_ids=None, record=False, slot="today",
            candidate_limit=100000, mode="decide", runtime_max=None, runtime_min=None,
        ):
            when = when or date.today()
            requested = max(1, int(count))
            # Browse/list surfaces get a wider pool so 4.1 can diversify without touching the
            # validated Top-3 membership used for the decision surface.
            expanded = requested
            if 4 <= requested <= 24:
                expanded = min(40, max(requested, requested * 2))

            base = super().recommend(
                when=when,
                count=expanded,
                exclude_ids=exclude_ids,
                record=False,
                slot=slot,
                candidate_limit=candidate_limit,
                mode=mode,
                runtime_max=runtime_max,
                runtime_min=runtime_min,
            )
            mood = str(self.db.get_setting("chooser_mood", "neutral") or "neutral")
            if requested <= 3:
                # Do not replace a validated Top-3 member; only reorder/annotate those exact films.
                enhanced = self.personalization_v41.enhance_and_diversify(
                    list(base)[:requested], when, requested, mode=mode, mood=mood
                )
            else:
                enhanced = self.personalization_v41.enhance_and_diversify(
                    base, when, requested, mode=mode, mood=mood
                )

            if record and enhanced:
                recorder = getattr(self, "_record_selected", None)
                if callable(recorder):
                    recorder(enhanced, when, slot, len(base))
            return enhanced

        def decision_pick(self, when=None, exclude_ids=None, mode="decide"):
            when = when or date.today()
            runtime_max, runtime_min = self.personalization_v41.choose_runtime_bounds()
            if runtime_max is None and runtime_min is None:
                return super().decision_pick(when, exclude_ids, mode)
            pool = self.recommend(
                when=when,
                count=max(8, int(getattr(self, "DECISION_CACHE_SIZE", 12) or 12)),
                exclude_ids=exclude_ids,
                record=False,
                slot="decision-v41",
                candidate_limit=int(getattr(self, "EXPLORE_POOL", 45000) or 45000),
                mode=mode,
                runtime_max=runtime_max,
                runtime_min=runtime_min,
            )
            return (pool[0] if pool else None, pool[1:3])

        def personalization_status(self) -> dict:
            return self.personalization_v41.status()

    PersonalizedV41.__name__ = f"{base_cls.__name__}PersonalizedV41"
    PersonalizedV41.__qualname__ = PersonalizedV41.__name__
    return PersonalizedV41
