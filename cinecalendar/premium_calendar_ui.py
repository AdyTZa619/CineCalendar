from __future__ import annotations

import sys
from calendar import month_name, monthrange
from datetime import date

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


class CalendarPremiumWindow(PremiumDecisionWindow):
    """Premium UI with a real day-by-day calendar program.

    The page is intentionally instant: month context is rendered immediately and only the
    selected day's movie program is calculated in a worker. Reopening a day uses the engine
    cache instead of recalculating multiple month intervals.
    """

    def __init__(self, service):
        self.calendar_worker: WorkerThread | None = None
        self.calendar_focus_layout: QVBoxLayout | None = None
        self.calendar_selected: date = date.today()
        self.calendar_month_anchor: date = date.today().replace(day=1)
        self.calendar_pending: date | None = None
        self.calendar_last_result: dict | None = None
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
        self.show_page("month")

    def _go_today(self):
        self.calendar_month_anchor = date.today().replace(day=1)
        self.calendar_selected = date.today()
        self.show_page("month")

    def _open_calendar_date(self, target: date):
        self.calendar_month_anchor = target.replace(day=1)
        self.calendar_selected = target
        self.show_page("month")

    # ---------- complete calendar reference ----------
    def page_calendar(self):
        today = date.today()
        year = self.calendar_month_anchor.year if self.calendar_month_anchor else today.year
        page, content = self.page_shell(
            f"Calendar {year}",
            "Repere ortodoxe, perioade de post, tradiții românești, date istorice, civice și sezoniere. Fiecare reper poate deschide recomandările lui de filme.",
            [
                ("Anul anterior", lambda: self._change_calendar_year(-1), False),
                ("Anul următor", lambda: self._change_calendar_year(1), False),
                ("Azi", self._go_today, True),
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
        x = QLabel("Nu este o listă de câteva sărbători puse manual în UI: CalendarEngine furnizează perioade active și influențe înainte/după reper, iar Program calendar folosește aceste relații în scorul filmelor.")
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
        self.show_page("calendar")

    # ---------- fast month / selected day program ----------
    def page_month(self):
        anchor = self.calendar_month_anchor
        if self.calendar_selected.year != anchor.year or self.calendar_selected.month != anchor.month:
            self.calendar_selected = anchor

        page, content = self.page_shell(
            f"Program calendar — {RO_MONTHS[anchor.month]} {anchor.year}",
            "Fiecare zi are contextul ei. Alege orice dată și primești filme legate expres de reperul religios, istoric, spiritual, atmosferic sau sezonier activ în ziua respectivă.",
            [
                ("‹ Luna anterioară", lambda: self._shift_month(-1), False),
                ("Azi", self._go_today, True),
                ("Luna următoare ›", lambda: self._shift_month(1), False),
            ],
        )

        focus = QFrame(); focus.setObjectName("HeroCard")
        self.calendar_focus_layout = QVBoxLayout(focus)
        self.calendar_focus_layout.setContentsMargins(24,22,24,22)
        self.calendar_focus_layout.setSpacing(12)
        content.addWidget(focus)
        self._render_calendar_loading(self.calendar_selected)

        head = QLabel("Luna, zi cu zi")
        head.setObjectName("SectionTitle"); content.addWidget(head)
        expl = QLabel("Nu trebuie să existe o sărbătoare mare ca o zi să fie utilizabilă: zilele fără reper nominal folosesc perioada ortodoxă activă și atmosfera sezonului. Zilele cu repere au etichete explicite.")
        expl.setObjectName("Muted"); expl.setWordWrap(True); content.addWidget(expl)

        days_grid = QGridLayout(); days_grid.setHorizontalSpacing(10); days_grid.setVerticalSpacing(8)
        last_day = monthrange(anchor.year, anchor.month)[1]
        for day_no in range(1, last_day + 1):
            d = date(anchor.year, anchor.month, day_no)
            events = self.s.calendar.relevant_events(d)
            phase, _tags = self.s.calendar.season_phase(d)
            card = QFrame(); card.setObjectName("PremiumCard")
            row = QHBoxLayout(card); row.setContentsMargins(13,10,13,10); row.setSpacing(10)
            date_label = QLabel(d.strftime("%d.%m")); date_label.setObjectName("BodyStrong"); date_label.setFixedWidth(48); row.addWidget(date_label)
            if events:
                names = [ev.name for ev, _ in events[:2]]
                context = QLabel(" • ".join(names))
            else:
                context = QLabel(phase)
            context.setObjectName("Muted"); context.setWordWrap(True); row.addWidget(context, 1)
            btn = QPushButton("Filme")
            if d == self.calendar_selected:
                btn.setProperty("accent", True)
            btn.clicked.connect(lambda _, target=d: self._select_calendar_day(target))
            row.addWidget(btn)
            days_grid.addWidget(card, (day_no - 1) // 2, (day_no - 1) % 2)
        wrap = QFrame(); wrap.setLayout(days_grid); content.addWidget(wrap)
        content.addStretch(1)

        QTimer.singleShot(0, lambda d=self.calendar_selected: self._load_calendar_day_async(d))
        return page

    def _select_calendar_day(self, target: date):
        self.calendar_selected = target
        self._render_calendar_loading(target)
        self._load_calendar_day_async(target)

    def _render_calendar_loading(self, target: date):
        layout = self.calendar_focus_layout
        if layout is None:
            return
        self._clear_layout(layout)
        h = QLabel(f"Filme pentru {target:%d.%m.%Y}")
        h.setObjectName("SectionTitle"); layout.addWidget(h)
        events = self.s.calendar.relevant_events(target)
        phase, _ = self.s.calendar.season_phase(target)
        if events:
            text = " • ".join(ev.name for ev, _ in events[:4])
        else:
            text = f"Fără reper nominal major • {phase}"
        c = QLabel(text); c.setObjectName("Muted"); c.setWordWrap(True); layout.addWidget(c)
        wait = QLabel("Calculez o singură dată pool-ul zilei și construiesc categoriile…")
        wait.setObjectName("Muted"); layout.addWidget(wait)

    def _load_calendar_day_async(self, target: date):
        if self.calendar_worker and self.calendar_worker.isRunning():
            self.calendar_pending = target
            return
        self.calendar_pending = None
        self.set_status(f"Calculez programul pentru {target:%d.%m}…", True)
        worker = WorkerThread(lambda progress: self.s.recommender.calendar_day_program(target, 6), self)
        self.calendar_worker = worker

        def success(result):
            self.calendar_worker = None
            self.calendar_last_result = result
            self.set_status("Programul zilei este gata.", False)
            if self.current_page == "month" and self.calendar_selected == result.get("date"):
                self._render_calendar_program(result)
            pending = self.calendar_pending
            self.calendar_pending = None
            if pending is not None and pending != result.get("date"):
                self._render_calendar_loading(pending)
                QTimer.singleShot(0, lambda d=pending: self._load_calendar_day_async(d))

        def failure(message):
            self.calendar_worker = None
            self.set_status("Programul calendaristic a eșuat.", False)
            if self.current_page == "month" and self.calendar_focus_layout is not None:
                self._clear_layout(self.calendar_focus_layout)
                x = QLabel("Nu am putut calcula recomandările: " + message)
                x.setWordWrap(True); self.calendar_focus_layout.addWidget(x)

        worker.success.connect(success); worker.failure.connect(failure); worker.start()

    def _render_calendar_program(self, result: dict):
        layout = self.calendar_focus_layout
        if layout is None:
            return
        self._clear_layout(layout)
        target = result["date"]
        h = QLabel(f"Pentru {target:%d.%m.%Y} — {result.get('phase', '')}")
        h.setObjectName("SectionTitle"); layout.addWidget(h)

        events = result.get("events") or []
        if events:
            event_box = QFrame(); event_box.setObjectName("PremiumCard")
            el = QVBoxLayout(event_box); el.setContentsMargins(16,14,16,14); el.setSpacing(6)
            eh = QLabel("Context activ în ziua selectată"); eh.setObjectName("BodyStrong"); el.addWidget(eh)
            for ev, proximity in events[:8]:
                row = QHBoxLayout()
                n = QLabel(ev.name); n.setWordWrap(True); row.addWidget(n, 1)
                row.addWidget(self.pill(ev.category.replace("_", " ").title()))
                strength = QLabel(f"{round(proximity * 100)}%")
                strength.setObjectName("Muted"); row.addWidget(strength)
                el.addLayout(row)
            layout.addWidget(event_box)
        else:
            x = QLabel("Nu există un reper nominal major în această dată; recomandările de mai jos sunt marcate explicit ca sezoniere/atmosferice, nu ca legătură directă.")
            x.setObjectName("Muted"); x.setWordWrap(True); layout.addWidget(x)

        stats = QLabel(
            f"Pool rapid: {int(result.get('pre_rank_count', 0)):,} candidați • scor complet: {int(result.get('full_score_count', 0)):,}. "
            "Filmele deja evaluate/văzute rămân excluse."
        )
        stats.setObjectName("Muted"); stats.setWordWrap(True); layout.addWidget(stats)

        sections = result.get("sections") or []
        if not sections:
            n = QLabel("Nu am găsit suficiente filme eligibile cu o legătură calendaristică reală pentru ziua asta.")
            n.setObjectName("Muted"); layout.addWidget(n); return

        for section in sections:
            sh = QLabel(section["title"]); sh.setObjectName("SectionTitle"); layout.addWidget(sh)
            ss = QLabel(section["subtitle"]); ss.setObjectName("Muted"); ss.setWordWrap(True); layout.addWidget(ss)
            grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
            for i, rec in enumerate(section["recommendations"]):
                grid.addWidget(self.calendar_movie_card(rec), i // 2, i % 2)
            wrap = QFrame(); wrap.setLayout(grid); layout.addWidget(wrap)

    def calendar_movie_card(self, rec: Recommendation):
        m, s = rec.movie, rec.score
        card = QFrame(); card.setObjectName("PremiumCard"); card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        main = QHBoxLayout(card); main.setContentsMargins(14,14,14,14); main.setSpacing(12)
        poster = self.poster_label(82,123); main.addWidget(poster, 0, Qt.AlignTop)
        if m.poster_url:
            self.load_poster_async(poster, m.poster_url, m.imdb_id or str(m.id))
        l = QVBoxLayout(); l.setSpacing(5)
        title = QLabel(m.title + (f" ({m.year})" if m.year else "")); title.setObjectName("CardTitle"); title.setWordWrap(True); l.addWidget(title)
        score = QLabel(f"{s.predicted_rating:.1f}/10 pentru tine • {round(s.confidence*100)}% încredere")
        score.setObjectName("Score"); l.addWidget(score)
        meta = QLabel(" • ".join(self.movie_chips(m, 5))); meta.setObjectName("Muted"); meta.setWordWrap(True); l.addWidget(meta)
        relation = QLabel(f"Legătura: {s.calendar_kind} • {s.calendar_reason}")
        relation.setObjectName("BodyStrong"); relation.setWordWrap(True); l.addWidget(relation)
        row = QHBoxLayout()
        details = QPushButton("Detalii"); details.clicked.connect(lambda _, r=rec: self.open_details(r)); row.addWidget(details)
        watch = QPushButton("Watchlist"); watch.clicked.connect(lambda _, mid=m.id: self.feedback(mid, "want_to_watch")); row.addWidget(watch)
        no = QPushButton("Nu"); no.clicked.connect(lambda _, mid=m.id: self.feedback(mid, "not_interested")); row.addWidget(no)
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
