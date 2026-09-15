from __future__ import annotations

from datetime import date, timedelta

from .calendar_engine import CalendarEngine, orthodox_easter
from .models import CalendarEvent


class RichCalendarEngine(CalendarEngine):
    """Richer Romanian calendar used by the calendar-first experience.

    The original engine intentionally had only a small set of anchors.  That is fine for a
    weak calendar tie-breaker, but it is not enough for a real day-by-day movie program.
    This layer keeps the proven Easter/season calculations and adds major Romanian Orthodox
    observances, fasting periods, Romanian civic/history dates and a few internationally
    relevant commemorations with clear cinematic themes.
    """

    def events_for_year(self, year: int) -> list[CalendarEvent]:
        events = list(super().events_for_year(year))
        existing = {e.key for e in events}

        def add(
            key: str,
            name: str,
            start: date,
            category: str,
            importance: float,
            themes: dict[str, float],
            *,
            direct=(),
            historical=(),
            spiritual=(),
            atmosphere=(),
            before: int = 0,
            after: int = 0,
            end: date | None = None,
        ) -> None:
            if key in existing:
                return
            existing.add(key)
            events.append(
                CalendarEvent(
                    key=key,
                    name=name,
                    start=start,
                    end=end or start,
                    category=category,
                    importance=importance,
                    themes=dict(themes),
                    direct_tags=set(direct),
                    historical_tags=set(historical),
                    spiritual_tags=set(spiritual),
                    atmosphere_tags=set(atmosphere),
                    influence_before=before,
                    influence_after=after,
                )
            )

        # --- Major Romanian Orthodox observances not present in the compact v1 calendar ---
        add(
            "new_year_basil", "Tăierea împrejur a Domnului • Sf. Vasile cel Mare",
            date(year, 1, 1), "ortodox", .76,
            {"christianity": .85, "saints": .65, "new_year": .55},
            direct=("christianity", "saints"), spiritual=("faith",),
            atmosphere=("winter", "family", "hopeful"), after=1,
        )
        add(
            "st_john_baptist", "Soborul Sf. Ioan Botezătorul",
            date(year, 1, 7), "ortodox", .72,
            {"saints": .9, "christianity": .7},
            direct=("saints",), spiritual=("christianity", "faith"), after=1,
        )
        add(
            "three_hierarchs", "Sfinții Trei Ierarhi",
            date(year, 1, 30), "ortodox", .62,
            {"saints": .9, "christianity": .65, "history": .25},
            direct=("saints",), spiritual=("christianity", "faith"),
        )
        add(
            "st_george", "Sf. Mare Mucenic Gheorghe",
            date(year, 4, 23), "ortodox", .64,
            {"saints": .9, "martyrdom": .7, "christianity": .6},
            direct=("saints",), historical=("history",), spiritual=("christianity", "faith"),
        )
        add(
            "constantine_helena", "Sfinții Împărați Constantin și Elena",
            date(year, 5, 21), "ortodox", .66,
            {"saints": .8, "christianity": .65, "history": .6, "antiquity": .45},
            direct=("saints",), historical=("antiquity", "history"), spiritual=("christianity", "faith"),
        )
        add(
            "nativity_john", "Nașterea Sf. Ioan Botezătorul",
            date(year, 6, 24), "ortodox", .68,
            {"saints": .8, "christianity": .7},
            direct=("saints",), spiritual=("christianity", "faith"),
        )
        add(
            "peter_paul", "Sfinții Apostoli Petru și Pavel",
            date(year, 6, 29), "ortodox", .76,
            {"saints": .85, "christianity": .8, "history": .3},
            direct=("saints",), historical=("antiquity",), spiritual=("christianity", "faith"), after=1,
        )
        add(
            "st_elijah", "Sf. Proroc Ilie",
            date(year, 7, 20), "ortodox", .60,
            {"saints": .75, "christianity": .55, "nature": .35},
            direct=("saints",), spiritual=("christianity", "faith"), atmosphere=("summer", "nature"),
        )
        add(
            "dormition_fast", "Postul Adormirii Maicii Domnului",
            date(year, 8, 1), "perioada_ortodoxa", .58,
            {"christianity": .55, "faith": .72, "contemplative": .7},
            spiritual=("christianity", "faith", "monasticism"), atmosphere=("contemplative",),
            end=date(year, 8, 14),
        )
        add(
            "beheading_john", "Tăierea capului Sf. Ioan Botezătorul",
            date(year, 8, 29), "ortodox", .72,
            {"saints": .8, "martyrdom": .75, "death": .55, "christianity": .6},
            direct=("saints",), historical=("antiquity",), spiritual=("christianity", "faith"),
            atmosphere=("dark", "contemplative"),
        )
        add(
            "protection_theotokos", "Acoperământul Maicii Domnului",
            date(year, 10, 1), "ortodox", .64,
            {"christianity": .7, "faith": .75, "family": .25},
            direct=("christianity",), spiritual=("faith",), atmosphere=("autumn", "contemplative"),
        )
        add(
            "st_demetrius", "Sf. Mare Mucenic Dimitrie",
            date(year, 10, 26), "ortodox", .62,
            {"saints": .85, "christianity": .6},
            direct=("saints",), spiritual=("christianity", "faith"),
        )
        add(
            "st_dimitrie_bas", "Sf. Cuvios Dimitrie cel Nou, Ocrotitorul Bucureștilor",
            date(year, 10, 27), "ortodox", .58,
            {"saints": .85, "christianity": .55, "romania": .45},
            direct=("saints",), historical=("romania",), spiritual=("christianity", "faith"),
        )
        add(
            "archangels", "Sfinții Arhangheli Mihail și Gavriil",
            date(year, 11, 8), "ortodox", .70,
            {"christianity": .72, "faith": .8},
            direct=("christianity",), spiritual=("faith",), atmosphere=("autumn", "contemplative"),
        )
        add(
            "nativity_fast", "Postul Nașterii Domnului",
            date(year, 11, 15), "perioada_ortodoxa", .58,
            {"christianity": .58, "faith": .68, "contemplative": .62, "christmas": .35},
            spiritual=("christianity", "faith", "monasticism"), atmosphere=("winter", "contemplative"),
            end=date(year, 12, 24),
        )
        add(
            "synaxis_theotokos", "Soborul Maicii Domnului",
            date(year, 12, 26), "ortodox", .72,
            {"christianity": .8, "family": .5, "christmas": .65},
            direct=("christianity", "christmas"), spiritual=("faith",), atmosphere=("winter", "family"),
        )
        add(
            "st_stephen", "Sf. Arhidiacon Ștefan",
            date(year, 12, 27), "ortodox", .66,
            {"saints": .8, "christianity": .65, "martyrdom": .45},
            direct=("saints",), historical=("antiquity",), spiritual=("christianity", "faith"),
        )

        easter = orthodox_easter(year)
        add(
            "lazarus_saturday", "Sâmbăta lui Lazăr", easter - timedelta(days=8),
            "ortodox", .82, {"christianity": .85, "faith": .75, "death": .35, "hopeful": .45},
            direct=("christianity",), spiritual=("faith",), atmosphere=("contemplative", "hopeful"),
        )
        add(
            "holy_thursday", "Joia Mare", easter - timedelta(days=3),
            "ortodox", .92, {"passion_of_christ": .9, "christianity": .9, "faith": .75},
            direct=("passion_of_christ",), spiritual=("christianity", "faith"), atmosphere=("contemplative", "dark"),
        )
        add(
            "good_friday", "Vinerea Mare", easter - timedelta(days=2),
            "ortodox", 1.0, {"passion_of_christ": 1.0, "christianity": .95, "death": .65},
            direct=("passion_of_christ", "cross_veneration"), spiritual=("christianity", "faith"),
            atmosphere=("dark", "contemplative"),
        )
        add(
            "holy_saturday", "Sâmbăta Mare", easter - timedelta(days=1),
            "ortodox", .95, {"passion_of_christ": .9, "christianity": .9, "contemplative": .7},
            direct=("passion_of_christ",), spiritual=("christianity", "faith"), atmosphere=("contemplative", "dark"),
        )
        add(
            "bright_week", "Săptămâna Luminată", easter + timedelta(days=1),
            "perioada_ortodoxa", .76, {"easter": .9, "christianity": .8, "hopeful": .8},
            direct=("easter",), spiritual=("christianity", "faith"), atmosphere=("hopeful", "spring"),
            end=easter + timedelta(days=6),
        )
        all_saints = easter + timedelta(days=56)
        add(
            "all_saints", "Duminica Tuturor Sfinților", all_saints,
            "ortodox", .66, {"saints": .8, "christianity": .7, "faith": .7},
            direct=("saints",), spiritual=("christianity", "faith"),
        )
        apostles_fast_start = all_saints + timedelta(days=1)
        apostles_fast_end = date(year, 6, 28)
        if apostles_fast_start <= apostles_fast_end:
            add(
                "apostles_fast", "Postul Sfinților Apostoli Petru și Pavel",
                apostles_fast_start, "perioada_ortodoxa", .52,
                {"christianity": .5, "faith": .65, "contemplative": .55},
                spiritual=("christianity", "faith", "monasticism"), atmosphere=("contemplative",),
                end=apostles_fast_end,
            )

        # --- Romanian civic, cultural and historical anchors ---
        add(
            "romanian_culture", "Ziua Culturii Naționale • Mihai Eminescu",
            date(year, 1, 15), "cultural", .72,
            {"romania": .9, "history": .35, "biography": .45},
            direct=("romania",), historical=("history",), atmosphere=("winter", "contemplative"), before=1, after=1,
        )
        add(
            "union_principalities", "Unirea Principatelor Române",
            date(year, 1, 24), "istoric", .90,
            {"romania": 1.0, "history": .9, "politics": .55},
            direct=("romania",), historical=("history", "politics"), before=2, after=1,
        )
        add(
            "brancusi_day", "Ziua Națională Constantin Brâncuși",
            date(year, 2, 19), "cultural", .56,
            {"romania": .7, "biography": .55, "history": .25},
            direct=("romania", "biography"), historical=("history",),
        )
        add(
            "martisor", "Mărțișor",
            date(year, 3, 1), "traditional", .58,
            {"romania": .75, "spring": .8, "folk": .55, "family": .25},
            direct=("romania",), atmosphere=("spring", "hopeful", "nature"), before=1, after=2,
        )
        add(
            "womens_day", "Ziua Internațională a Femeii",
            date(year, 3, 8), "secular", .48,
            {"family": .45, "history": .25}, atmosphere=("spring", "family"),
        )
        add(
            "labour_day", "Ziua Muncii",
            date(year, 5, 1), "secular", .58,
            {"history": .45, "politics": .35}, historical=("history", "politics"), atmosphere=("spring",),
        )
        add(
            "victory_europe", "Sfârșitul celui de-Al Doilea Război Mondial în Europa",
            date(year, 5, 8), "istoric", .82,
            {"war": .95, "history": .9, "peace": .65},
            direct=("war",), historical=("history",), spiritual=("peace",), before=1, after=1,
        )
        add(
            "romanian_independence", "Independența României • 10 Mai",
            date(year, 5, 10), "istoric", .78,
            {"romania": .95, "history": .85, "war": .45, "politics": .4},
            direct=("romania",), historical=("history", "war", "politics"), before=1, after=1,
        )
        add(
            "environment_day", "Ziua Mondială a Mediului",
            date(year, 6, 5), "secular", .52,
            {"nature": .95}, direct=("nature",), atmosphere=("summer", "nature"),
        )
        add(
            "sanziene", "Sânziene / Drăgaica",
            date(year, 6, 24), "traditional", .56,
            {"romania": .65, "folk_horror": .35, "nature": .65, "summer": .7},
            direct=("romania",), atmosphere=("summer", "nature"),
        )
        add(
            "flag_day_ro", "Ziua Drapelului Național",
            date(year, 6, 26), "secular", .48,
            {"romania": .8, "history": .45}, direct=("romania",), historical=("history",),
        )
        add(
            "anthem_day_ro", "Ziua Imnului Național",
            date(year, 7, 29), "secular", .46,
            {"romania": .75, "history": .4}, direct=("romania",), historical=("history",),
        )
        add(
            "august_23_1944", "23 August 1944 • România întoarce armele",
            date(year, 8, 23), "istoric", .82,
            {"romania": .95, "war": .9, "history": .9, "politics": .45},
            direct=("romania", "war"), historical=("history", "politics"), before=1, after=1,
        )
        add(
            "ww2_start", "Începutul celui de-Al Doilea Război Mondial",
            date(year, 9, 1), "istoric", .82,
            {"war": 1.0, "history": .9}, direct=("war",), historical=("history",), before=1, after=1,
        )
        add(
            "september_11", "Atentatele din 11 septembrie 2001",
            date(year, 9, 11), "istoric", .72,
            {"history": .75, "politics": .6, "death": .55},
            historical=("history", "politics"), atmosphere=("dark", "contemplative"),
        )
        add(
            "army_day_ro", "Ziua Armatei României",
            date(year, 10, 25), "secular", .74,
            {"romania": .9, "war": .7, "history": .65},
            direct=("romania", "war"), historical=("history",), before=1, after=1,
        )
        add(
            "berlin_wall", "Căderea Zidului Berlinului",
            date(year, 11, 9), "istoric", .70,
            {"communism": .9, "history": .8, "politics": .55, "peace": .35},
            direct=("communism",), historical=("history", "politics"), spiritual=("peace",),
        )
        add(
            "human_rights", "Ziua Internațională a Drepturilor Omului",
            date(year, 12, 10), "secular", .58,
            {"politics": .5, "history": .35, "peace": .55},
            historical=("history", "politics"), spiritual=("peace",),
        )
        add(
            "new_year_eve", "Ajunul Anului Nou",
            date(year, 12, 31), "sezon", .66,
            {"winter": .9, "family": .5, "hopeful": .7}, atmosphere=("winter", "family", "hopeful"),
        )

        return sorted(events, key=lambda e: (e.start, e.end, -e.importance, e.name))

    def day_context(self, when: date) -> dict:
        events = self.relevant_events(when)
        phase, season_tags = self.season_phase(when)
        categories: dict[str, list[CalendarEvent]] = {}
        for event, _proximity in events:
            categories.setdefault(event.category, []).append(event)
        return {
            "date": when,
            "phase": phase,
            "season_tags": dict(season_tags),
            "events": events,
            "categories": categories,
        }
