from __future__ import annotations

from datetime import date

from .models import Recommendation
from .profile import get_profile
from .recommendation import romance_policy, row_to_movie
from .recommender_v5 import FastRecommendationEngineV5
from .semantic import extract_semantic
from .util import clamp


ENGINE_VERSION = "6.1.0"

# Events in this map require a genuinely event-specific semantic anchor.  A generic
# History/Drama/War label is never enough for these dates.  Precision is deliberately
# preferred over filling every lane with weak matches.
STRICT_EVENT_ANCHORS: dict[str, tuple[str, ...]] = {
    "exaltation_cross": ("cross_veneration", "passion_of_christ"),
    "good_friday": ("passion_of_christ", "cross_veneration"),
    "holy_thursday": ("passion_of_christ", "cross_veneration"),
    "holy_saturday": ("passion_of_christ", "cross_veneration"),
    "holy_week": ("passion_of_christ", "cross_veneration"),
    "palm_sunday": ("passion_of_christ",),
    "nativity": ("christmas",),
    "easter": ("easter",),
    "bright_week": ("easter",),
    "holocaust_day": ("holocaust",),
    "romania_national": ("romania",),
    "romanian_revolution": ("revolution", "communism"),
}

TAG_LABELS = {
    "cross_veneration": "Sfânta Cruce",
    "passion_of_christ": "Patimile/Răstignirea lui Hristos",
    "christianity": "creștinism",
    "faith": "credință",
    "easter": "Paști/Înviere",
    "christmas": "Nașterea Domnului/Crăciun",
    "holocaust": "Holocaust",
    "romania": "România",
    "revolution": "revoluție",
    "communism": "comunism",
    "saints": "sfinți/martiri",
    "monasticism": "monahism",
    "war": "război",
    "peace": "pace",
    "history": "istorie",
    "medieval": "Evul Mediu",
    "antiquity": "Antichitate",
    "nature": "natură",
    "family": "familie",
    "contemplative": "atmosferă contemplativă",
    "dark": "ton sobru",
    "autumn": "toamnă",
    "winter": "iarnă",
    "spring": "primăvară",
    "summer": "vară",
}


class FastRecommendationEngineV6(FastRecommendationEngineV5):
    """Fast calendar recommender with strict, explainable event relevance.

    The calendar view is precision-first: a title is not allowed to claim a historical or
    spiritual relation merely because it shares a broad genre such as History.  When an
    event has a concrete anchor (Holy Cross, Holocaust, Romania, Easter, etc.), secondary
    relations must also carry that anchor.  If there are no genuine matches, the UI shows
    fewer results instead of fabricating relevance.
    """

    CALENDAR_POOL = 2500
    CALENDAR_FINALISTS = 380

    def __init__(self, db, calendar=None):
        super().__init__(db, calendar)
        self._calendar_program_cache: dict[tuple, dict] = {}
        self.last_calendar_pre_rank_count = 0
        self.last_calendar_full_score_count = 0

    @staticmethod
    def _calendar_rank(rec: Recommendation) -> float:
        s = rec.score
        return (
            .60 * float(s.calendar) +
            .18 * float(s.final) +
            .10 * clamp(float(s.predicted_rating) / 10.0) +
            .07 * float(s.confidence) +
            .05 * float(s.season)
        )

    @staticmethod
    def _tag_strength(sem: dict[str, float], tags) -> tuple[float, str | None]:
        best_strength = 0.0
        best_tag = None
        for tag in tags:
            strength = float(sem.get(tag, 0.0) or 0.0)
            if strength > best_strength:
                best_strength = strength
                best_tag = tag
        return best_strength, best_tag

    @staticmethod
    def _tag_label(tag: str | None) -> str:
        if not tag:
            return "semnal tematic verificat"
        return TAG_LABELS.get(tag, tag.replace("_", " "))

    def _strict_event_relation(self, ev, proximity: float, sem: dict[str, float]):
        """Return a relation only when the movie carries evidence specific to the event.

        This is the hard precision gate used by Program calendar.  In particular, for the
        Exaltation of the Holy Cross, `History` or `Medieval` alone produces zero relevance.
        """
        religious = ev.category in {"ortodox", "perioada_ortodoxa"}
        strict_tags = STRICT_EVENT_ANCHORS.get(ev.key)
        anchor_tags = strict_tags if strict_tags is not None else tuple(ev.direct_tags)
        anchor_strength, anchor_tag = self._tag_strength(sem, anchor_tags)

        direct_tags = strict_tags if strict_tags is not None else tuple(ev.direct_tags)
        direct, direct_tag = self._tag_strength(sem, direct_tags)
        historical, historical_tag = self._tag_strength(sem, ev.historical_tags)
        spiritual, spiritual_tag = self._tag_strength(sem, ev.spiritual_tags)
        atmosphere, atmosphere_tag = self._tag_strength(sem, ev.atmosphere_tags)

        if strict_tags is not None:
            # A strict feast/commemoration cannot inherit relevance from generic history,
            # faith or atmosphere unless the event-specific anchor is present too.
            historical *= anchor_strength
            spiritual *= anchor_strength
        elif religious:
            if ev.direct_tags:
                # A religious feast with a direct anchor requires that anchor before a
                # broad historical/spiritual label can be presented as feast relevance.
                historical *= anchor_strength
                if ev.category != "perioada_ortodoxa":
                    spiritual *= anchor_strength
            else:
                # Fasting periods legitimately use spiritual/monastic/contemplative themes.
                historical *= spiritual
        elif ev.direct_tags:
            # Historical/civic events with a concrete subject (Romania, Holocaust, nature,
            # peace...) require that subject before generic History/War can qualify.
            historical *= anchor_strength
            spiritual *= anchor_strength
        else:
            # No direct anchor: require corroboration from a second dimension rather than
            # accepting History/War on its own.
            corroboration = max(spiritual, atmosphere)
            historical *= corroboration

        candidates = [
            ("directă", direct, 1.00, direct_tag or anchor_tag),
            ("istorică", historical, .72, anchor_tag or historical_tag),
            ("spirituală", spiritual, .58, anchor_tag or spiritual_tag),
            ("atmosferică", atmosphere, .46, atmosphere_tag),
        ]
        kind, raw, multiplier, evidence_tag = max(candidates, key=lambda item: item[1] * item[2])
        score = clamp(raw * multiplier * float(ev.importance) * float(proximity))
        if score < .12:
            return 0.0, "slabă", "Fără legătură calendaristică verificabilă."

        reason = (
            f"{ev.name}: legătură {kind} prin {self._tag_label(evidence_tag)}."
        )
        return score, kind, reason

    def _calendar_score_cached(self, movie, when: date):
        sem = movie.semantic or extract_semantic(movie)
        events, _season_label, _season_tags = self._date_context(when)
        best = (0.0, "slabă", "Fără reper calendaristic puternic.")
        for ev, proximity in events:
            relation = self._strict_event_relation(ev, proximity, sem)
            if relation[0] > best[0]:
                best = relation
        return best

    def calendar_day_program(self, when: date | None = None, count_per_section: int = 6) -> dict:
        when = when or date.today()
        count_per_section = max(3, min(8, int(count_per_section or 6)))
        cache_key = (when.isoformat(), count_per_section, self._state_token(), ENGINE_VERSION)
        cached = self._calendar_program_cache.get(cache_key)
        if cached is not None:
            return cached

        profile = get_profile(self.db)
        if int(profile.get("rated_count", 0) or 0) < self.MIN_PERSONAL_RATINGS:
            raise RuntimeError(
                "Nu am suficiente ratinguri personale încărcate pentru recomandări. "
                "CineCalendar nu va inventa un scor «pentru tine»."
            )

        context = self._run_context()
        exclude_romance = bool(self.db.get_setting("exclude_romance", True))
        rows = self._candidate_rows(when, self.CALENDAR_POOL)

        rough = []
        for row in rows:
            movie = row_to_movie(row)
            if not movie.semantic:
                movie.semantic = extract_semantic(movie)
            allowed, _penalty, _reason = romance_policy(movie, exclude_romance)
            if not allowed:
                continue
            calendar_score, kind, reason = self._calendar_score_cached(movie, when)
            season_score, _season_label = self._season_score_cached(movie, when)
            affinity, evidence = self._cheap_personal_affinity(movie, profile)
            quality = self._quality(movie)
            novelty = self._novelty(movie, context)
            value = (
                .52 * calendar_score + .22 * affinity + .10 * quality +
                .06 * season_score + .05 * evidence + .05 * novelty
            )
            rough.append((value, movie, calendar_score, kind, season_score, reason))

        rough.sort(key=lambda item: item[0], reverse=True)
        self.last_calendar_pre_rank_count = len(rough)

        chosen: list = []
        chosen_ids: set[int] = set()

        def add_many(items, limit: int):
            for item in items[:limit]:
                movie = item[1]
                mid = int(movie.id)
                if mid in chosen_ids:
                    continue
                chosen.append(movie)
                chosen_ids.add(mid)
                if len(chosen) >= self.CALENDAR_FINALISTS:
                    return

        # Calendar-relevant candidates first.  The personal/quality tail exists only so the
        # seasonal lane can still work on ordinary days; it cannot enter a strict event lane
        # without passing _calendar_score_cached again during full scoring.
        relevant_rough = [item for item in rough if item[2] >= .12]
        add_many(relevant_rough, 260)
        add_many(rough, 120)
        for relation in ("directă", "spirituală", "istorică", "atmosferică"):
            relation_items = sorted(
                (item for item in rough if item[3] == relation and item[2] >= .12),
                key=lambda item: (item[2], item[0]), reverse=True,
            )
            add_many(relation_items, 34)
        seasonal = sorted(
            (item for item in rough if item[4] > .34),
            key=lambda item: (item[4], item[0]), reverse=True,
        )
        add_many(seasonal, 40)

        scored: list[Recommendation] = []
        for movie in chosen:
            score = self._score_one(movie, when, profile, context, exclude_romance, mode="calendar")
            if score is None:
                continue
            if score.confidence >= .55 and score.predicted_rating < 5.8:
                continue
            scored.append(Recommendation(movie, score))

        self.last_calendar_full_score_count = len(chosen)
        self._assert_no_blocked_leak(scored)
        scored.sort(key=self._calendar_rank, reverse=True)

        def lane(kind: str | None = None, min_calendar: float = .12, season_only: bool = False):
            out = []
            for rec in scored:
                s = rec.score
                if season_only:
                    if s.season <= .34:
                        continue
                else:
                    if s.calendar < min_calendar:
                        continue
                    if kind is not None and s.calendar_kind != kind:
                        continue
                out.append(rec)
            if season_only:
                out.sort(key=lambda r: (r.score.season, self._calendar_rank(r)), reverse=True)
            else:
                out.sort(key=lambda r: (r.score.calendar, self._calendar_rank(r)), reverse=True)
            return out

        active_events = self.calendar.relevant_events(when)
        has_specific_event = any(
            ev.category != "sezon" and float(ev.importance) * float(proximity) >= .45
            for ev, proximity in active_events
        )

        # The summary lane contains only actual direct/spiritual/historical relations.  On a
        # concrete feast/commemoration we never back-fill it with generic seasonal films.
        exact = [
            r for r in scored
            if r.score.calendar >= .16 and r.score.calendar_kind in {"directă", "spirituală", "istorică"}
        ]
        exact.sort(key=self._calendar_rank, reverse=True)
        if not exact and not has_specific_event:
            exact = [r for r in scored if r.score.season > .40]
            exact.sort(key=lambda r: (r.score.season, r.score.final), reverse=True)

        specs = [
            ("exact", "Pentru ziua asta", "Doar filme cu o legătură verificabilă cu ziua/perioada; gustul tău decide ordinea.", exact),
            ("direct", "Legătură directă", "Filme cu o legătură tematică explicită cu reperul calendaristic activ.", lane("directă", .16)),
            ("spiritual", "Legătură spirituală", "Teme spirituale acceptate numai când există și dovadă relevantă pentru reperul zilei.", lane("spirituală", .15)),
            ("historical", "Legătură istorică", "Istoria generică nu este suficientă; filmul trebuie să aibă și subiectul concret al zilei.", lane("istorică", .18)),
            ("atmosphere", "Atmosfera zilei", "Ton/anotimp potrivit, separat clar de legătura factuală cu evenimentul.", lane("atmosferică", .14)),
            ("season", "Sezon și perioadă", "Potrivire cu momentul anului; nu este prezentată ca legătură directă cu sărbătoarea.", lane(season_only=True)),
        ]

        sections = []
        used_specialized: set[int] = set()
        for key, title, subtitle, recs in specs:
            picked = []
            for rec in recs:
                mid = int(rec.movie.id)
                if key != "exact" and mid in used_specialized:
                    continue
                picked.append(rec)
                if key != "exact":
                    used_specialized.add(mid)
                if len(picked) >= count_per_section:
                    break
            if picked:
                sections.append({"key": key, "title": title, "subtitle": subtitle, "recommendations": picked})

        phase, season_tags = self.calendar.season_phase(when)
        result = {
            "date": when,
            "phase": phase,
            "season_tags": dict(season_tags),
            "events": active_events,
            "sections": sections,
            "pre_rank_count": self.last_calendar_pre_rank_count,
            "full_score_count": self.last_calendar_full_score_count,
        }

        if len(self._calendar_program_cache) >= 12:
            self._calendar_program_cache.clear()
        self._calendar_program_cache[cache_key] = result
        return result
