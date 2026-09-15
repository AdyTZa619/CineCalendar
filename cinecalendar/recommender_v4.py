from __future__ import annotations

from datetime import date

from .recommendation import WEIGHTS, romance_policy
from .recommender_v3 import FastRecommendationEngine
from .semantic import extract_semantic
from .util import clamp


ENGINE_VERSION = "4.0.0"


class FastRecommendationEngineV4(FastRecommendationEngine):
    """v3 shortlist/identity safety plus date-context caching.

    Calendar events, Orthodox movable dates and season phase are properties of the requested
    day, not of each movie. v2/v3 rebuilt that context for every candidate. This class computes
    the date context once and only evaluates each movie against the cached tags/events.
    """

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self._date_context_cache: dict[str, tuple] = {}

    def _date_context(self, when: date):
        key = when.isoformat()
        cached = self._date_context_cache.get(key)
        if cached is not None:
            return cached
        events = tuple(self.calendar.relevant_events(when))
        season_label, season_tags = self.calendar.season_phase(when)
        cached = (events, season_label, dict(season_tags))
        # Recommendations normally care about today/a few calendar dates; keep cache tiny.
        if len(self._date_context_cache) >= 16:
            self._date_context_cache.clear()
        self._date_context_cache[key] = cached
        return cached

    def _calendar_score_cached(self, movie, when: date):
        sem = movie.semantic or extract_semantic(movie)
        events, _season_label, _season_tags = self._date_context(when)
        best_score = 0.0
        best_kind = "slabă"
        best_reason = "Fără reper calendaristic puternic."
        for ev, proximity in events:
            direct = max((sem.get(t, 0) for t in ev.direct_tags), default=0)
            historical = max((sem.get(t, 0) for t in ev.historical_tags), default=0)
            spiritual = max((sem.get(t, 0) for t in ev.spiritual_tags), default=0)
            atmosphere = max((sem.get(t, 0) for t in ev.atmosphere_tags), default=0)
            kinds = [
                ("directă", direct, 1.0),
                ("istorică", historical, .72),
                ("spirituală", spiritual, .58),
                ("atmosferică", atmosphere, .46),
            ]
            kind, val, mult = max(kinds, key=lambda x: x[1] * x[2])
            score = clamp(val * mult * ev.importance * proximity)
            if score > best_score:
                best_score = score
                best_kind = kind if score >= .12 else "slabă"
                best_reason = f"{ev.name}: relevanță {best_kind}."
        return best_score, best_kind, best_reason

    def _season_score_cached(self, movie, when: date):
        sem = movie.semantic or extract_semantic(movie)
        _events, label, tags = self._date_context(when)
        overlap = sum(min(1.0, sem.get(k, 0)) * v for k, v in tags.items()) / (sum(tags.values()) or 1)
        return clamp(.3 + .7 * overlap), label

    def _score_one(self, movie, when, profile, context, exclude_romance=True, mode="decide"):
        if not movie.semantic:
            movie.semantic = extract_semantic(movie)
        allowed, rom_penalty, rom_reason = romance_policy(movie, exclude_romance)
        if not allowed:
            return None

        predicted, confidence, evidence, details = self._predict_user_rating(movie, profile)
        taste = clamp(predicted / 10.0)
        semantic = self._semantic_score(movie, profile)
        cal, kind, cal_reason = self._calendar_score_cached(movie, when)
        season, season_label = self._season_score_cached(movie, when)
        dc = self._director_cinema(movie, profile)
        novelty = self._novelty(movie, context)
        quality = self._quality(movie)
        repeat_pen, repeat_reason = self._repeat_penalty(int(movie.id), when, context)
        diversity = .5

        subtotal = (
            taste * WEIGHTS["taste"] + semantic * WEIGHTS["semantic"] +
            cal * WEIGHTS["calendar"] + season * WEIGHTS["season"] +
            dc * WEIGHTS["director_cinema"] + novelty * WEIGHTS["novelty"] +
            diversity * WEIGHTS["diversity"] + quality * WEIGHTS["quality"]
        )
        mode_adj, mode_reason = self._mode_adjustment(
            mode, movie, confidence, novelty, quality, cal, season
        )
        uncertainty_penalty = max(0.0, .50 - confidence) * .10
        final = clamp(subtotal + mode_adj - uncertainty_penalty - repeat_pen - rom_penalty)

        positive = [d for d in details if d["support"] > 0]
        positive.sort(key=lambda d: d["support"], reverse=True)
        bits = [
            f"{self._human_feature(d['feature'])}: {d['mean']:.1f}/10 din {d['count']} ratinguri"
            for d in positive[:3]
        ]
        if bits:
            personal_reason = f"Estimare {predicted:.1f}/10 din tiparele tale — " + "; ".join(bits)
        else:
            personal_reason = (
                f"Estimare {predicted:.1f}/10; metadatele acestui titlu oferă încă puține semnale personale."
            )

        contrib = [
            ("Rating personal estimat", taste * WEIGHTS["taste"] * 100, personal_reason),
            ("Afinitate cu filmele apreciate", semantic * WEIGHTS["semantic"] * 100,
             "Compară profilul titlului cu filmele tale apreciate și penalizează tiparele din ratingurile mici."),
            ("Regizor/cinematografie", dc * WEIGHTS["director_cinema"] * 100,
             "Folosește preferințele învățate pentru regizori și cinematografii când există metadate."),
            ("Calitate externă", quality * WEIGHTS["quality"] * 100,
             "IMDb este doar un filtru/tie-breaker regularizat, nu motorul principal."),
            ("Noutate", novelty * WEIGHTS["novelty"] * 100,
             "Reduce repetarea acelorași recomandări."),
            ("Calendar", cal * WEIGHTS["calendar"] * 100, cal_reason),
            ("Anotimp", season * WEIGHTS["season"] * 100, season_label),
            ("Încredere model", mode_adj * 100, mode_reason),
        ]
        if uncertainty_penalty:
            contrib.append(("Incertitudine", -uncertainty_penalty * 100,
                            "Metadatele sau dovezile din ratinguri sunt prea puține pentru o predicție fermă."))
        if repeat_pen:
            contrib.append(("Repetare", -repeat_pen * 100, repeat_reason))
        if rom_penalty:
            contrib.append(("Romance", -rom_penalty * 100, rom_reason))

        # v3's evidence honesty rules: low-evidence predictions are not promoted as strong fits.
        if evidence < .08:
            evidence_penalty = .18
            final = clamp(final - evidence_penalty)
            personal_reason = (
                f"Date personale insuficiente pentru o predicție fermă. {predicted:.1f}/10 "
                "este aproape de media ta generală, nu o potrivire confirmată."
            )
            contrib[0] = ("Rating personal estimat", taste * WEIGHTS["taste"] * 100, personal_reason)
            contrib.append(("Dovezi personale insuficiente", -evidence_penalty * 100,
                            "Titlul nu are încă suficiente caracteristici comparabile cu ratingurile tale."))
        elif evidence < .35:
            evidence_penalty = .08
            final = clamp(final - evidence_penalty)
            contrib.append(("Dovezi personale puține", -evidence_penalty * 100,
                            "Predicția rămâne conservatoare până există mai multe semnale personale."))

        # Avoid importing the class through a second module path; use the same model class
        # returned by the base recommendation engine.
        from .models import ScoreBreakdown
        return ScoreBreakdown(
            taste=taste, semantic=semantic, calendar=cal, season=season,
            director_cinema=dc, novelty=novelty, diversity=diversity, quality=quality,
            repeat_penalty=repeat_pen, romance_penalty=rom_penalty, final=final,
            calendar_kind=kind, calendar_reason=cal_reason, personal_reason=personal_reason,
            contributions=contrib, predicted_rating=predicted, confidence=confidence,
            uncertainty=1.0-confidence, decision_mode=mode, evidence=evidence,
        )
