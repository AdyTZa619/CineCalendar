from __future__ import annotations
from datetime import date, datetime
import math
from .calendar_engine import CalendarEngine
from .db import Database
from .models import Movie, Recommendation, ScoreBreakdown
from .profile import get_profile
from .semantic import extract_semantic, feature_vector
from .util import clamp, cosine_sparse, json_loads, normalize_text, utcnow_iso

# The normal recommendation path is deliberately rating-first. Calendar remains useful,
# but it cannot overpower the user's explicit 1-10 history. Calendar mode adds its own
# contextual boost only when the user opens the calendar/month program.
WEIGHTS = {
    "taste": .52,
    "semantic": .19,
    "calendar": .04,
    "season": .02,
    "director_cinema": .09,
    "novelty": .04,
    "diversity": .03,
    "quality": .07,
}
ENGINE_VERSION = "2.0.0"

FEATURE_KIND_WEIGHT = {
    "director": 1.30,
    "theme": 1.15,
    "genre": 1.00,
    "country": .65,
    "decade": .55,
    "runtime": .35,
    "popularity": .25,
}


def row_to_movie(row) -> Movie:
    return Movie(
        id=row["id"], imdb_id=row["imdb_id"], title=row["title"], original_title=row["original_title"] or "",
        year=row["year"], title_type=row["title_type"] or "Movie", runtime_min=row["runtime_min"],
        genres=json_loads(row["genres_json"], []), directors=json_loads(row["directors_json"], []),
        countries=json_loads(row["countries_json"], []), overview=row["overview"] or "",
        keywords=json_loads(row["keywords_json"], []), imdb_rating=row["imdb_rating"], num_votes=row["num_votes"],
        release_date=row["release_date"], poster_url=row["poster_url"], source=row["source"],
        semantic=json_loads(row["semantic_json"], {}) or {},
    )


def romance_policy(movie: Movie, exclude_romance: bool = True) -> tuple[bool, float, str]:
    if not exclude_romance:
        return True, 0.0, ""
    genres = {g.lower() for g in movie.genres}
    sem = movie.semantic or extract_semantic(movie)
    has_romance = "romance" in genres or sem.get("romance", 0) >= .45
    if not has_romance:
        return True, 0.0, ""
    strong_non_romance = max([
        sem.get(x, 0) for x in (
            "history", "war", "christianity", "holocaust", "crime", "horror", "scifi",
            "documentary", "biography", "nature", "survival", "politics", "antiquity", "medieval"
        )
    ] + [0])
    hard = "romance" in genres and strong_non_romance < .55 and len(genres - {"romance", "drama", "comedy"}) == 0
    if hard:
        return False, 1.0, "Romance este dominant."
    penalty = .08 if strong_non_romance >= .7 else .16
    return True, penalty, "Romance pare secundar; s-a aplicat penalizare."


class RecommendationEngine:
    def __init__(self, db: Database, calendar: CalendarEngine | None = None):
        self.db = db
        self.calendar = calendar or CalendarEngine()

    def _eligible_sql(self) -> str:
        return """ FROM movies m
            LEFT JOIN ratings r ON r.movie_id=m.id
            WHERE r.movie_id IS NULL
              AND m.id NOT IN (SELECT movie_id FROM feedback WHERE kind IN ('not_interested','seen','never_similar'))
              AND lower(COALESCE(m.title_type,'movie')) IN ('movie','short','tvmovie','video','tv movie') """

    def _candidate_rows(self, when: date, limit: int = 100000):
        """Generate a mixed candidate pool instead of only the most popular titles.

        This keeps mainstream quality, recent films, hidden gems and explicit watchlist
        entries in play before personal ranking.
        """
        base = self._eligible_sql()
        popular_n = max(5000, int(limit * .52))
        hidden_n = max(3000, int(limit * .28))
        recent_n = max(2000, int(limit * .20))
        cutoff = when.year - 8
        with self.db.connect() as con:
            watch = con.execute(
                "SELECT m.*" + base + " AND m.id IN (SELECT movie_id FROM watchlist) ORDER BY COALESCE(m.num_votes,0) DESC"
            ).fetchall()
            popular = con.execute(
                "SELECT m.*" + base + " ORDER BY COALESCE(m.num_votes,0) DESC LIMIT ?", (popular_n,)
            ).fetchall()
            hidden = con.execute(
                "SELECT m.*" + base + " AND COALESCE(m.num_votes,0) BETWEEN 50 AND 15000 "
                "ORDER BY COALESCE(m.imdb_rating,0) DESC, COALESCE(m.num_votes,0) DESC LIMIT ?", (hidden_n,)
            ).fetchall()
            recent = con.execute(
                "SELECT m.*" + base + " AND COALESCE(m.year,0)>=? "
                "ORDER BY COALESCE(m.num_votes,0) DESC LIMIT ?", (cutoff, recent_n)
            ).fetchall()
        out, seen = [], set()
        for row in list(watch) + list(popular) + list(hidden) + list(recent):
            mid = int(row["id"])
            if mid in seen:
                continue
            seen.add(mid); out.append(row)
            if len(out) >= limit:
                break
        return out

    def _run_context(self) -> dict:
        """Load history/watchlist once. The old engine did database queries per candidate."""
        with self.db.connect() as con:
            hist_rows = con.execute("""SELECT movie_id, COUNT(*) AS c, MAX(recommended_at) AS latest,
                SUM(COALESCE(ignored,0)) AS ignored_count
                FROM recommendation_history GROUP BY movie_id""").fetchall()
            watch = {int(r[0]) for r in con.execute("SELECT movie_id FROM watchlist").fetchall()}
        history = {
            int(r["movie_id"]): {
                "count": int(r["c"] or 0),
                "latest": r["latest"],
                "ignored": int(r["ignored_count"] or 0),
            } for r in hist_rows
        }
        return {"history": history, "watchlist": watch}

    def _repeat_penalty(self, movie_id: int, when: date, context: dict) -> tuple[float, str]:
        info = context["history"].get(int(movie_id))
        if not info:
            return 0.0, ""
        try:
            d = datetime.fromisoformat(str(info["latest"]).replace("Z", "+00:00")).date()
        except Exception:
            d = when
        days = max(0, (when - d).days)
        count = info["count"]
        if days <= 14: p = .26
        elif days <= 60: p = .18
        elif days <= 120: p = .11
        elif days <= 365: p = .05
        else: p = 0.0
        p += min(.08, max(0, count - 2) * .02) + min(.04, info["ignored"] * .01)
        return min(.34, p), f"Recomandat de {count} ori; ignorat {info['ignored']} ori; ultima dată acum {days} zile."

    def _feature_kind(self, feature: str) -> str:
        return feature.split(":", 1)[0] if ":" in feature else "theme"

    def _human_feature(self, feature: str) -> str:
        kind, value = (feature.split(":", 1) + [feature])[:2] if ":" in feature else ("temă", feature)
        labels = {
            "genre": "gen", "theme": "temă", "director": "regizor", "country": "cinematografie",
            "decade": "deceniu", "runtime": "durată", "popularity": "popularitate",
        }
        return f"{labels.get(kind, kind)} {value}"

    def _predict_user_rating(self, movie: Movie, profile: dict) -> tuple[float, float, float, list[dict]]:
        """Predict the user's own 1-10 rating from their explicit rating history.

        Every matching feature votes with its observed mean rating, evidence count and
        stability. Sparse features are automatically shrunk so one title cannot dominate.
        """
        feats = profile.get("features", {})
        global_mean = float(profile.get("global_mean_rating", 6.5) or 6.5)
        vec = feature_vector(movie)
        num = den = evidence = 0.0
        details = []
        for feature, fw in vec.items():
            st = feats.get(feature)
            if not st or st.get("mean_rating") is None:
                continue
            count = max(0, int(st.get("count", 0) or 0))
            mean = float(st.get("mean_rating", global_mean))
            std = float(st.get("std_rating", 1.6) or 1.6)
            reliability = count / (count + 5.0)
            precision = 1.0 / (.75 + max(.35, std))
            kind_w = FEATURE_KIND_WEIGHT.get(self._feature_kind(feature), .5)
            w = max(.05, float(fw)) * kind_w * reliability * precision
            residual = mean - global_mean
            num += residual * w
            den += abs(w)
            evidence += max(.05, float(fw)) * kind_w * min(1.0, count / 12.0)
            details.append({
                "feature": feature, "mean": mean, "count": count, "std": std,
                "weight": w, "support": residual * w,
            })
        predicted = global_mean + (num / den if den else 0.0)
        predicted = max(1.0, min(10.0, predicted))
        metadata = 0.0
        if movie.genres: metadata += .35
        if movie.directors: metadata += .20
        if movie.runtime_min: metadata += .10
        if movie.semantic or movie.overview or movie.keywords: metadata += .25
        if movie.year: metadata += .10
        confidence = .20 + .62 * (1.0 - math.exp(-evidence / 2.8)) + .18 * metadata
        confidence = clamp(confidence)
        details.sort(key=lambda x: abs(x["support"]), reverse=True)
        return predicted, confidence, evidence, details

    def _semantic_score(self, movie: Movie, profile: dict) -> float:
        vec = feature_vector(movie)
        signed = cosine_sparse(vec, profile.get("semantic_vector", {}))
        positive = max(0.0, cosine_sparse(vec, profile.get("positive_vector", {})))
        elite = max(0.0, cosine_sparse(vec, profile.get("elite_vector", {})))
        negative = max(0.0, cosine_sparse(vec, profile.get("negative_vector", {})))
        return clamp(.46 + .16 * signed + .24 * positive + .20 * elite - .24 * negative)

    def _season_score(self, movie: Movie, when: date) -> tuple[float, str]:
        label, tags = self.calendar.season_phase(when)
        sem = movie.semantic or extract_semantic(movie)
        overlap = sum(min(1.0, sem.get(k, 0)) * v for k, v in tags.items()) / (sum(tags.values()) or 1)
        return clamp(.3 + .7 * overlap), label

    def _director_cinema(self, movie: Movie, profile: dict) -> float:
        feats = profile.get("features", {}); vals = []
        for d in movie.directors:
            s = feats.get("director:" + normalize_text(d))
            if s: vals.append(float(s["preference"]))
        for c in movie.countries:
            s = feats.get("country:" + normalize_text(c))
            if s: vals.append(float(s["preference"]) * .65)
        return clamp(.5 + .5 * (sum(vals) / len(vals) if vals else 0.0))

    def _quality(self, movie: Movie) -> float:
        if movie.imdb_rating is None:
            return .45
        votes = max(0, movie.num_votes or 0); m = 2500; prior = 6.5
        bayes = (votes / (votes + m)) * movie.imdb_rating + (m / (votes + m)) * prior
        return clamp((bayes - 4.0) / 5.0)

    def _novelty(self, movie: Movie, context: dict) -> float:
        info = context["history"].get(int(movie.id))
        h = info["count"] if info else 0
        base = 1.0 if h == 0 else max(.2, 1.0 - .18 * h)
        if int(movie.id) in context["watchlist"]:
            base = min(1.0, base + .08)
        return base

    def _mode_adjustment(self, mode: str, movie: Movie, confidence: float, novelty: float,
                         quality: float, calendar: float, season: float) -> tuple[float, str]:
        if mode == "safe":
            adj = .10 * (confidence - .5) + .06 * (quality - .5)
            return adj, "Mod sigur: favorizează dovezi multe și calitate stabilă."
        if mode == "surprise":
            adj = .08 * (novelty - .5) + .02 * (confidence - .5)
            return adj, "Mod surpriză: caută noutate fără să abandoneze gustul personal."
        if mode == "calendar":
            adj = .12 * (calendar - .5) + .05 * (season - .5)
            return adj, "Mod calendar: contextul perioadei primește un bonus suplimentar."
        if mode == "short":
            if movie.runtime_min and movie.runtime_min > 110:
                return -.20, "Mod scurt: penalizare pentru peste 110 minute."
            return .04, "Mod scurt: preferă titluri de cel mult aproximativ 110 minute."
        # Default decision mode: choose one film with high expected satisfaction and confidence.
        return .06 * (confidence - .5), "Mod decizie: încrederea ridicată sparge egalitățile."

    def _score_one(self, movie: Movie, when: date, profile: dict, context: dict,
                   exclude_romance: bool = True, mode: str = "decide") -> ScoreBreakdown | None:
        if not movie.semantic:
            movie.semantic = extract_semantic(movie)
        allowed, rom_penalty, rom_reason = romance_policy(movie, exclude_romance)
        if not allowed:
            return None

        predicted, confidence, evidence, details = self._predict_user_rating(movie, profile)
        taste = clamp(predicted / 10.0)
        semantic = self._semantic_score(movie, profile)
        cal, kind, cal_reason = self.calendar.calendar_relevance(movie, when)
        season, season_label = self._season_score(movie, when)
        dc = self._director_cinema(movie, profile)
        novelty = self._novelty(movie, context)
        quality = self._quality(movie)
        repeat_pen, repeat_reason = self._repeat_penalty(int(movie.id), when, context)
        diversity = .5

        subtotal = (
            taste * WEIGHTS["taste"] + semantic * WEIGHTS["semantic"] + cal * WEIGHTS["calendar"] +
            season * WEIGHTS["season"] + dc * WEIGHTS["director_cinema"] + novelty * WEIGHTS["novelty"] +
            diversity * WEIGHTS["diversity"] + quality * WEIGHTS["quality"]
        )
        mode_adj, mode_reason = self._mode_adjustment(mode, movie, confidence, novelty, quality, cal, season)
        uncertainty_penalty = max(0.0, .50 - confidence) * .10
        final = clamp(subtotal + mode_adj - uncertainty_penalty - repeat_pen - rom_penalty)

        positive = [d for d in details if d["support"] > 0]
        positive.sort(key=lambda d: d["support"], reverse=True)
        bits = []
        for d in positive[:3]:
            bits.append(f"{self._human_feature(d['feature'])}: {d['mean']:.1f}/10 din {d['count']} ratinguri")
        if bits:
            personal_reason = f"Estimare {predicted:.1f}/10 din tiparele tale — " + "; ".join(bits)
        else:
            personal_reason = f"Estimare {predicted:.1f}/10; metadatele acestui titlu oferă încă puține semnale personale."

        contrib = [
            ("Rating personal estimat", taste * WEIGHTS["taste"] * 100, personal_reason),
            ("Afinitate cu filmele apreciate", semantic * WEIGHTS["semantic"] * 100, "Compară profilul titlului cu filmele tale apreciate și penalizează tiparele din ratingurile mici."),
            ("Regizor/cinematografie", dc * WEIGHTS["director_cinema"] * 100, "Folosește preferințele învățate pentru regizori și cinematografii când există metadate."),
            ("Calitate externă", quality * WEIGHTS["quality"] * 100, "IMDb este doar un filtru/tie-breaker regularizat, nu motorul principal."),
            ("Noutate", novelty * WEIGHTS["novelty"] * 100, "Reduce repetarea acelorași recomandări."),
            ("Calendar", cal * WEIGHTS["calendar"] * 100, cal_reason),
            ("Anotimp", season * WEIGHTS["season"] * 100, season_label),
            ("Încredere model", mode_adj * 100, mode_reason),
        ]
        if uncertainty_penalty:
            contrib.append(("Incertitudine", -uncertainty_penalty * 100, "Metadatele sau dovezile din ratinguri sunt prea puține pentru o predicție fermă."))
        if repeat_pen:
            contrib.append(("Repetare", -repeat_pen * 100, repeat_reason))
        if rom_penalty:
            contrib.append(("Romance", -rom_penalty * 100, rom_reason))

        return ScoreBreakdown(
            taste=taste, semantic=semantic, calendar=cal, season=season, director_cinema=dc,
            novelty=novelty, diversity=diversity, quality=quality, repeat_penalty=repeat_pen,
            romance_penalty=rom_penalty, final=final, calendar_kind=kind, calendar_reason=cal_reason,
            personal_reason=personal_reason, contributions=contrib, predicted_rating=predicted,
            confidence=confidence, uncertainty=1.0-confidence, decision_mode=mode, evidence=evidence,
        )

    def recommend(self, when: date | None = None, count: int = 3, exclude_ids: set[int] | None = None,
                  record: bool = False, slot: str = "today", candidate_limit: int = 100000,
                  mode: str = "decide", runtime_max: int | None = None, runtime_min: int | None = None) -> list[Recommendation]:
        when = when or date.today(); exclude_ids = exclude_ids or set(); profile = get_profile(self.db)
        with self.db.tx() as con:
            con.execute("UPDATE recommendation_history SET ignored=1 WHERE ignored=0 AND action IS NULL AND context_date < ?", (when.isoformat(),))
        exclude_romance = bool(self.db.get_setting("exclude_romance", True))
        context = self._run_context()
        candidates = []
        for row in self._candidate_rows(when, candidate_limit):
            mid = int(row["id"])
            if mid in exclude_ids:
                continue
            m = row_to_movie(row)
            if runtime_max is not None and m.runtime_min is not None and m.runtime_min > runtime_max:
                continue
            if runtime_min is not None and m.runtime_min is not None and m.runtime_min < runtime_min:
                continue
            if mode == "short" and m.runtime_min is not None and m.runtime_min > 120:
                continue
            s = self._score_one(m, when, profile, context, exclude_romance, mode)
            if s is None:
                continue
            # Avoid high-confidence bad fits. Low-confidence items can still survive for exploration.
            if s.confidence >= .55 and s.predicted_rating < 5.8:
                continue
            candidates.append(Recommendation(m, s))
        candidates.sort(key=lambda r: (r.score.final, r.score.predicted_rating, r.score.confidence), reverse=True)

        selected = []
        pool = candidates[:max(250, count * 35)]
        while pool and len(selected) < count:
            best = None; best_value = -1.0
            for rec in pool:
                if not selected:
                    div = 1.0
                else:
                    div = 1.0 - max(cosine_sparse(feature_vector(rec.movie), feature_vector(x.movie)) for x in selected)
                diversity_weight = .055 if mode == "surprise" else WEIGHTS["diversity"]
                adjusted = rec.score.final + diversity_weight * (div - .5)
                if adjusted > best_value:
                    best_value = adjusted; best = (rec, div)
            rec, div = best
            rec.score.diversity = clamp(div); rec.score.final = clamp(best_value)
            rec.score.contributions.append(("Diversitate", rec.score.diversity * WEIGHTS["diversity"] * 100, "Evită o listă de recomandări aproape identice."))
            selected.append(rec); pool.remove(rec)

        if record and selected:
            now = utcnow_iso()
            with self.db.tx() as con:
                for rec in selected:
                    con.execute("INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score) VALUES(?,?,?,?,?)",
                                (rec.movie.id, now, when.isoformat(), slot, rec.score.final))
                con.execute("INSERT INTO recommendation_runs(context_date,slot,generated_at,candidate_count,result_count,engine_version) VALUES(?,?,?,?,?,?)",
                            (when.isoformat(), slot, now, len(candidates), len(selected), ENGINE_VERSION))
        return selected

    def decision_pick(self, when: date | None = None, exclude_ids: set[int] | None = None,
                      mode: str = "decide") -> tuple[Recommendation | None, list[Recommendation]]:
        """Return exactly one primary choice plus two backups to minimize decision fatigue."""
        recs = self.recommend(when or date.today(), 3, exclude_ids=exclude_ids, record=False,
                              slot="decision", candidate_limit=100000, mode=mode)
        return (recs[0] if recs else None, recs[1:3] if len(recs) > 1 else [])

    def month_program(self, start: date | None = None, count_per_group: int = 3, record: bool = False):
        start = start or date.today(); groups = self.calendar.month_groups(start); used = set(); result = []
        for gstart, gend, label in groups:
            midpoint = gstart + (gend - gstart) // 2
            recs = self.recommend(midpoint, count_per_group, exclude_ids=used, record=record,
                                  slot=f"month:{gstart}:{gend}", candidate_limit=40000, mode="calendar")
            used.update(r.movie.id for r in recs)
            result.append((gstart, gend, label, recs))
        return result
