from __future__ import annotations

from datetime import date, timedelta
import math

from .calendar_engine import orthodox_easter
from .calendar_engine_v2 import RichCalendarEngine
from .models import CalendarEvent, Movie
from .semantic import extract_semantic
from .util import clamp, normalize_text


CONTEXT_ENGINE_VERSION = "calendar-context-v3.5.0"


class ContextCalendarEngineV35(RichCalendarEngine):
    """Context-intelligence layer for CineCalendar 3.5.

    Keeps the curated Romanian/Orthodox calendar from v2, but makes three things explicit:
    * fixed observances, fasting periods and civic/cultural dates are enriched instead of
      being treated as interchangeable keyword matches;
    * influence around a date decays smoothly instead of behaving like a binary switch;
    * explanations separate concrete event relevance from seasonal atmosphere.

    The recommender still owns taste ranking. This class only supplies bounded context.
    """

    CONTEXT_VERSION = CONTEXT_ENGINE_VERSION

    _EXTRA_PATTERNS: dict[str, tuple[str, ...]] = {
        "women": ("woman", "women", "female", "femeie", "femei", "mother", "mama"),
        "children": ("child", "children", "kid", "kids", "copil", "copii", "childhood"),
        "labour": ("worker", "workers", "labour", "labor", "factory", "muncitor", "munca", "strike"),
        "education": ("school", "student", "teacher", "education", "scoala", "elev", "profesor"),
        "literature": ("writer", "poet", "poetry", "literature", "author", "scriitor", "poet", "roman"),
        "art": ("artist", "art", "sculpt", "painter", "painting", "arta", "sculptor", "pictor"),
        "travel": ("travel", "journey", "trip", "tourism", "calator", "vacation"),
        "human_rights": ("human rights", "civil rights", "rights activist", "drepturile omului"),
        "terrorism": ("terrorist", "terrorism", "terror attack", "attentat"),
        "ww1": ("world war i", "first world war", "wwi", "1914", "1918"),
        "ww2": ("world war ii", "second world war", "wwii", "1939", "1945", "nazi"),
        "folk": ("folklore", "folk", "tradition", "traditional", "folclor", "traditie"),
    }

    def semantic_for(self, movie: Movie) -> dict[str, float]:
        """Return movie semantics plus a small context-only vocabulary.

        This does not rebuild the user's taste profile and does not mutate stored catalog rows.
        It only lets calendar dates such as 8 March or 1 June recognise relevant subjects.
        """
        merged = dict(movie.semantic or {})
        for key, value in extract_semantic(movie).items():
            merged[key] = max(float(merged.get(key, 0.0) or 0.0), float(value or 0.0))
        text = normalize_text(" ".join([
            movie.title or "",
            movie.original_title or "",
            movie.overview or "",
            " ".join(movie.keywords or []),
        ]))
        for tag, patterns in self._EXTRA_PATTERNS.items():
            hits = sum(1 for p in patterns if normalize_text(p) in text)
            if hits:
                merged[tag] = max(float(merged.get(tag, 0.0) or 0.0), min(1.0, .48 + .17 * hits))
        return merged

    def events_for_year(self, year: int) -> list[CalendarEvent]:
        events = list(super().events_for_year(year))
        by_key = {event.key: event for event in events}

        def enrich(
            key: str,
            *,
            themes: dict[str, float] | None = None,
            direct=(),
            historical=(),
            spiritual=(),
            atmosphere=(),
            before: int | None = None,
            after: int | None = None,
        ) -> None:
            event = by_key.get(key)
            if event is None:
                return
            for name, value in (themes or {}).items():
                event.themes[name] = max(float(event.themes.get(name, 0.0) or 0.0), float(value))
            event.direct_tags.update(direct)
            event.historical_tags.update(historical)
            event.spiritual_tags.update(spiritual)
            event.atmosphere_tags.update(atmosphere)
            if before is not None:
                event.influence_before = max(int(event.influence_before or 0), int(before))
            if after is not None:
                event.influence_after = max(int(event.influence_after or 0), int(after))

        # Dates the user explicitly expects to carry meaningful context.
        enrich(
            "romanian_culture",
            themes={"romania": 1.0, "literature": .95, "art": .55, "biography": .55},
            direct=("romania", "literature"), historical=("history",), atmosphere=("winter", "contemplative"),
            before=2, after=1,
        )
        enrich(
            "womens_day",
            themes={"women": 1.0, "biography": .55, "family": .45, "history": .35},
            direct=("women",), historical=("history",), atmosphere=("spring", "family"),
            before=1, after=1,
        )
        enrich(
            "labour_day",
            themes={"labour": 1.0, "history": .55, "politics": .35},
            direct=("labour",), historical=("history",), atmosphere=("spring",),
            before=1, after=1,
        )
        enrich(
            "children_day",
            themes={"children": 1.0, "family": .9, "hopeful": .55},
            direct=("children", "family"), atmosphere=("hopeful", "summer"),
            before=1, after=1,
        )
        enrich(
            "ww2_start",
            themes={"ww2": 1.0, "war": 1.0, "history": .95},
            direct=("ww2", "war"), historical=("history",), before=2, after=1,
        )
        enrich(
            "exaltation_cross",
            themes={"cross_veneration": 1.0, "christianity": .8, "faith": .75},
            direct=("cross_veneration",), spiritual=("christianity", "faith"),
            before=2, after=2,
        )
        enrich(
            "romania_national",
            themes={"romania": 1.0, "history": .95},
            direct=("romania",), historical=("history",), before=3, after=2,
        )
        enrich(
            "great_lent",
            themes={"christianity": .7, "faith": .85, "contemplative": .85, "monasticism": .55},
            spiritual=("christianity", "faith", "monasticism"), atmosphere=("contemplative",),
        )
        enrich(
            "nativity_fast",
            themes={"christianity": .68, "faith": .78, "contemplative": .72, "christmas": .45},
            spiritual=("christianity", "faith", "monasticism"), atmosphere=("contemplative", "winter"),
        )
        enrich(
            "dormition_fast",
            themes={"christianity": .65, "faith": .8, "contemplative": .75},
            spiritual=("christianity", "faith", "monasticism"), atmosphere=("contemplative", "summer"),
        )

        # 1 September has two honest contexts: a major historical anchor and the transition
        # into early autumn / return-to-routine atmosphere. The latter is intentionally weak,
        # so it cannot pretend to be a historical match.
        if "september_transition" not in by_key:
            transition = CalendarEvent(
                key="september_transition",
                name="Început de septembrie • revenire la ritmul de toamnă",
                start=date(year, 9, 1),
                end=date(year, 9, 7),
                category="sezon",
                importance=.42,
                themes={"autumn": .85, "education": .45, "contemplative": .25},
                direct_tags=set(),
                historical_tags=set(),
                spiritual_tags=set(),
                atmosphere_tags={"autumn", "contemplative"},
                influence_before=0,
                influence_after=2,
            )
            events.append(transition)

        return sorted(events, key=lambda e: (e.start, e.end, -e.importance, e.name))

    @staticmethod
    def _proximity(event: CalendarEvent, when: date) -> float:
        if event.start <= when <= event.end:
            return 1.0
        if when < event.start:
            distance = (event.start - when).days
            window = int(event.influence_before or 0)
        else:
            distance = (when - event.end).days
            window = int(event.influence_after or 0)
        if window <= 0 or distance > window:
            return 0.0
        # Smooth decay: close days remain meaningful, edge days fade strongly.
        x = distance / float(window + 1)
        return clamp(max(.18, math.pow(max(0.0, 1.0 - x), .78)))

    def relevant_events(self, when: date) -> list[tuple[CalendarEvent, float]]:
        events = list(self.events_for_year(when.year))
        if when.month == 1:
            events.extend(self.events_for_year(when.year - 1))
        if when.month == 12:
            events.extend(self.events_for_year(when.year + 1))
        out: list[tuple[CalendarEvent, float]] = []
        seen: set[tuple[str, date, date]] = set()
        for event in events:
            key = (event.key, event.start, event.end)
            if key in seen:
                continue
            seen.add(key)
            proximity = self._proximity(event, when)
            if proximity > 0:
                out.append((event, proximity))
        out.sort(key=lambda item: (item[0].importance * item[1], item[1], item[0].importance), reverse=True)
        return out

    def season_phase(self, when: date) -> tuple[str, dict[str, float]]:
        events = self.relevant_events(when)
        active = {event.key for event, proximity in events if proximity >= .99}
        if "great_lent" in active:
            return "Postul Mare", {
                "christianity": .75, "faith": .85, "contemplative": .9,
                "monasticism": .5, "spring": .25,
            }
        if "nativity_fast" in active:
            return "Postul Nașterii Domnului", {
                "christianity": .65, "faith": .78, "contemplative": .76,
                "christmas": .42, "winter": .35,
            }
        if "dormition_fast" in active:
            return "Postul Adormirii Maicii Domnului", {
                "christianity": .62, "faith": .78, "contemplative": .72, "summer": .28,
            }
        if date(when.year, 9, 1) <= when <= date(when.year, 9, 10):
            return "început de septembrie", {
                "autumn": .85, "contemplative": .24, "education": .35, "summer": .18,
            }
        return super().season_phase(when)

    def calendar_relevance(self, movie: Movie, when: date) -> tuple[float, str, str]:
        sem = self.semantic_for(movie)
        best_score = 0.0
        best_kind = "slabă"
        best_reason = "Fără reper calendaristic puternic."
        best_event: CalendarEvent | None = None
        best_proximity = 0.0

        for event, proximity in self.relevant_events(when):
            direct = max((sem.get(tag, 0.0) for tag in event.direct_tags), default=0.0)
            historical = max((sem.get(tag, 0.0) for tag in event.historical_tags), default=0.0)
            spiritual = max((sem.get(tag, 0.0) for tag in event.spiritual_tags), default=0.0)
            atmosphere = max((sem.get(tag, 0.0) for tag in event.atmosphere_tags), default=0.0)
            kinds = [
                ("directă", direct, 1.0),
                ("istorică", historical, .72),
                ("spirituală", spiritual, .58),
                ("atmosferică", atmosphere, .42),
            ]
            kind, value, multiplier = max(kinds, key=lambda item: item[1] * item[2])
            score = clamp(value * multiplier * float(event.importance) * float(proximity))
            # Atmosphere is allowed to refine, never to impersonate a concrete event match.
            if kind == "atmosferică":
                score = min(score, .34)
            if score > best_score:
                best_score = score
                best_kind = kind if score >= .12 else "slabă"
                best_event = event
                best_proximity = proximity

        if best_event is not None:
            if best_proximity >= .99:
                timing = "reper activ azi"
            elif when < best_event.start:
                timing = f"cu {(best_event.start - when).days} zile înainte"
            else:
                timing = f"la {(when - best_event.end).days} zile după"
            best_reason = f"{best_event.name}: relevanță {best_kind}, {timing}."
        return best_score, best_kind, best_reason

    def day_context(self, when: date) -> dict:
        events = self.relevant_events(when)
        phase, season_tags = self.season_phase(when)
        concrete = [(event, p) for event, p in events if event.category != "sezon"]
        orthodox = [(event, p) for event, p in concrete if event.category in {"ortodox", "perioada_ortodoxa"}]
        civic = [(event, p) for event, p in concrete if event.category not in {"ortodox", "perioada_ortodoxa"}]
        primary = concrete[0] if concrete else (events[0] if events else None)
        weekday = when.weekday()
        rhythm = "weekend" if weekday >= 5 else "zi lucrătoare"
        explanation = phase
        if primary is not None:
            explanation = f"{primary[0].name} • {phase}"
        return {
            "date": when,
            "phase": phase,
            "season_tags": dict(season_tags),
            "events": events,
            "primary_event": primary,
            "orthodox_events": orthodox,
            "civic_events": civic,
            "rhythm": rhythm,
            "context_strength": max((event.importance * p for event, p in events), default=0.0),
            "explanation": explanation,
            "context_version": self.CONTEXT_VERSION,
        }

    def why_now(self, movie: Movie, when: date) -> dict:
        score, kind, reason = self.calendar_relevance(movie, when)
        context = self.day_context(when)
        return {
            "score": score,
            "kind": kind,
            "reason": reason,
            "phase": context["phase"],
            "rhythm": context["rhythm"],
            "context_strength": context["context_strength"],
            "primary_event": context["primary_event"][0].name if context["primary_event"] else "",
            "version": self.CONTEXT_VERSION,
        }
