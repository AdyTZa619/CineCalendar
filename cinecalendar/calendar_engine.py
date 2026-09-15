from __future__ import annotations
from datetime import date, timedelta, datetime, timezone
from math import floor
from .models import CalendarEvent, Movie
from .semantic import extract_semantic
from .util import clamp


def _julian_to_gregorian(year: int, month: int, day: int) -> date:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = day + (153*m + 2)//5 + 365*y + y//4 - 32083
    a2 = jdn + 32044
    b = (4*a2 + 3)//146097
    c = a2 - (146097*b)//4
    d = (4*c + 3)//1461
    e = c - (1461*d)//4
    m2 = (5*e + 2)//153
    gd = e - (153*m2 + 2)//5 + 1
    gm = m2 + 3 - 12*(m2//10)
    gy = 100*b + d - 4800 + m2//10
    return date(gy, gm, gd)


def orthodox_easter(year: int) -> date:
    """Gregorian date of Orthodox Easter using the Julian Paschalion, general calendar conversion."""
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19*c + 15) % 30
    e = (2*a + 4*b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    return _julian_to_gregorian(year, month, day)


def _jde_to_date(jd: float) -> date:
    # Meeus-style Julian day -> Gregorian calendar date; date precision is enough for phase grouping.
    z = int(jd + 0.5); f = jd + 0.5 - z
    if z < 2299161:
        a = z
    else:
        alpha = int((z - 1867216.25)/36524.25)
        a = z + 1 + alpha - alpha//4
    b = a + 1524
    c = int((b - 122.1)/365.25)
    d = int(365.25*c)
    e = int((b-d)/30.6001)
    day = b-d-int(30.6001*e)+f
    month = e-1 if e < 14 else e-13
    year = c-4716 if month > 2 else c-4715
    return date(year, month, int(day))


def seasonal_turning_dates(year: int) -> dict[str,date]:
    # Polynomial approximations from Astronomical Algorithms, adequate for calendar-day grouping 2000-3000.
    t = (year - 2000)/1000.0
    vals = {
        "spring_equinox": 2451623.80984 + 365242.37404*t + 0.05169*t*t - 0.00411*t**3 - 0.00057*t**4,
        "summer_solstice": 2451716.56767 + 365241.62603*t + 0.00325*t*t + 0.00888*t**3 - 0.00030*t**4,
        "autumn_equinox": 2451810.21715 + 365242.01767*t - 0.11575*t*t + 0.00337*t**3 + 0.00078*t**4,
        "winter_solstice": 2451900.05952 + 365242.74049*t - 0.06223*t*t - 0.00823*t**3 + 0.00032*t**4,
    }
    return {k:_jde_to_date(v) for k,v in vals.items()}


class CalendarEngine:
    def events_for_year(self, year: int) -> list[CalendarEvent]:
        E: list[CalendarEvent] = []
        def add(key,name,dt,category,importance,themes,direct=(),historical=(),spiritual=(),atmosphere=(),before=0,after=0,end=None):
            E.append(CalendarEvent(key,name,dt,end or dt,category,importance,themes,set(direct),set(historical),set(spiritual),set(atmosphere),before,after))

        # Romanian Orthodox fixed feasts (Gregorian civil dates).
        add("nativity","Nașterea Domnului",date(year,12,25),"ortodox",1.0,{"christmas":1,"christianity":.9,"family":.45},
            direct=("christmas",), spiritual=("christianity","faith"), atmosphere=("winter","family","hopeful"), before=5, after=6)
        add("epiphany","Botezul Domnului (Boboteaza)",date(year,1,6),"ortodox",.88,{"christianity":1,"faith":.8},
            direct=("christianity",), spiritual=("faith",), atmosphere=("winter",), before=1, after=1)
        add("meeting","Întâmpinarea Domnului",date(year,2,2),"ortodox",.72,{"christianity":.9,"family":.35},
            direct=("christianity",), spiritual=("faith",), before=1, after=1)
        add("annunciation","Buna Vestire",date(year,3,25),"ortodox",.84,{"christianity":.9,"faith":.7},
            direct=("christianity",), spiritual=("faith",), before=1, after=1)
        add("transfiguration","Schimbarea la Față",date(year,8,6),"ortodox",.80,{"christianity":.9,"faith":.8},
            direct=("christianity",), spiritual=("faith",), before=1, after=1)
        add("dormition","Adormirea Maicii Domnului",date(year,8,15),"ortodox",.88,{"christianity":.9,"faith":.8,"death":.3},
            direct=("christianity",), spiritual=("faith",), before=1, after=1)
        add("nativity_theotokos","Nașterea Maicii Domnului",date(year,9,8),"ortodox",.78,{"christianity":.85,"family":.3},
            direct=("christianity",), spiritual=("faith",), before=1, after=1)
        add("exaltation_cross","Înălțarea Sfintei Cruci",date(year,9,14),"ortodox",.92,{"cross_veneration":1,"christianity":.65,"faith":.65},
            direct=("cross_veneration",), historical=("medieval","history"), spiritual=("christianity","faith"), before=1, after=1)
        add("st_andrew","Sf. Apostol Andrei",date(year,11,30),"ortodox",.72,{"saints":.9,"christianity":.7,"romania":.6},
            direct=("saints",), historical=("romania",), spiritual=("christianity","faith"), before=1, after=1)
        add("st_nicholas","Sf. Nicolae",date(year,12,6),"ortodox",.68,{"saints":.85,"christianity":.55,"family":.35},
            direct=("saints",), spiritual=("christianity","faith"), atmosphere=("winter","family"), before=1, after=1)

        easter = orthodox_easter(year)
        add("palm_sunday","Floriile",easter-timedelta(days=7),"ortodox",.88,{"christianity":.9,"passion_of_christ":.45},
            direct=("christianity",), spiritual=("faith",), before=1, after=0)
        add("holy_week","Săptămâna Patimilor",easter-timedelta(days=6),"ortodox",1.0,{"passion_of_christ":1,"christianity":.85,"faith":.75},
            direct=("passion_of_christ",), spiritual=("christianity","faith"), atmosphere=("contemplative","dark"), end=easter-timedelta(days=1))
        add("easter","Sfintele Paști",easter,"ortodox",1.0,{"easter":1,"christianity":1,"faith":.9,"hopeful":.7},
            direct=("easter",), spiritual=("christianity","faith"), atmosphere=("hopeful","spring"), before=1, after=7)
        add("ascension","Înălțarea Domnului",easter+timedelta(days=39),"ortodox",.90,{"christianity":.9,"faith":.8},
            direct=("christianity",), spiritual=("faith",), before=1, after=1)
        add("pentecost","Rusaliile",easter+timedelta(days=49),"ortodox",.90,{"christianity":.9,"faith":.8},
            direct=("christianity",), spiritual=("faith",), before=1, after=1)
        add("great_lent","Postul Mare",easter-timedelta(days=48),"perioada_ortodoxa",.62,{"christianity":.6,"faith":.75,"contemplative":.75},
            spiritual=("christianity","faith","monasticism"), atmosphere=("contemplative",), end=easter-timedelta(days=8))

        # Curated secular/historical dates with cinematic relevance.
        add("holocaust_day","Ziua Internațională de Comemorare a Victimelor Holocaustului",date(year,1,27),"secular",.90,{"holocaust":1,"history":.6},
            direct=("holocaust",), historical=("war","history"), before=1, after=1)
        add("earth_day","Ziua Pământului",date(year,4,22),"secular",.72,{"nature":1},direct=("nature",), before=1, after=1)
        add("europe_day","Ziua Europei",date(year,5,9),"secular",.60,{"history":.45,"peace":.45,"politics":.35},historical=("history","war"), spiritual=("peace",), before=0, after=1)
        add("children_day","Ziua Copilului",date(year,6,1),"secular",.58,{"family":.8},direct=("family",), atmosphere=("hopeful",), before=0, after=1)
        add("peace_day","Ziua Internațională a Păcii",date(year,9,21),"secular",.88,{"peace":1,"war":.65,"reconciliation":.85},
            direct=("peace","reconciliation"), historical=("war",), before=1, after=1)
        add("tourism_day","Ziua Mondială a Turismului",date(year,9,27),"secular",.48,{"nature":.55,"adventure":.45},direct=("nature","adventure"), before=0, after=1)
        add("halloween","Halloween",date(year,10,31),"secular",.82,{"halloween":1,"horror":.8,"folk_horror":.55},direct=("halloween","horror","folk_horror"), atmosphere=("dark",), before=3, after=0)
        add("armistice","Armistițiul din 1918",date(year,11,11),"istoric",.72,{"war":.8,"peace":.65,"history":.7}, historical=("war","history"), spiritual=("peace",), before=1, after=1)
        add("romania_national","Ziua Națională a României",date(year,12,1),"secular",.95,{"romania":1,"history":.8,"war":.35},direct=("romania",),historical=("history","war"), before=2, after=1)
        add("romanian_revolution","Comemorarea Revoluției Române din 1989",date(year,12,16),"istoric",.85,{"romania":.9,"communism":.8,"revolution":1,"history":.8},
            direct=("revolution","communism"),historical=("romania","history"), end=date(year,12,22), before=0, after=0)

        turns = seasonal_turning_dates(year)
        add("spring_equinox","Echinocțiul de primăvară",turns["spring_equinox"],"sezon",.62,{"spring":1,"nature":.4,"hopeful":.35}, atmosphere=("spring","nature","hopeful"), before=2, after=3)
        add("summer_solstice","Solstițiul de vară",turns["summer_solstice"],"sezon",.58,{"summer":1,"nature":.45,"adventure":.3}, atmosphere=("summer","nature","adventure"), before=2, after=3)
        add("autumn_equinox","Echinocțiul de toamnă",turns["autumn_equinox"],"sezon",.70,{"autumn":1,"nature":.45,"melancholic":.35,"contemplative":.25}, atmosphere=("autumn","nature","melancholic","contemplative"), before=2, after=4)
        add("winter_solstice","Solstițiul de iarnă",turns["winter_solstice"],"sezon",.62,{"winter":1,"nature":.35,"dark":.25}, atmosphere=("winter","nature","dark"), before=2, after=3)
        return sorted(E, key=lambda x:(x.start,x.end,x.name))

    def relevant_events(self, when: date) -> list[tuple[CalendarEvent,float]]:
        events = self.events_for_year(when.year)
        # Include Jan dates influenced by late Dec from previous year and vice versa.
        if when.month == 1: events += self.events_for_year(when.year-1)
        if when.month == 12: events += self.events_for_year(when.year+1)
        out=[]
        for ev in events:
            start = ev.start - timedelta(days=ev.influence_before)
            end = ev.end + timedelta(days=ev.influence_after)
            if start <= when <= end:
                if ev.start <= when <= ev.end: proximity = 1.0
                else:
                    dist = (ev.start-when).days if when < ev.start else (when-ev.end).days
                    window = ev.influence_before if when < ev.start else ev.influence_after
                    proximity = max(.35, 1.0 - dist/max(1,window+1))
                out.append((ev, proximity))
        out.sort(key=lambda x:x[0].importance*x[1], reverse=True)
        return out

    def season_phase(self, when: date) -> tuple[str,dict[str,float]]:
        y=when.year; turns=seasonal_turning_dates(y)
        spring,summer,autumn,winter = turns["spring_equinox"],turns["summer_solstice"],turns["autumn_equinox"],turns["winter_solstice"]
        # Special cinematic micro-periods first.
        easter=orthodox_easter(y)
        if easter-timedelta(days=6) <= when <= easter+timedelta(days=7): return "perioada pascală", {"easter":1,"spring":.45,"hopeful":.35,"contemplative":.25}
        if date(y,12,20) <= when <= date(y,12,31): return "perioada Crăciun–Anul Nou", {"winter":1,"christmas":.9,"family":.55,"hopeful":.45}
        if when.month==1 and when.day<=7: return "perioada Crăciun–Anul Nou", {"winter":1,"christmas":.7,"family":.45}
        if spring <= when < summer:
            days=(when-spring).days; phase="început de primăvară" if days<30 else ("mijloc de primăvară" if days<65 else "primăvară târzie")
            return phase,{"spring":1,"nature":.45,"hopeful":.25}
        if summer <= when < autumn:
            days=(when-summer).days; phase="început de vară" if days<30 else ("mijlocul verii" if days<65 else "vară târzie")
            return phase,{"summer":1,"nature":.4,"adventure":.25}
        if autumn <= when < winter:
            days=(when-autumn).days; phase="început de toamnă" if days<30 else ("mijloc de toamnă" if days<65 else "toamnă târzie")
            return phase,{"autumn":1,"melancholic":.38,"contemplative":.28,"nature":.25}
        # Winter spans year boundary.
        days = (when - winter).days if when >= winter else (when - seasonal_turning_dates(y-1)["winter_solstice"]).days
        phase="început de iarnă" if days<30 else ("mijloc de iarnă" if days<65 else "iarnă târzie")
        return phase,{"winter":1,"dark":.18,"contemplative":.20}

    def calendar_relevance(self, movie: Movie, when: date) -> tuple[float,str,str]:
        sem = movie.semantic or extract_semantic(movie)
        best_score=0.0; best_kind="slabă"; best_reason="Fără reper calendaristic puternic."
        for ev, proximity in self.relevant_events(when):
            direct=max((sem.get(t,0) for t in ev.direct_tags), default=0)
            historical=max((sem.get(t,0) for t in ev.historical_tags), default=0)
            spiritual=max((sem.get(t,0) for t in ev.spiritual_tags), default=0)
            atmosphere=max((sem.get(t,0) for t in ev.atmosphere_tags), default=0)
            # Direct relevance dominates, then historical/spiritual, then atmosphere. General Christianity is not enough
            # to make a Passion film 'direct' for the Exaltation of the Cross.
            kinds=[("directă",direct,1.0),("istorică",historical,.72),("spirituală",spiritual,.58),("atmosferică",atmosphere,.46)]
            kind,val,mult=max(kinds,key=lambda x:x[1]*x[2])
            score=clamp(val*mult*ev.importance*proximity)
            if score>best_score:
                best_score=score; best_kind=kind if score>=.12 else "slabă"
                best_reason=f"{ev.name}: relevanță {best_kind}."
        return best_score,best_kind,best_reason

    def month_groups(self, start: date) -> list[tuple[date,date,str]]:
        from calendar import monthrange
        last=date(start.year,start.month,monthrange(start.year,start.month)[1])
        events=[e for e in self.events_for_year(start.year) if start <= e.start <= last and e.importance>=.45]
        events.sort(key=lambda e:(e.start,-e.importance))
        # Collapse multiple events on the same date into one anchor; the strongest one supplies the label.
        anchors=[]
        seen=set()
        for ev in events:
            if ev.start in seen: continue
            same=[x for x in events if x.start==ev.start]
            strongest=max(same,key=lambda x:x.importance); anchors.append(strongest); seen.add(ev.start)
        if not anchors:
            return [(start,last,self.season_phase(start+(last-start)//2)[0])]
        groups=[]; current=start
        for i,ev in enumerate(anchors):
            if ev.start < current: continue
            next_start=anchors[i+1].start if i+1<len(anchors) else last+timedelta(days=1)
            # If the month/program starts immediately next to a feast, keep a tight first window around it.
            # This prevents a 14 Sept feast from swallowing the whole week before the 21 Sept anchor.
            if i==0 and (ev.start-start).days <= max(2,ev.influence_before+1):
                end=min(last, ev.end+timedelta(days=max(1,ev.influence_after)), next_start-timedelta(days=1))
            else:
                end=min(last,next_start-timedelta(days=1))
            # Very long gaps are split into weekly atmosphere blocks before the anchor.
            while (end-current).days > 9 and current < ev.start-timedelta(days=2):
                seg_end=min(end,current+timedelta(days=6))
                groups.append((current,seg_end,self.season_phase(current+(seg_end-current)//2)[0]))
                current=seg_end+timedelta(days=1)
            if end>=current:
                labels=[x.name for x in events if current<=x.start<=end]
                label=", ".join(labels[:2]) if labels else self.season_phase(current+(end-current)//2)[0]
                groups.append((current,end,label)); current=end+timedelta(days=1)
        if current<=last:
            groups.append((current,last,self.season_phase(current+(last-current)//2)[0]))
        return groups

