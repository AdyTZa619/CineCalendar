from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
import math
import re
import threading

from .util import clamp, json_loads, normalize_text


LEARNING_INSIGHT_VERSION = "learning-insight-v4.3.0"
CALIBRATION_SETTING = "learning_insight_v43_calibration"
SEMANTIC_SETTING = "learning_insight_v43_semantic"

_STOP = {
    "this","that","with","from","into","after","before","about","their","there","where","when","which",
    "while","through","between","during","against","without","within","would","could","should","being",
    "have","has","had","will","they","them","then","than","were","been","also","some","more","most",
    "film","movie","story","young","life","finds","must","becomes","years","world","family",
    "acest","aceasta","care","dintr","dintre","pentru","fara","este","sunt","unui","unei","dupa","inainte",
    "cand","unde","prin","spre","despre","filmul","poveste","viata","lumea","familie","tanar","tanara",
}


def _content_type_from_values(title_type: str, genres: list[str]) -> str:
    typ = str(title_type or "").casefold()
    g = {str(x).casefold() for x in genres}
    if typ == "short":
        return "short"
    if "documentary" in g:
        return "documentary"
    if "animation" in g:
        return "animation"
    return "movie"


def _movie_content_type(movie) -> str:
    return _content_type_from_values(
        str(getattr(movie, "title_type", "") or ""),
        list(getattr(movie, "genres", None) or []),
    )


def _words(text: str) -> list[str]:
    normalized = normalize_text(str(text or "")).casefold()
    return [
        token for token in re.findall(r"[a-z0-9]{4,}", normalized)
        if token not in _STOP and not token.isdigit()
    ]


def text_signature_from_values(overview: str, keywords: list[str]) -> dict[str, float]:
    """Compact semantic signature without a heavyweight model.

    Keywords get stronger weight. Overview unigrams/bigrams are capped so very long synopses do
    not dominate the profile. The learned layer later applies IDF/support shrinkage.
    """
    weights: dict[str, float] = {}
    for raw in list(keywords or [])[:30]:
        phrase = normalize_text(str(raw or "")).casefold().strip()
        if not phrase:
            continue
        weights[f"kw:{phrase}"] = 1.50
        for token in _words(phrase):
            weights[f"w:{token}"] = max(weights.get(f"w:{token}", 0.0), 1.05)

    tokens = _words(overview)[:180]
    counts = Counter(tokens)
    for token, count in counts.most_common(36):
        weights[f"w:{token}"] = max(
            weights.get(f"w:{token}", 0.0),
            min(1.0, 0.42 + 0.12 * math.log1p(count)),
        )
    bigrams = Counter(zip(tokens, tokens[1:]))
    for (a, b), count in bigrams.most_common(18):
        if a == b:
            continue
        weights[f"bg:{a}_{b}"] = min(1.0, 0.50 + 0.10 * math.log1p(count))
    return weights


def text_signature(movie) -> dict[str, float]:
    return text_signature_from_values(
        str(getattr(movie, "overview", "") or ""),
        list(getattr(movie, "keywords", None) or []),
    )


@dataclass(frozen=True)
class _RatedText:
    movie_id: int
    rating: int
    date_key: str
    title_type: str
    genres: tuple[str, ...]
    signature: dict[str, float]


class PredictionCalibratorV43:
    """Outcome-based rating calibration with a chronological local quality gate."""

    MIN_TOTAL = 30
    MIN_HOLDOUT = 8
    MAX_CORRECTION = 0.70
    MAX_FINAL_SHIFT = 0.012

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._token = None
        self._model: dict = {}
        self._status: dict = {}

    def state_token(self) -> tuple:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT COUNT(*),COALESCE(MAX(updated_at),'')
                   FROM recommendation_outcomes
                   WHERE actual_rating IS NOT NULL AND predicted_rating IS NOT NULL"""
            ).fetchone()
        return (LEARNING_INSIGHT_VERSION, int(row[0] or 0), str(row[1] or ""))

    def _rows(self) -> list[dict]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT o.exposure_history_id,o.actual_rating,o.predicted_rating,
                          o.rating_date,o.context_date,o.updated_at,
                          m.title_type,m.genres_json
                   FROM recommendation_outcomes o
                   JOIN movies m ON m.id=o.movie_id
                   WHERE o.actual_rating IS NOT NULL
                     AND o.predicted_rating IS NOT NULL
                   ORDER BY COALESCE(NULLIF(o.rating_date,''),o.context_date,o.updated_at),
                            o.exposure_history_id"""
            ).fetchall()
        out = []
        for row in rows:
            genres = [str(x) for x in (json_loads(row["genres_json"], []) or [])]
            out.append({
                "actual": float(row["actual_rating"]),
                "predicted": float(row["predicted_rating"]),
                "date": str(row["rating_date"] or row["context_date"] or row["updated_at"] or ""),
                "content": _content_type_from_values(str(row["title_type"] or ""), genres),
            })
        return out

    @staticmethod
    def _fit(rows: list[dict]) -> dict:
        if not rows:
            return {"global": 0.0, "content": {}, "bins": {}, "count": 0}
        residuals = [r["actual"] - r["predicted"] for r in rows]
        global_bias = sum(residuals) / len(residuals)
        by_content: dict[str, list[float]] = defaultdict(list)
        by_bin: dict[int, list[float]] = defaultdict(list)
        for row, residual in zip(rows, residuals):
            by_content[str(row["content"])].append(residual)
            by_bin[int(max(1, min(9, math.floor(row["predicted"]))))].append(residual)

        def shrunk(values: list[float], prior: float, strength: float) -> dict:
            n = len(values)
            raw = sum(values) / max(1, n)
            value = (raw * n + prior * strength) / (n + strength)
            return {"bias": value, "count": n}

        return {
            "global": max(-1.0, min(1.0, global_bias)),
            "content": {
                key: shrunk(values, global_bias, 8.0)
                for key, values in by_content.items()
            },
            "bins": {
                str(key): shrunk(values, global_bias, 10.0)
                for key, values in by_bin.items()
            },
            "count": len(rows),
        }

    @classmethod
    def _predict(cls, predicted: float, content: str, model: dict) -> tuple[float, float]:
        base = float(model.get("global", 0.0) or 0.0)
        pieces: list[tuple[float, float]] = [(base, 1.0)]
        c = (model.get("content") or {}).get(content)
        if c and int(c.get("count", 0) or 0) >= 5:
            pieces.append((float(c.get("bias", base)), min(1.0, int(c.get("count", 0)) / 20.0)))
        b = (model.get("bins") or {}).get(str(int(max(1, min(9, math.floor(predicted))))))
        if b and int(b.get("count", 0) or 0) >= 5:
            pieces.append((float(b.get("bias", base)), min(1.0, int(b.get("count", 0)) / 24.0)))
        correction = sum(v * w for v, w in pieces) / max(0.001, sum(w for _, w in pieces))
        correction = max(-cls.MAX_CORRECTION, min(cls.MAX_CORRECTION, correction))
        return max(1.0, min(10.0, predicted + correction)), correction

    def _build(self, rows: list[dict]) -> tuple[dict, dict]:
        if len(rows) < self.MIN_TOTAL:
            return self._fit(rows), {
                "approved": False,
                "reason": "insufficient_outcomes",
                "count": len(rows),
                "minimum": self.MIN_TOTAL,
            }

        cut = max(self.MIN_TOTAL - self.MIN_HOLDOUT, int(len(rows) * 0.75))
        cut = min(cut, len(rows) - self.MIN_HOLDOUT)
        train, holdout = rows[:cut], rows[cut:]
        model = self._fit(train)
        raw_errors = []
        calibrated_errors = []
        raw_within = calibrated_within = 0
        for row in holdout:
            actual = float(row["actual"])
            raw = float(row["predicted"])
            calibrated, _ = self._predict(raw, str(row["content"]), model)
            re = abs(actual - raw)
            ce = abs(actual - calibrated)
            raw_errors.append(re)
            calibrated_errors.append(ce)
            raw_within += int(re <= 1.0)
            calibrated_within += int(ce <= 1.0)

        raw_mae = sum(raw_errors) / len(raw_errors)
        cal_mae = sum(calibrated_errors) / len(calibrated_errors)
        gain = raw_mae - cal_mae
        within_gain = (calibrated_within - raw_within) / len(holdout)
        approved = bool(
            cal_mae <= raw_mae * 0.995
            and (gain >= 0.025 or within_gain >= 0.04)
        )
        status = {
            "approved": approved,
            "reason": "validated" if approved else "no_measured_gain",
            "count": len(rows),
            "training_count": len(train),
            "holdout_count": len(holdout),
            "raw_mae": round(raw_mae, 5),
            "calibrated_mae": round(cal_mae, 5),
            "mae_gain": round(gain, 5),
            "within_one_gain": round(within_gain, 5),
        }
        # Once approved, refit on all measured outcomes for production.
        return (self._fit(rows) if approved else model), status

    def _ensure(self) -> None:
        token = self.state_token()
        with self._lock:
            if token == self._token:
                return
        rows = self._rows()
        model, status = self._build(rows)
        status = {
            "version": LEARNING_INSIGHT_VERSION,
            "state_token": list(token),
            **status,
        }
        try:
            self.db.set_setting(CALIBRATION_SETTING, status)
        except Exception:
            pass
        with self._lock:
            self._model = model
            self._status = status
            self._token = token

    def status(self) -> dict:
        self._ensure()
        with self._lock:
            return dict(self._status)

    def apply(self, movie, score) -> dict:
        self._ensure()
        raw = float(getattr(score, "predicted_rating", 0.0) or 0.0)
        if raw <= 0:
            return {"approved": False, "raw": raw, "calibrated": raw, "correction": 0.0}
        content = _movie_content_type(movie)
        calibrated, correction = self._predict(raw, content, self._model)
        approved = bool(self._status.get("approved"))
        applied = correction if approved else 0.0
        if approved and abs(applied) >= 1e-6:
            score.predicted_rating = calibrated
            final_shift = max(
                -self.MAX_FINAL_SHIFT,
                min(self.MAX_FINAL_SHIFT, applied / 10.0 * 0.12),
            )
            score.final = clamp(float(score.final) + final_shift)
            score.contributions.insert(
                0,
                (
                    "Calibrare după rezultate reale",
                    final_shift * 100.0,
                    f"Predicția brută {raw:.1f}/10 este calibrată la {calibrated:.1f}/10 "
                    f"din {int(self._status.get('count', 0) or 0)} rezultate reale măsurabile.",
                ),
            )
            score.score_factors["calibrare rezultat"] = final_shift
        return {
            "approved": approved,
            "raw": raw,
            "calibrated": calibrated if approved else raw,
            "correction": applied,
        }


class TextSemanticBrainV43:
    """Fine-grained synopsis/keyword preference with chronological self-validation."""

    MIN_TEXT_RATINGS = 120
    MIN_HOLDOUT = 24
    MAX_FINAL_SHIFT = 0.009

    def __init__(self, db):
        self.db = db
        self._lock = threading.RLock()
        self._token = None
        self._model: dict = {}
        self._status: dict = {}

    def state_token(self) -> tuple:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT COUNT(*),COALESCE(MAX(r.updated_at),''),
                          SUM(CASE WHEN length(COALESCE(m.overview,''))>20
                                    OR COALESCE(m.keywords_json,'[]') NOT IN ('','[]')
                                   THEN 1 ELSE 0 END)
                   FROM ratings r JOIN movies m ON m.id=r.movie_id"""
            ).fetchone()
        return (
            LEARNING_INSIGHT_VERSION,
            int(row[0] or 0),
            str(row[1] or ""),
            int(row[2] or 0),
        )

    def _rows(self) -> list[_RatedText]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT m.id,m.title_type,m.genres_json,m.overview,m.keywords_json,
                          r.rating,COALESCE(NULLIF(r.date_rated,''),r.updated_at,'') AS date_key
                   FROM ratings r JOIN movies m ON m.id=r.movie_id
                   WHERE length(COALESCE(m.overview,''))>20
                      OR COALESCE(m.keywords_json,'[]') NOT IN ('','[]')
                   ORDER BY COALESCE(NULLIF(r.date_rated,''),r.updated_at,''),r.id"""
            ).fetchall()
        out: list[_RatedText] = []
        for row in rows:
            keywords = [str(x) for x in (json_loads(row["keywords_json"], []) or [])]
            genres = tuple(str(x) for x in (json_loads(row["genres_json"], []) or []))
            sig = text_signature_from_values(str(row["overview"] or ""), keywords)
            if not sig:
                continue
            out.append(_RatedText(
                movie_id=int(row["id"]),
                rating=int(row["rating"]),
                date_key=str(row["date_key"] or ""),
                title_type=str(row["title_type"] or ""),
                genres=genres,
                signature=sig,
            ))
        return out

    @staticmethod
    def _fit(rows: list[_RatedText]) -> dict:
        if not rows:
            return {"global_mean": 6.5, "tokens": {}, "documents": 0}
        global_mean = sum(x.rating for x in rows) / len(rows)
        df: Counter[str] = Counter()
        residual_sum: dict[str, float] = defaultdict(float)
        weight_sum: dict[str, float] = defaultdict(float)
        for row in rows:
            seen = set(row.signature)
            df.update(seen)
            residual = float(row.rating) - global_mean
            for token, weight in row.signature.items():
                residual_sum[token] += residual * float(weight)
                weight_sum[token] += abs(float(weight))

        n_docs = len(rows)
        tokens = {}
        for token, docs in df.items():
            if docs < 3 or docs > max(8, int(n_docs * 0.42)):
                continue
            raw = residual_sum[token] / max(0.001, weight_sum[token])
            support = docs / (docs + 7.0)
            idf = math.log((1.0 + n_docs) / (1.0 + docs)) + 0.35
            pref = max(-2.5, min(2.5, raw * support))
            tokens[token] = {
                "preference": pref,
                "count": int(docs),
                "idf": min(3.0, idf),
            }
        return {"global_mean": global_mean, "tokens": tokens, "documents": n_docs}

    @staticmethod
    def _affinity(signature: dict[str, float], model: dict) -> tuple[float, list[tuple[str, float]]]:
        values = []
        for token, weight in signature.items():
            st = (model.get("tokens") or {}).get(token)
            if not st:
                continue
            contribution = (
                float(st.get("preference", 0.0))
                * float(st.get("idf", 1.0))
                * float(weight)
            )
            values.append((token, contribution))
        if not values:
            return 0.0, []
        values.sort(key=lambda item: abs(item[1]), reverse=True)
        top = values[:14]
        denom = sum(abs(v) for _t, v in top)
        if denom <= 0:
            return 0.0, top
        raw = sum(v for _t, v in top) / max(3.0, math.sqrt(denom) * 3.0)
        return max(-1.0, min(1.0, raw)), top

    @classmethod
    def _predict(cls, row: _RatedText, model: dict) -> float:
        affinity, _ = cls._affinity(row.signature, model)
        return max(
            1.0,
            min(10.0, float(model.get("global_mean", 6.5)) + affinity * 1.15),
        )

    def _build(self, rows: list[_RatedText]) -> tuple[dict, dict]:
        if len(rows) < self.MIN_TEXT_RATINGS:
            return self._fit(rows), {
                "approved": False,
                "reason": "insufficient_text_ratings",
                "count": len(rows),
                "minimum": self.MIN_TEXT_RATINGS,
            }
        cut = max(
            self.MIN_TEXT_RATINGS - self.MIN_HOLDOUT,
            int(len(rows) * 0.80),
        )
        cut = min(cut, len(rows) - self.MIN_HOLDOUT)
        train, holdout = rows[:cut], rows[cut:]
        model = self._fit(train)
        baseline = float(model.get("global_mean", 6.5))
        base_mae = sum(abs(float(x.rating) - baseline) for x in holdout) / len(holdout)
        sem_mae = sum(abs(float(x.rating) - self._predict(x, model)) for x in holdout) / len(holdout)
        gain = base_mae - sem_mae
        approved = bool(sem_mae <= base_mae * 0.995 and gain >= 0.02)
        status = {
            "approved": approved,
            "reason": "validated" if approved else "no_measured_gain",
            "count": len(rows),
            "training_count": len(train),
            "holdout_count": len(holdout),
            "baseline_mae": round(base_mae, 5),
            "semantic_mae": round(sem_mae, 5),
            "mae_gain": round(gain, 5),
            "token_count": int(len(model.get("tokens") or {})),
        }
        return (self._fit(rows) if approved else model), status

    def _ensure(self) -> None:
        token = self.state_token()
        with self._lock:
            if token == self._token:
                return
        rows = self._rows()
        model, status = self._build(rows)
        status = {"version": LEARNING_INSIGHT_VERSION, "state_token": list(token), **status}
        try:
            self.db.set_setting(SEMANTIC_SETTING, status)
        except Exception:
            pass
        with self._lock:
            self._model = model
            self._status = status
            self._token = token

    def status(self) -> dict:
        self._ensure()
        with self._lock:
            return dict(self._status)

    @staticmethod
    def _label(token: str) -> str:
        if token.startswith("kw:"):
            return token[3:].replace("_", " ")
        if token.startswith("bg:"):
            return token[3:].replace("_", " ")
        return token.split(":", 1)[-1].replace("_", " ")

    def apply(self, movie, score) -> dict:
        self._ensure()
        signature = text_signature(movie)
        affinity, evidence = self._affinity(signature, self._model)
        approved = bool(self._status.get("approved"))
        shift = max(
            -self.MAX_FINAL_SHIFT,
            min(self.MAX_FINAL_SHIFT, affinity * self.MAX_FINAL_SHIFT),
        ) if approved else 0.0
        if abs(shift) >= 1e-6:
            score.final = clamp(float(score.final) + shift)
            labels = [self._label(token) for token, value in evidence if value > 0][:3]
            reason = (
                "Sinopsisul/keyword-urile se potrivesc cu teme fine din filmele tale apreciate"
                + (": " + ", ".join(labels) if labels else "")
                + "."
            )
            score.contributions.append(("Semantică text 4.3", shift * 100.0, reason))
            score.score_factors["semantică text"] = shift
        return {
            "approved": approved,
            "affinity": affinity,
            "shift": shift,
            "evidence": [self._label(t) for t, _v in evidence[:5]],
        }


class LearningInsightBrainV43:
    MAX_EXPLORATION_SHIFT = 0.006

    def __init__(self, db):
        self.db = db
        self.calibrator = PredictionCalibratorV43(db)
        self.semantic = TextSemanticBrainV43(db)

    def state_token(self) -> tuple:
        return (
            LEARNING_INSIGHT_VERSION,
            self.calibrator.state_token(),
            self.semantic.state_token(),
        )

    def status(self) -> dict:
        return {
            "version": LEARNING_INSIGHT_VERSION,
            "calibration": self.calibrator.status(),
            "semantic": self.semantic.status(),
        }

    def enhance(self, rec, *, mode: str = "decide") -> None:
        semantic = self.semantic.apply(rec.movie, rec.score)
        calibration = self.calibrator.apply(rec.movie, rec.score)

        if mode == "surprise":
            s = rec.score
            predicted = float(getattr(s, "predicted_rating", 0.0) or 0.0)
            confidence = float(getattr(s, "confidence", 0.0) or 0.0)
            novelty = float(getattr(s, "novelty", 0.0) or 0.0)
            quality = float(getattr(s, "quality", 0.0) or 0.0)
            if predicted >= 6.4 and confidence >= .45 and quality >= .50:
                exploratory = clamp((novelty - .5) * .9 + (1.0 - confidence) * .25)
                shift = max(0.0, min(self.MAX_EXPLORATION_SHIFT, exploratory * .008))
                if shift:
                    s.final = clamp(float(s.final) + shift)
                    s.contributions.append(
                        (
                            "Explorare controlată",
                            shift * 100.0,
                            "Modul «Surprinde-mă» favorizează ușor noutatea numai în shortlist-ul "
                            "deja acceptat de motor; nu introduce un film nou în afara Top 3 validat.",
                        )
                    )
                    s.score_factors["explorare controlată"] = shift

        rec.score.score_factors["calibrare activă"] = 1.0 if calibration.get("approved") else 0.0
        rec.score.score_factors["semantică fină activă"] = 1.0 if semantic.get("approved") else 0.0


def learning_insight_engine_class(base_cls: type) -> type:
    if bool(getattr(base_cls, "_cinecalendar_v43_learning_insight", False)):
        return base_cls

    class LearningInsightV43(base_cls):
        _cinecalendar_v43_learning_insight = True
        LEARNING_INSIGHT_VERSION = LEARNING_INSIGHT_VERSION

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.learning_insight_v43 = LearningInsightBrainV43(self.db)

        def _state_token(self):
            return super()._state_token() + self.learning_insight_v43.state_token()

        def recommend(
            self, when=None, count=3, exclude_ids=None, record=False, slot="today",
            candidate_limit=100000, mode="decide", runtime_max=None, runtime_min=None,
        ):
            recs = super().recommend(
                when=when,
                count=count,
                exclude_ids=exclude_ids,
                record=False,
                slot=slot,
                candidate_limit=candidate_limit,
                mode=mode,
                runtime_max=runtime_max,
                runtime_min=runtime_min,
            )
            for rec in recs:
                self.learning_insight_v43.enhance(rec, mode=mode)
            # Only reorders the exact shortlist returned by the validated lower stack.
            recs.sort(
                key=lambda r: (
                    float(r.score.final),
                    float(r.score.predicted_rating),
                    float(r.score.confidence),
                ),
                reverse=True,
            )
            if record and recs:
                recorder = getattr(self, "_record_selected", None)
                if callable(recorder):
                    recorder(recs, when or date.today(), slot, len(recs))
            return recs

        def learning_insight_status(self) -> dict:
            return self.learning_insight_v43.status()

    LearningInsightV43.__name__ = f"{base_cls.__name__}LearningInsightV43"
    LearningInsightV43.__qualname__ = LearningInsightV43.__name__
    return LearningInsightV43


def comparison_reason(primary, alternative) -> str:
    """Explain why one already-ranked recommendation sits above another."""
    a = primary.score
    b = alternative.score
    points: list[tuple[float, str]] = []

    pred_gap = float(a.predicted_rating or 0.0) - float(b.predicted_rating or 0.0)
    if abs(pred_gap) >= .08:
        points.append((abs(pred_gap) / 10.0, f"estimarea personală este {a.predicted_rating:.1f} vs {b.predicted_rating:.1f}"))

    conf_gap = float(a.confidence or 0.0) - float(b.confidence or 0.0)
    if abs(conf_gap) >= .03:
        direction = "mai mare" if conf_gap > 0 else "mai mică"
        points.append((abs(conf_gap), f"încrederea este {direction} ({a.confidence*100:.0f}% vs {b.confidence*100:.0f}%)"))

    names = {
        "gust": "gustul personal",
        "ALS/context personal": "afinitatea cu istoricul",
        "regizor/cinematografie": "regizorul/cinematografia",
        "calitate": "calitatea externă",
        "noutate": "noutatea",
        "semantică text": "sinopsisul și keyword-urile",
        "evoluția gustului": "gustul recent",
    }
    af = dict(getattr(a, "score_factors", {}) or {})
    bf = dict(getattr(b, "score_factors", {}) or {})
    for key, label in names.items():
        gap = float(af.get(key, 0.0) or 0.0) - float(bf.get(key, 0.0) or 0.0)
        if abs(gap) >= .012:
            points.append((abs(gap), f"{label} favorizează " + ("prima alegere" if gap > 0 else "alternativa")))

    points.sort(key=lambda item: item[0], reverse=True)
    if not points:
        gap = (float(a.final) - float(b.final)) * 100.0
        return f"Sunt foarte apropiate; prima alegere are un avantaj final de aproximativ {gap:+.1f} puncte de potrivire."
    return "Prima alegere este înainte deoarece " + "; ".join(text for _w, text in points[:3]) + "."
