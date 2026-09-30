from __future__ import annotations

import sys
from calendar import monthrange
from datetime import date, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout,
)

from .premium_ui import PremiumDecisionWindow
from .qt_ui import WorkerThread
from .qt_ui_v2 import DecisionWindow
from .recommendation import Recommendation


RO_MONTHS = (
    "", "Ianuarie", "Februarie", "Martie", "Aprilie", "Mai", "Iunie",
    "Iulie", "August", "Septembrie", "Octombrie", "Noiembrie", "Decembrie",
)
RO_WEEKDAYS = ("LUN", "MAR", "MIE", "JOI", "VIN", "SÂM", "DUM")


class CalendarPremiumWindow(PremiumDecisionWindow):
    """Premium UI with a real day-by-day calendar program.

    Calendar and the old separate "Program calendar" destination are intentionally merged:
    one navigation entry owns both the immersive month and the annual reference view.
    """

    NAV = [(key, label) for key, label in PremiumDecisionWindow.NAV if key != "month"]

    """Premium UI with a real day-by-day calendar program.

    The page is intentionally instant: month context is rendered immediately and only the
    selected day's movie program is calculated in a worker. Reopening a day uses the engine
    cache instead of recalculating multiple month intervals.
    """

    def __init__(self, service):
        self.calendar_worker: WorkerThread | None = None
        self.calendar_focus_layout: QVBoxLayout | None = None
        self.calendar_spotlight_layout: QVBoxLayout | None = None
        self.calendar_day_buttons: dict[date, QPushButton] = {}
        self.calendar_selected: date = date.today()
        self.calendar_month_anchor: date = date.today().replace(day=1)
        self.calendar_pending: date | None = None
        self.calendar_last_result: dict | None = None
        self.calendar_last_signature = None
        self.calendar_view_mode = "month"
        super().__init__(service)

    # ---------- calendar navigation ----------
    def _shift_month(self, delta: int):
        y = self.calendar_month_anchor.year
        m = self.calendar_month_anchor.month + int(delta)
        while m < 1:
            m += 12; y -= 1
        while m > 12:
            m -= 12; y += 1
        self.calendar_month_anchor = date(y, m, 1)
        self.calendar_selected = self.calendar_month_anchor
        self.calendar_view_mode = "month"
        self.show_page("calendar")

    def _go_today(self):
        self.calendar_month_anchor = date.today().replace(day=1)
        self.calendar_selected = date.today()
        self.calendar_view_mode = "month"
        self.show_page("calendar")

    def _open_calendar_date(self, target: date):
        self.calendar_month_anchor = target.replace(day=1)
        self.calendar_selected = target
        self.calendar_view_mode = "month"
        self.show_page("calendar")

    def _set_calendar_view(self, mode: str):
        self.calendar_view_mode = "year" if str(mode) == "year" else "month"
        self.show_page("calendar")

    # ---------- complete calendar reference ----------
    def _page_calendar_year(self):
        today = date.today()
        year = self.calendar_month_anchor.year if self.calendar_month_anchor else today.year
        page, content = self.page_shell(
            f"Calendar — Repere {year}",
            "Vedere anuală a reperelor. Alege un reper și revii direct în luna lui, unde apar numai filmele cu legătură susținută de acel context.",
            [
                ("‹ Anul anterior", lambda: self._change_calendar_year(-1), False),
                ("Lună", lambda: self._set_calendar_view("month"), True),
                ("Anul următor ›", lambda: self._change_calendar_year(1), False),
                ("Azi", self._go_today, False),
            ],
        )

        events = self.s.calendar.events_for_year(year)
        groups: dict[int, list] = {m: [] for m in range(1, 13)}
        for ev in events:
            groups[ev.start.month].append(ev)

        intro = QFrame(); intro.setObjectName("HeroCard")
        il = QVBoxLayout(intro); il.setContentsMargins(22,20,22,20); il.setSpacing(8)
        h = QLabel(f"{len(events)} repere majore indexate pentru {year}")
        h.setObjectName("SectionTitle"); il.addWidget(h)
        x = QLabel("CalendarEngine furnizează perioade active și ferestre de influență. Alegerea unui reper deschide aceeași pagină Calendar în luna lui; nu mai există un al doilea tab cu aceeași funcție.")
        x.setObjectName("Muted"); x.setWordWrap(True); il.addWidget(x)
        content.addWidget(intro)

        for month in range(1, 13):
            month_events = groups.get(month) or []
            if not month_events:
                continue
            mh = QLabel(RO_MONTHS[month]); mh.setObjectName("SectionTitle"); content.addWidget(mh)
            grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(10)
            for i, ev in enumerate(month_events):
                card = QFrame(); card.setObjectName("PremiumCard"); card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
                l = QVBoxLayout(card); l.setContentsMargins(16,14,16,14); l.setSpacing(6)
                if ev.start == ev.end:
                    dtext = ev.start.strftime("%d.%m")
                else:
                    dtext = f"{ev.start:%d.%m}–{ev.end:%d.%m}"
                top = QHBoxLayout()
                title = QLabel(f"{dtext}  •  {ev.name}"); title.setObjectName("CardTitle"); title.setWordWrap(True); top.addWidget(title, 1)
                cat = self.pill(ev.category.replace("_", " ").title()); top.addWidget(cat)
                l.addLayout(top)
                links = []
                if ev.direct_tags: links.append("direct: " + ", ".join(sorted(ev.direct_tags)))
                if ev.spiritual_tags: links.append("spiritual: " + ", ".join(sorted(ev.spiritual_tags)))
                if ev.historical_tags: links.append("istoric: " + ", ".join(sorted(ev.historical_tags)))
                if ev.atmosphere_tags: links.append("atmosferă: " + ", ".join(sorted(ev.atmosphere_tags)))
                meta = QLabel(" • ".join(links[:3]) or "Context calendaristic general")
                meta.setObjectName("Muted"); meta.setWordWrap(True); l.addWidget(meta)
                b = QPushButton("Filme pentru reperul ăsta")
                b.clicked.connect(lambda _, d=ev.start: self._open_calendar_date(d))
                l.addWidget(b, alignment=Qt.AlignLeft)
                grid.addWidget(card, i // 2, i % 2)
            wrap = QFrame(); wrap.setLayout(grid); content.addWidget(wrap)

        content.addStretch(1)
        return page

    def _change_calendar_year(self, delta: int):
        self.calendar_month_anchor = date(self.calendar_month_anchor.year + int(delta), self.calendar_month_anchor.month, 1)
        self.calendar_view_mode = "year"
        self.show_page("calendar")

    def page_calendar(self):
        if self.calendar_view_mode == "year":
            return self._page_calendar_year()
        return self.page_month()

    # ---------- fast month / selected day program ----------
    def page_month(self):
        anchor = self.calendar_month_anchor
        if self.calendar_selected.year != anchor.year or self.calendar_selected.month != anchor.month:
            self.calendar_selected = anchor

        page, content = self.page_shell(
            f"Calendar — {RO_MONTHS[anchor.month]} {anchor.year}",
            "Alege o zi. Dacă există un reper real, vezi numai filmele legate de el; zilele obișnuite nu repetă recomandările generale din alt tab.",
            [
                ("‹ Luna anterioară", lambda: self._shift_month(-1), False),
                ("Azi", self._go_today, True),
                ("Luna următoare ›", lambda: self._shift_month(1), False),
                ("Repere anuale", lambda: self._set_calendar_view("year"), False),
            ],
        )

        month_events = [ev for ev in self.s.calendar.events_for_year(anchor.year) if ev.start.month == anchor.month]
        starts: dict[int, list] = {}
        for ev in month_events:
            starts.setdefault(ev.start.day, []).append(ev)
        for items in starts.values():
            items.sort(key=lambda ev: (float(ev.importance), ev.name), reverse=True)

        stage = QFrame(); stage.setObjectName("CalendarStage")
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(24,22,24,24); stage_layout.setSpacing(16)

        month_head = QHBoxLayout()
        month_copy = QVBoxLayout(); month_copy.setSpacing(2)
        kicker = QLabel("LUNA TA DE FILM"); kicker.setObjectName("Kicker"); month_copy.addWidget(kicker)
        month_title = QLabel(f"{RO_MONTHS[anchor.month]} {anchor.year}")
        month_title.setObjectName("HeroTitle"); month_copy.addWidget(month_title)
        month_head.addLayout(month_copy, 1)
        count_label = QLabel(f"{len(month_events)} repere indexate")
        count_label.setObjectName("Muted"); month_head.addWidget(count_label, 0, Qt.AlignBottom)
        stage_layout.addLayout(month_head)

        weekday_grid = QGridLayout(); weekday_grid.setHorizontalSpacing(10)
        for col, name in enumerate(RO_WEEKDAYS):
            label = QLabel(name); label.setObjectName("CalendarWeekday")
            label.setAlignment(Qt.AlignCenter)
            weekday_grid.addWidget(label, 0, col)
        stage_layout.addLayout(weekday_grid)

        days_grid = QGridLayout()
        days_grid.setHorizontalSpacing(10); days_grid.setVerticalSpacing(10)
        first_col = anchor.weekday()
        last_day = monthrange(anchor.year, anchor.month)[1]
        total_slots = first_col + last_day
        rows = (total_slots + 6) // 7
        for row in range(rows):
            days_grid.setRowStretch(row, 1)
        for col in range(7):
            days_grid.setColumnStretch(col, 1)

        self.calendar_day_buttons = {}
        today = date.today()
        for slot in range(first_col):
            blank = QFrame(); blank.setObjectName("CalendarDayBlank"); blank.setMinimumHeight(96)
            days_grid.addWidget(blank, 0, slot)

        for day_no in range(1, last_day + 1):
            target = date(anchor.year, anchor.month, day_no)
            events = starts.get(day_no, [])
            primary = events[0] if events else None
            button = QPushButton()
            button.setObjectName("CalendarDay")
            button.setProperty("hasEvent", bool(events))
            button.setProperty("major", any(float(ev.importance) >= .80 for ev in events))
            button.setProperty("selected", target == self.calendar_selected)
            button.setProperty("today", target == today)
            button.setProperty("weekend", target.weekday() >= 5)
            button.setMinimumHeight(96)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

            cell = QVBoxLayout(button)
            cell.setContentsMargins(12,10,12,10); cell.setSpacing(4)
            top_line = QHBoxLayout(); top_line.setSpacing(5)
            number = QLabel(str(day_no)); number.setObjectName("CalendarDayNumber")
            number.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            top_line.addWidget(number)
            if target == today:
                today_tag = QLabel("AZI"); today_tag.setObjectName("CalendarTodayTag")
                today_tag.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                top_line.addWidget(today_tag)
            top_line.addStretch(1)
            if events:
                dot = QLabel("●"); dot.setObjectName("CalendarEventDot")
                dot.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                top_line.addWidget(dot)
            cell.addLayout(top_line)
            cell.addStretch(1)

            if primary is not None:
                name = primary.name
                short = name if len(name) <= 31 else name[:30].rstrip() + "…"
                event_label = QLabel(short); event_label.setObjectName("CalendarDayEvent")
                event_label.setWordWrap(True)
                event_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                cell.addWidget(event_label)
                if len(events) > 1:
                    extra_count = len(events) - 1
                    extra = QLabel(f"+{extra_count} reper" + ("e" if extra_count != 1 else ""))
                    extra.setObjectName("CalendarDayCount")
                    extra.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                    cell.addWidget(extra)
            else:
                quiet = QLabel(" "); quiet.setObjectName("CalendarDayEvent")
                quiet.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                cell.addWidget(quiet)

            if events:
                button.setToolTip("\n".join(ev.name for ev in events))
            else:
                phase, _tags = self.s.calendar.season_phase(target)
                button.setToolTip(f"{target:%d.%m.%Y} • {phase}")
            button.clicked.connect(lambda _checked=False, d=target: self._select_calendar_day(d))
            self.calendar_day_buttons[target] = button
            absolute = first_col + day_no - 1
            row, col = divmod(absolute, 7)
            days_grid.addWidget(button, row, col)

        trailing = rows * 7 - total_slots
        for offset in range(trailing):
            absolute = total_slots + offset
            row, col = divmod(absolute, 7)
            blank = QFrame(); blank.setObjectName("CalendarDayBlank"); blank.setMinimumHeight(96)
            days_grid.addWidget(blank, row, col)

        stage_layout.addLayout(days_grid)
        content.addWidget(stage)

        spotlight = QFrame(); spotlight.setObjectName("CalendarSpotlight")
        self.calendar_spotlight_layout = QVBoxLayout(spotlight)
        self.calendar_spotlight_layout.setContentsMargins(26,24,26,24)
        self.calendar_spotlight_layout.setSpacing(14)
        content.addWidget(spotlight)

        program_head = QHBoxLayout()
        program_title = QLabel("FILMELE ZILEI")
        program_title.setObjectName("CalendarProgramTitle"); program_head.addWidget(program_title)
        program_head.addStretch(1)
        content.addLayout(program_head)
        program = QFrame(); program.setObjectName("CalendarProgram")
        self.calendar_focus_layout = QVBoxLayout(program)
        self.calendar_focus_layout.setContentsMargins(18,18,18,18)
        self.calendar_focus_layout.setSpacing(12)
        content.addWidget(program)
        content.addStretch(1)

        if self._calendar_cached(self.calendar_selected):
            self._render_calendar_program(self.calendar_last_result)
        else:
            self._render_calendar_loading(self.calendar_selected)
            QTimer.singleShot(0, lambda d=self.calendar_selected: self._load_calendar_day_async(d))
        return page

    def _calendar_cached(self, target: date) -> bool:
        result = self.calendar_last_result
        return bool(
            isinstance(result, dict) and result.get("date") == target
            and self.calendar_last_signature == (target, self._browse_state_signature())
        )

    def _refresh_calendar_day_button(self, target: date):
        button = self.calendar_day_buttons.get(target)
        if button is None:
            return
        button.setProperty("selected", target == self.calendar_selected)
        style = button.style()
        style.unpolish(button); style.polish(button); button.update()

    def _select_calendar_day(self, target: date):
        previous = self.calendar_selected
        self.calendar_selected = target
        self._refresh_calendar_day_button(previous)
        self._refresh_calendar_day_button(target)
        if self._calendar_cached(target):
            self._render_calendar_program(self.calendar_last_result)
        else:
            self._render_calendar_loading(target)
            self._load_calendar_day_async(target)

    def _render_calendar_spotlight(self, target: date, result: dict | None = None, *, loading: bool = False):
        layout = self.calendar_spotlight_layout
        if layout is None:
            return
        self._clear_layout(layout)

        events = list((result or {}).get("events") or self.s.calendar.relevant_events(target))
        exact = [(ev, p) for ev, p in events if ev.category != "sezon" and ev.start <= target <= ev.end]
        nearby = [(ev, p) for ev, p in events if ev.category != "sezon"]
        candidates = exact or nearby or events
        primary = max(candidates, key=lambda item: float(item[0].importance) * float(item[1]), default=None)
        phase = (result or {}).get("phase") or self.s.calendar.season_phase(target)[0]

        top = QHBoxLayout(); top.setSpacing(22)

        date_tile = QFrame(); date_tile.setObjectName("CalendarDateTile")
        date_box = QVBoxLayout(date_tile); date_box.setContentsMargins(18,14,18,14); date_box.setSpacing(0)
        day_big = QLabel(str(target.day)); day_big.setObjectName("CalendarDateNumber"); day_big.setAlignment(Qt.AlignCenter)
        date_box.addWidget(day_big)
        month_small = QLabel(RO_MONTHS[target.month].upper()); month_small.setObjectName("CalendarDateMonth"); month_small.setAlignment(Qt.AlignCenter)
        date_box.addWidget(month_small)
        year_small = QLabel(str(target.year)); year_small.setObjectName("CalendarDateYear"); year_small.setAlignment(Qt.AlignCenter)
        date_box.addWidget(year_small)
        top.addWidget(date_tile, 0, Qt.AlignTop)

        copy = QVBoxLayout(); copy.setSpacing(6)
        eyebrow = "REPER ACTIV AZI" if exact else ("ÎN JURUL UNUI REPER" if nearby else "ZI FĂRĂ REPER MAJOR")
        k = QLabel(eyebrow); k.setObjectName("Kicker"); copy.addWidget(k)

        if primary is not None:
            event, proximity = primary
            title = QLabel(event.name); title.setObjectName("CalendarSpotlightTitle"); title.setWordWrap(True); copy.addWidget(title)
            if exact:
                explanation = (
                    "Pentru acest reper, filmele factuale trebuie să treacă regula lui specifică. "
                    "«Istorie», «război», «România», «credință» sau alte etichete generale nu sunt suficiente singure."
                )
            elif nearby:
                explanation = (
                    "Data este în fereastra de influență a reperului. Recomandările păstrează aceeași "
                    "regulă strictă și nu primesc o legătură factuală doar din teme generale."
                )
            else:
                explanation = "Nu există un reper concret; aici perioada și anotimpul sunt prezentate explicit ca atmosferă."
            detail = QLabel(explanation); detail.setObjectName("Muted"); detail.setWordWrap(True); copy.addWidget(detail)
            pills = QHBoxLayout(); pills.setSpacing(6)
            pills.addWidget(self.pill(event.category.replace("_", " ").title()))
            pills.addWidget(self.pill(f"{round(float(proximity) * 100)}% context"))
            if len(exact) > 1:
                pills.addWidget(self.pill(f"+{len(exact)-1} repere azi"))
            pills.addStretch(1); copy.addLayout(pills)
        else:
            title = QLabel(phase); title.setObjectName("CalendarSpotlightTitle"); copy.addWidget(title)
            detail = QLabel(
                "Nu există un reper calendaristic concret pentru această dată. Calendarul nu dublează "
                "pagina Recomandări cu filme generale; aici rămâne doar contextul perioadei."
            )
            detail.setObjectName("Muted"); detail.setWordWrap(True); copy.addWidget(detail)

        top.addLayout(copy, 1)
        nav = QHBoxLayout(); nav.setSpacing(6)
        previous = QPushButton("‹ Ziua")
        previous.clicked.connect(lambda _checked=False, d=target-timedelta(days=1): self._open_calendar_date(d))
        following = QPushButton("Ziua ›")
        following.clicked.connect(lambda _checked=False, d=target+timedelta(days=1): self._open_calendar_date(d))
        nav.addWidget(previous); nav.addWidget(following); top.addLayout(nav)
        layout.addLayout(top)

        if result is not None:
            sections = list(result.get("sections") or [])
            unique = {int(rec.movie.id) for section in sections for rec in (section.get("recommendations") or []) if rec.movie.id}
            if bool(result.get("specific_event_active")) and not unique:
                status = "0 potriviri reale în catalogul nevăzut — nu completez ziua cu filme fără legătură."
            elif bool(result.get("specific_event_active")):
                status = f"{len(unique)} filme eligibile au trecut legătura cu reperul acestei zile."
            elif bool(result.get("ordinary_day")):
                status = "Nicio listă de filme aici: ziua nu are un reper concret, iar recomandările generale rămân în tabul Recomandări."
            else:
                status = f"{len(unique)} opțiuni legate de contextul calendaristic."
            label = QLabel(status); label.setObjectName("BodyStrong"); label.setWordWrap(True); layout.addWidget(label)
        elif loading:
            label = QLabel("Verific filmele eligibile și păstrez numai legăturile pe care motorul le poate susține.")
            label.setObjectName("Muted"); label.setWordWrap(True); layout.addWidget(label)

    def _render_calendar_loading(self, target: date):
        self._render_calendar_spotlight(target, loading=True)
        layout = self.calendar_focus_layout
        if layout is None:
            return
        self._clear_layout(layout)
        wait = QLabel("Caut filme pentru ziua selectată…")
        wait.setObjectName("SectionTitle"); layout.addWidget(wait)
        detail = QLabel(
            "Mai întâi verific legătura cu reperul; gustul tău ordonează doar filmele care au trecut acel filtru."
        )
        detail.setObjectName("Muted"); detail.setWordWrap(True); layout.addWidget(detail)

    def _load_calendar_day_async(self, target: date):
        if self._ui_closing or self.current_page != "calendar" or target != self.calendar_selected:
            return
        if self._calendar_cached(target):
            self._render_calendar_program(self.calendar_last_result)
            return

        signature = (target, self._browse_state_signature())
        events = list(self.s.calendar.relevant_events(target))
        concrete = [
            (ev, proximity) for ev, proximity in events
            if ev.category != "sezon" and float(ev.importance) * float(proximity) >= .28
        ]
        if not concrete:
            result = {
                "date": target,
                "phase": self.s.calendar.season_phase(target)[0],
                "events": events,
                "sections": [],
                "specific_event_active": False,
                "ordinary_day": True,
                "pre_rank_count": 0,
                "full_score_count": 0,
            }
            self.calendar_last_result = result
            self.calendar_last_signature = signature
            self.set_status("Zi fără reper calendaristic major.", False)
            self._render_calendar_program(result)
            return

        if self.calendar_worker and self.calendar_worker.isRunning():
            self.calendar_pending = target
            return
        self.calendar_pending = None
        self.set_status(f"Calculez filmele legate de reperul din {target:%d.%m}…", True)
        worker = WorkerThread(lambda progress: self.s.recommender.calendar_day_program(target, 6), self)
        self.calendar_worker = worker

        def success(result):
            self.calendar_worker = None
            if signature == (target, self._browse_state_signature()):
                self.calendar_last_result = result
                self.calendar_last_signature = signature
            self.set_status("Programul zilei este gata.", False)
            if self.current_page == "calendar" and self.calendar_selected == result.get("date") and self._calendar_cached(target):
                self._render_calendar_program(result)
            pending = self.calendar_pending
            self.calendar_pending = None
            if pending is not None and pending != result.get("date"):
                self._render_calendar_loading(pending)
                QTimer.singleShot(0, lambda d=pending: self._load_calendar_day_async(d))
            elif self.current_page == "calendar" and self.calendar_selected == target and not self._calendar_cached(target):
                QTimer.singleShot(0, lambda d=target: self._load_calendar_day_async(d))

        def failure(message):
            self.calendar_worker = None
            pending = self.calendar_pending
            self.calendar_pending = None
            if pending is not None and pending != target:
                QTimer.singleShot(0, lambda d=pending: self._load_calendar_day_async(d))
                return
            self.set_status("Programul calendaristic a eșuat.", False)
            if self.current_page == "calendar" and self.calendar_focus_layout is not None:
                self._clear_layout(self.calendar_focus_layout)
                x = QLabel("Nu am putut calcula recomandările: " + message)
                x.setWordWrap(True); self.calendar_focus_layout.addWidget(x)

        worker.success.connect(success); worker.failure.connect(failure); worker.start()

    def _render_calendar_program(self, result: dict):
        layout = self.calendar_focus_layout
        if layout is None:
            return
        self._render_calendar_spotlight(result["date"], result)
        self._clear_layout(layout)

        sections = list(result.get("sections") or [])
        if not sections:
            if bool(result.get("specific_event_active")):
                text = (
                    "Nu am găsit momentan niciun film nevăzut care să treacă regula specifică a reperului. "
                    "Las ziua goală decât să-ți prezint o potrivire falsă."
                )
            elif bool(result.get("ordinary_day")):
                text = (
                    "Ziua nu are un reper calendaristic concret. Nu afișez aici filme generale doar ca să umplu pagina."
                )
            else:
                text = "Nu am găsit suficiente filme eligibile pentru această zi."
            label = QLabel(text); label.setObjectName("BodyStrong"); label.setWordWrap(True); layout.addWidget(label)
            if bool(result.get("ordinary_day")):
                go = QPushButton("Deschide Recomandări")
                go.clicked.connect(lambda _checked=False: self.show_page("recommendations"))
                layout.addWidget(go, alignment=Qt.AlignLeft)
            return

        stats = QLabel(
            f"Analiză: {int(result.get('pre_rank_count', 0)):,} candidați rapizi • "
            f"{int(result.get('full_score_count', 0)):,} finaliști evaluați complet • filmele văzute rămân excluse."
        )
        stats.setObjectName("Muted"); stats.setWordWrap(True); layout.addWidget(stats)

        for section in sections:
            recs = list(section.get("recommendations") or [])
            if not recs:
                continue
            head = QHBoxLayout()
            sh = QLabel(section["title"]); sh.setObjectName("SectionTitle"); head.addWidget(sh, 1)
            head.addWidget(self.pill(f"{len(recs)} filme")); layout.addLayout(head)
            ss = QLabel(section["subtitle"]); ss.setObjectName("Muted"); ss.setWordWrap(True); layout.addWidget(ss)
            grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
            for i, rec in enumerate(recs):
                grid.addWidget(self.calendar_movie_card(rec), i // 2, i % 2)
            wrap = QFrame(); wrap.setLayout(grid); layout.addWidget(wrap)

    def calendar_movie_card(self, rec: Recommendation):
        m, s = rec.movie, rec.score
        card = QFrame(); card.setObjectName("CalendarMovieCard"); card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        main = QHBoxLayout(card); main.setContentsMargins(16,16,16,16); main.setSpacing(16)
        poster = self.poster_label(112,168); main.addWidget(poster, 0, Qt.AlignTop)
        if m.poster_url:
            self.load_poster_async(poster, m.poster_url, m.imdb_id or str(m.id))
        l = QVBoxLayout(); l.setSpacing(5)
        title = QLabel(m.title + (f" ({m.year})" if m.year else "")); title.setObjectName("CardTitle"); title.setWordWrap(True); l.addWidget(title)
        score = QLabel(f"{s.predicted_rating:.1f}/10 pentru tine • {round(s.confidence*100)}% încredere")
        score.setObjectName("Score"); l.addWidget(score)
        meta = QLabel(" • ".join(self.movie_chips(m, 5))); meta.setObjectName("Muted"); meta.setWordWrap(True); l.addWidget(meta)
        relation_head = QLabel(str(s.calendar_kind or "legătură verificată").upper())
        relation_head.setObjectName("CalendarRelationKind"); l.addWidget(relation_head)
        relation = QLabel(s.calendar_reason)
        relation.setObjectName("CalendarRelationText"); relation.setWordWrap(True); l.addWidget(relation)
        row = QHBoxLayout()
        details = QPushButton("Detalii"); details.clicked.connect(lambda _, r=rec: self.open_details(r)); row.addWidget(details)
        watch = QPushButton("Watchlist"); watch.clicked.connect(lambda _, mid=m.id: self.feedback(mid, "want_to_watch")); row.addWidget(watch)
        no = QPushButton("Ascunde"); no.clicked.connect(lambda _, mid=m.id: self.feedback(mid, "not_interested")); row.addWidget(no)
        row.addStretch(1); l.addLayout(row)
        main.addLayout(l, 1)
        return card

    # Use the verified bundle-aware updater page from DecisionWindow instead of the obsolete
    # placeholder inherited from the first Premium prototype.
    def page_updates(self):
        return DecisionWindow.page_updates(self)


def run_premium_calendar(service, on_ready=None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("CineCalendar")
    app.setOrganizationName("CineCalendar")
    try:
        app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    w = CalendarPremiumWindow(service)
    w.show()
    if on_ready is not None:
        QTimer.singleShot(350, on_ready)
    return app.exec()
