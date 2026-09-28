"""Three selectable visual shells for the production Qt interface.

The skin is presentation state. Recommendation rounds, exposures and user actions
stay in the existing service and decision methods.
"""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)


SKINS = {
    "cinematic": ("Cinematic", "Film în prim-plan, alternative într-un rând."),
    "editorial": ("Editorial", "Afiș și explicație, cu alternative alături."),
    "workbench": ("Workbench", "Rezultate comparabile și detalii la selecție."),
}

PALETTES = {
    "cinematic": dict(bg="#0B0B0D", surface="#151416", card="#1B1A1D", card2="#282529",
                      text="#F7F1E7", muted="#B8AFA5", border="#3B3535",
                      accent="#D9AF69", on="#21170B", good="#D7BB80"),
    "editorial": dict(bg="#F6F1E8", surface="#FBF8F1", card="#FFFCF6", card2="#EEE6DA",
                     text="#241F1D", muted="#6D625D", border="#D5C8BB",
                     accent="#792B30", on="#FFFFFF", good="#815C35"),
    "workbench": dict(bg="#10191D", surface="#152126", card="#1B292F", card2="#26373D",
                     text="#EDF3F1", muted="#ADBBB9", border="#375057",
                     accent="#E3A16D", on="#201912", good="#88CCBC"),
}


def normalized_skin(value: object) -> str:
    value = str(value or "").lower().strip()
    return value if value in SKINS else "cinematic"


def _label(text: str, name: str = "Muted", wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _skin_preview(skin: str) -> QFrame:
    """Tiny layout preview made from Qt widgets, with no fictitious movie data."""
    c = PALETTES[skin]
    frame = QFrame()
    frame.setFixedHeight(84)
    frame.setStyleSheet(f"background:{c['bg']};border:1px solid {c['border']};border-radius:4px;")

    def block(width=0, stretch=0, fill=None):
        part = QFrame()
        part.setStyleSheet(f"background:{fill or c['card2']};border:0;border-radius:2px;")
        if width:
            part.setFixedWidth(width)
        return part, stretch

    if skin == "cinematic":
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        top, _ = block(fill=c['accent']); top.setFixedHeight(8); layout.addWidget(top)
        hero, _ = block(fill=c['card2']); layout.addWidget(hero, 1)
        rail = QHBoxLayout(); rail.setSpacing(4)
        for _ in range(3):
            tile, _ = block(); tile.setFixedHeight(13); rail.addWidget(tile, 1)
        layout.addLayout(rail)
    else:
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        if skin == "workbench":
            rail, _ = block(9, fill=c['accent']); layout.addWidget(rail)
        nav, _ = block(27 if skin == "workbench" else 36, fill=c['surface']); layout.addWidget(nav)
        main, _ = block(fill=c['card2']); layout.addWidget(main, 3)
        info, _ = block(fill=c['card']); layout.addWidget(info, 1 if skin == "editorial" else 2)
    return frame


def _status_widgets(window):
    window.undo_feedback_button = QPushButton("Anulează feedback")
    window.undo_feedback_button.setEnabled(False)
    window.undo_feedback_button.setToolTip("Anulează ultima acțiune de feedback din sesiune (Ctrl+Z).")
    window.undo_feedback_button.clicked.connect(window.undo_last_feedback)
    window.status = _label("Pregătit")
    window.status.setMaximumHeight(44)
    window.status.setWordWrap(True)
    window.progress = QProgressBar()
    window.progress.setVisible(False)
    window.progress.setRange(0, 0)


def build_skin_shell(window):
    window.skin = normalized_skin(window.db.get_setting("ui_skin", "cinematic"))
    root = QWidget()
    window.setCentralWidget(root)
    _status_widgets(window)
    window.nav_buttons = {}
    window.stack = QStackedWidget()

    if window.skin == "cinematic":
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top = QFrame()
        top.setObjectName("Topbar")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(26, 7, 26, 5)
        tl.setSpacing(2)
        first = QHBoxLayout()
        first.addWidget(_label("◉  CineCalendar", "Brand"))
        first.addStretch(1)
        first.addWidget(window.status)
        first.addWidget(window.progress)
        first.addWidget(window.undo_feedback_button)
        tl.addLayout(first)
        for chunk in (window.NAV[:7], window.NAV[7:]):
            row = QHBoxLayout()
            row.setSpacing(2)
            for key, name in chunk:
                button = QPushButton(name)
                button.setProperty("nav", True)
                button.setToolTip(name)
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                button.clicked.connect(lambda _=False, k=key: window.show_page(k))
                window.nav_buttons[key] = button
                row.addWidget(button, 1)
            tl.addLayout(row)
        outer.addWidget(top)
        outer.addWidget(window.stack, 1)
        return

    outer = QHBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    if window.skin == "workbench":
        rail = QFrame()
        rail.setObjectName("IconRail")
        rail.setFixedWidth(49)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(5, 13, 5, 13)
        rl.setSpacing(9)
        for glyph, key in (("▶", "today"), ("★", "recommendations"),
                           ("▦", "ratings"), ("◷", "calendar"), ("⚙", "settings")):
            g = QPushButton(glyph)
            g.setObjectName("RailAction")
            g.setToolTip(next((name for item, name in window.NAV if item == key), key))
            g.clicked.connect(lambda _=False, target=key: window.show_page(target))
            rl.addWidget(g)
        rl.addStretch(1)
        outer.addWidget(rail)

    side = QFrame()
    side.setObjectName("Sidebar")
    side.setFixedWidth(263 if window.skin == "editorial" else 229)
    sl = QVBoxLayout(side)
    sl.setContentsMargins(17, 15, 17, 12)
    sl.setSpacing(4)
    sl.addWidget(_label("CineCalendar", "Brand"))
    sl.addWidget(_label("FILME ALESE PENTRU TIMPUL TĂU" if window.skin == "workbench"
                        else "Filme bune, la timpul tău."))
    sl.addSpacing(12)
    nav_scroll = QScrollArea()
    nav_scroll.setObjectName("SidebarNav")
    nav_scroll.setWidgetResizable(True)
    nav_scroll.setFrameShape(QFrame.NoFrame)
    nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    nav_body = QWidget()
    nav_body.setObjectName("SidebarNavContent")
    nl = QVBoxLayout(nav_body)
    nl.setContentsMargins(0, 0, 3, 0)
    nl.setSpacing(2)
    for index, (key, name) in enumerate(window.NAV):
        if index in (0, 4, 7, 10):
            group = {0: "DESCOPERĂ", 4: "BIBLIOTECĂ", 7: "PLANIFICĂ",
                     10: "INSTRUMENTE"}[index]
            header = _label(group, "NavGroup")
            nl.addWidget(header)
        button = QPushButton(name)
        button.setProperty("nav", True)
        button.setToolTip(name)
        button.clicked.connect(lambda _=False, k=key: window.show_page(k))
        nl.addWidget(button)
        window.nav_buttons[key] = button
    nl.addStretch(1)
    nav_scroll.setWidget(nav_body)
    sl.addWidget(nav_scroll, 1)
    sl.addWidget(window.undo_feedback_button)
    sl.addWidget(window.status)
    sl.addWidget(window.progress)
    sl.addWidget(_label("v" + str(window.windowTitle().split(" ")[1])
                        if len(window.windowTitle().split(" ")) > 1 else "CineCalendar"))
    outer.addWidget(side)
    outer.addWidget(window.stack, 1)


def skin_qss(skin: str) -> str:
    c = PALETTES[skin]
    editorial = skin == "editorial"
    sharp = skin == "workbench"
    radius = "5px" if sharp else ("3px" if editorial else "13px")
    title_font = "'Georgia'" if editorial else "'Segoe UI'"
    title_weight = "600" if editorial else "750"
    padding = "7px 9px" if sharp else "9px 13px"
    return f"""
        QWidget {{ background:{c['bg']}; color:{c['text']}; font-family:'Segoe UI'; font-size:14px; }}
        QMainWindow, QScrollArea, QScrollArea>QWidget>QWidget {{ background:{c['bg']}; }}
        QLabel {{ background:transparent; }}
        QFrame#Sidebar, QFrame#Topbar, QFrame#IconRail {{ background:{c['surface']}; border-color:{c['border']}; }}
        QFrame#Sidebar {{ border-right:1px solid {c['border']}; }}
        QFrame#Topbar {{ border-bottom:1px solid {c['border']}; }}
        QFrame#IconRail {{ border-right:1px solid {c['border']}; }}
        QScrollArea#SidebarNav, QWidget#SidebarNavContent {{ background:transparent; border:0; }}
        QFrame#PremiumCard, QFrame#Card, QFrame#HeroCard, QFrame#DetailHero {{
            background:{c['card']}; border:1px solid {c['border']}; border-radius:{radius};
        }}
        QFrame#HeroCard {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 {c['card2']}, stop:.62 {c['card']}, stop:1 {c['bg']}); }}
        QLabel#Brand {{ font-family:{title_font}; font-size:25px; font-weight:750; color:{c['accent']}; }}
        QLabel#PageTitle {{ font-family:{title_font}; font-size:{'37' if editorial else '32'}px;
            font-weight:{title_weight}; color:{c['text']}; }}
        QLabel#HeroTitle {{ font-family:{title_font}; font-size:{'36' if editorial else '30'}px;
            font-weight:{title_weight}; }}
        QLabel#SectionTitle {{ font-family:{title_font}; font-size:21px; font-weight:700; }}
        QLabel#CardTitle {{ font-size:17px; font-weight:700; }}
        QLabel#Muted, QLabel#NavGroup {{ color:{c['muted']}; }}
        QLabel#NavGroup {{ font-size:10px; font-weight:700; margin-top:8px; }}
        QLabel#RailGlyph {{ font-size:21px; color:{c['accent']}; }}
        QPushButton#RailAction {{ border:0; background:transparent; color:{c['accent']};
            font-size:19px; padding:7px 2px; }}
        QPushButton#RailAction:hover {{ background:{c['card2']}; }}
        QLabel#Kicker, QLabel#Score, QLabel#MetricValue {{ color:{c['accent']}; }}
        QLabel#ScoreLarge, QLabel#SignalPositive {{ color:{c['good']}; }}
        QLabel#SignalNegative {{ color:#D97978; }}
        QLabel#Pill {{ background:{c['card2']}; color:{c['muted']}; border:1px solid {c['border']};
                        border-radius:{radius}; padding:5px 9px; }}
        QLabel#ScoreBadge {{ background:{c['accent']}; color:{c['on']}; border-radius:36px;
                             font-size:20px; font-weight:800; }}
        QFrame#ReliabilityGood, QFrame#ReliabilityCaution, QFrame#ReliabilityBad {{
            background:{c['card2']}; border:1px solid {c['border']}; border-radius:{radius}; }}
        QPushButton {{ background:{c['card2']}; color:{c['text']}; border:1px solid {c['border']};
                      border-radius:{radius}; padding:{padding}; font-weight:600; }}
        QPushButton:hover {{ border-color:{c['accent']}; }}
        QPushButton:focus {{ border-color:{c['accent']}; }}
        QPushButton:disabled {{ color:{c['muted']}; background:{c['surface']}; }}
        QPushButton[accent='true'] {{ background:{c['accent']}; color:{c['on']}; border-color:{c['accent']}; font-weight:750; }}
        QPushButton[nav='true'] {{ text-align:left; background:transparent; border:0; color:{c['muted']}; }}
        QPushButton[nav='true']:hover {{ background:{c['card2']}; color:{c['text']}; }}
        QPushButton[navActive='true'] {{ text-align:left; background:{c['card2']};
            color:{c['text']}; border-left:3px solid {c['accent']}; font-weight:750; }}
        QFrame#Topbar QPushButton[nav='true'], QFrame#Topbar QPushButton[navActive='true'] {{
            text-align:center; font-size:12px; padding:6px 2px; }}
        QLineEdit, QSpinBox, QComboBox {{ background:{c['card2']}; color:{c['text']};
            border:1px solid {c['border']}; border-radius:{radius}; padding:7px; }}
        QComboBox QAbstractItemView {{ background:{c['surface']}; color:{c['text']};
            selection-background-color:{c['accent']}; selection-color:{c['on']}; }}
        QProgressBar {{ border:1px solid {c['border']}; border-radius:5px; background:{c['card2']}; }}
        QProgressBar::chunk {{ background:{c['accent']}; }}
        QTableView, QTableWidget {{ background:{c['card']}; alternate-background-color:{c['card2']};
            color:{c['text']}; gridline-color:{c['border']}; border:1px solid {c['border']};
            selection-background-color:{c['accent']}; selection-color:{c['on']}; }}
        QTableView::item, QTableWidget::item {{ color:{c['text']}; padding:5px; }}
        QTableView::item:selected, QTableWidget::item:selected {{ background:{c['accent']}; color:{c['on']}; }}
        QHeaderView::section {{ background:{c['surface']}; color:{c['text']}; border:0;
            border-bottom:1px solid {c['border']}; padding:8px; }}
        QScrollBar:vertical {{ background:transparent; width:11px; }}
        QScrollBar::handle:vertical {{ background:{c['border']}; min-height:30px; border-radius:5px; }}
    """


def _action(window, text, handler, primary=False):
    button = QPushButton(text)
    button.setProperty("accent", bool(primary))
    button.clicked.connect(handler)
    return button


def _film_title(rec) -> str:
    return rec.movie.title + (f"  ({rec.movie.year})" if rec.movie.year else "")


def _film_panel(window, rec, *, cinematic=False):
    box = QFrame()
    box.setObjectName("HeroCard" if cinematic else "PremiumCard")
    layout = QHBoxLayout(box)
    layout.setContentsMargins(22, 17, 22, 17)
    layout.setSpacing(25)
    poster = window.poster_label(174 if cinematic else 192, 246 if cinematic else 282)
    if not rec.movie.poster_url:
        poster.setText("Poster indisponibil")
    if not cinematic:
        layout.addWidget(poster, 0, Qt.AlignTop)
    info = QVBoxLayout()
    info.setSpacing(11)
    info.addWidget(_label("ALEGEREA ZILEI  ·  DATE REALE", "Kicker"))
    info.addWidget(_label(_film_title(rec), "HeroTitle", True))
    meta = "  ·  ".join(window.movie_chips(rec.movie, 4))
    info.addWidget(_label(meta, "Muted", True))
    info.addWidget(window.reliability_widget(rec, compact=True))
    info.addWidget(_label(window.human_reason(rec), "BodyStrong", True))
    if rec.score.calendar_reason:
        info.addWidget(_label("Context: " + rec.score.calendar_reason, "Muted", True))
    info.addStretch(1)
    choose = _action(window, "Aleg pentru azi", lambda _=False, mid=rec.movie.id: window.choose_decision(mid), True)
    details = _action(window, "De ce acesta?", lambda _=False, r=rec: window.open_details(r))
    feedback = QPushButton("Nu acum / motiv")
    feedback.setToolTip("Alege un motiv temporar sau spune ce nu ți se potrivește.")
    feedback.clicked.connect(lambda _=False, mid=rec.movie.id, button=feedback:
                             window.contextual_feedback_menu(mid, button))
    skip = _action(window, "Alt film", lambda _=False, mid=rec.movie.id: window.skip_decision(mid))
    actions = QHBoxLayout() if cinematic else QGridLayout()
    if cinematic:
        for button in (choose, details, feedback, skip):
            actions.addWidget(button)
    else:
        for row, column, button in ((0, 0, choose), (0, 1, details),
                                    (1, 0, feedback), (1, 1, skip)):
            actions.addWidget(button, row, column)
    info.addLayout(actions)
    layout.addLayout(info, 1)
    if cinematic:
        layout.addWidget(poster, 0, Qt.AlignTop)
    if rec.movie.poster_url:
        window.load_poster_async(poster, rec.movie.poster_url, rec.movie.imdb_id or str(rec.movie.id))
    return box


def _alternative(window, rec, index):
    box = QFrame()
    box.setObjectName("PremiumCard")
    layout = QHBoxLayout(box)
    layout.setContentsMargins(13, 12, 13, 12)
    layout.setSpacing(10)
    poster = window.poster_label(67, 99)
    if not rec.movie.poster_url:
        poster.setText("Fără afiș")
    layout.addWidget(poster)
    if rec.movie.poster_url:
        window.load_poster_async(poster, rec.movie.poster_url, rec.movie.imdb_id or str(rec.movie.id))
    detail = QVBoxLayout()
    detail.addWidget(_label(f"{index}.  {_film_title(rec)}", "CardTitle", True))
    detail.addWidget(_label(f"Estimare personală {rec.score.predicted_rating:.1f}/10", "Muted", True))
    actions = QHBoxLayout()
    actions.addWidget(_action(window, "Detalii", lambda _=False, r=rec: window.open_details(r)))
    actions.addWidget(_action(window, "Aleg", lambda _=False, mid=rec.movie.id: window.choose_decision(mid)))
    detail.addLayout(actions)
    layout.addLayout(detail, 1)
    return box


def _quick_controls(window):
    bar = QFrame()
    bar.setObjectName("PremiumCard")
    row = QHBoxLayout(bar)
    row.setContentsMargins(14, 9, 14, 9)
    row.setSpacing(8)
    row.addWidget(_label("Cum alegem:", "Muted"))
    for title, key in (("Echilibrat", "decide"), ("Mai sigur", "safe"),
                       ("Surprinde-mă", "surprise"), ("Mai scurt", "short")):
        row.addWidget(_action(window, title,
                    lambda _=False, value=key: window.set_decision_mode(value),
                    key == window.decision_mode))
    runtime = QComboBox()
    for name, value in (("Orice durată", "all"), ("≤60 min", "60"),
                        ("≤90 min", "90"), ("≤120 min", "120"), ("180+ min", "180plus")):
        runtime.addItem(name, value)
    runtime.setCurrentIndex(max(0, runtime.findData(str(window.db.get_setting("chooser_runtime_bucket", "all")))))
    runtime.currentIndexChanged.connect(lambda _=0: _set_filter(window, "chooser_runtime_bucket", runtime.currentData()))
    row.addWidget(runtime)
    mood = QComboBox()
    for name, value in (("Orice ton", "neutral"), ("Lejer", "light"),
                        ("Intens", "intense"), ("Contemplativ", "contemplative"),
                        ("Ușor de urmărit", "easy")):
        mood.addItem(name, value)
    mood.setCurrentIndex(max(0, mood.findData(str(window.db.get_setting("chooser_mood", "neutral")))))
    mood.currentIndexChanged.connect(lambda _=0: _set_filter(window, "chooser_mood", mood.currentData()))
    row.addWidget(mood)
    row.addStretch(1)
    return bar


def _set_filter(window, name, value):
    window.db.set_setting(name, str(value or ""))
    window.show_page("today")


def render_skin_today(window, primary, backups):
    content = window.today_content
    if content is None:
        return
    window._clear_layout(content)
    if primary is None:
        content.addWidget(_label("Nu am găsit momentan un titlu suficient de bun după filtrele tale.", "Muted", True))
        return
    visible = [primary, *list(backups or [])[:2]]
    window.reliability_gate.refresh()
    window.record_once(visible, date.today(), "decision")
    window._schedule_retrieval_shadow(visible, "decision")
    content.addWidget(_quick_controls(window))
    skin = normalized_skin(getattr(window, "skin", None))
    if skin == "cinematic":
        content.addWidget(_film_panel(window, primary, cinematic=True))
        if backups:
            content.addWidget(_label("ALTE ALEGERI PENTRU DISEARĂ", "SectionTitle"))
            row = QHBoxLayout()
            for i, rec in enumerate(backups[:2], 2):
                row.addWidget(_alternative(window, rec, i), 1)
            wrap = QWidget()
            wrap.setLayout(row)
            content.addWidget(wrap)
    elif skin == "editorial":
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_film_panel(window, primary), 3)
        side = QFrame()
        side.setObjectName("PremiumCard")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 16, 14, 16)
        sl.addWidget(_label("ALTERNATIVE", "SectionTitle"))
        for i, rec in enumerate(backups[:2], 2):
            sl.addWidget(_alternative(window, rec, i))
        sl.addStretch(1)
        row.addWidget(side, 1)
        wrap = QWidget()
        wrap.setLayout(row)
        content.addWidget(wrap)
    else:
        row = QHBoxLayout()
        row.setSpacing(14)
        queue = QFrame()
        queue.setObjectName("PremiumCard")
        ql = QVBoxLayout(queue)
        ql.setContentsMargins(14, 15, 14, 15)
        ql.addWidget(_label(f"{len(visible)} rezultate pregătite", "SectionTitle"))
        inspector = QStackedWidget()
        buttons = []
        for i, rec in enumerate(visible, 1):
            selector = _alternative(window, rec, i)
            pick = _action(window, "Selectează", lambda _=False, index=i-1: _select(inspector, buttons, index))
            selector.layout().itemAt(1).layout().addWidget(pick)
            buttons.append(pick)
            ql.addWidget(selector)
            inspector.addWidget(_film_panel(window, rec))
        ql.addStretch(1)
        _select(inspector, buttons, 0)
        row.addWidget(queue, 2)
        row.addWidget(inspector, 3)
        wrap = QWidget()
        wrap.setLayout(row)
        content.addWidget(wrap)
    content.addStretch(1)


def _select(stack, buttons, index):
    stack.setCurrentIndex(index)
    for i, button in enumerate(buttons):
        button.setProperty("accent", i == index)
        button.style().unpolish(button)
        button.style().polish(button)


def install_premium_skins(window_cls) -> None:
    if getattr(window_cls, "_cinecalendar_skin_installed", False):
        return
    original_settings = window_cls.page_settings
    original_page_shell = window_cls.page_shell

    def _build_shell(self):
        build_skin_shell(self)

    def apply_theme(self):
        self.skin = normalized_skin(self.db.get_setting("ui_skin", "cinematic"))
        self.theme = "light" if self.skin == "editorial" else "dark"
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(skin_qss(self.skin))

    def set_skin(self, value):
        value = normalized_skin(value)
        if value == getattr(self, "skin", None):
            if self.db.get_setting("ui_skin") != value:
                self.db.set_setting("ui_skin", value)
            return
        self.db.set_setting("ui_skin", value)
        self.skin = value
        self._build_shell()
        self.apply_theme()
        self.show_page("settings")
        self.set_status(f"Skin activ: {SKINS[value][0]}. Alegerea este salvată.", False)

    def page_settings(self):
        page = original_settings(self)
        scroll = next((item for item in page.findChildren(QScrollArea)
                       if item.widget() is not None and item.widget().layout() is not None), None)
        if scroll is None:
            return page
        content = scroll.widget().layout()
        panel = QFrame()
        panel.setObjectName("PremiumCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(_label("Aspectul aplicației", "SectionTitle"))
        layout.addWidget(_label("Alege cum sunt așezate meniurile și recomandările. Schimbarea se aplică imediat și se păstrează după repornire.", "Muted", True))
        cards = QHBoxLayout()
        for key, (name, description) in SKINS.items():
            item = QFrame()
            item.setObjectName("PremiumCard")
            column = QVBoxLayout(item)
            column.addWidget(_label(name, "CardTitle"))
            column.addWidget(_skin_preview(key))
            column.addWidget(_label(description, "Muted", True))
            button = _action(self, "Activ" if key == self.skin else "Folosește skinul",
                             lambda _=False, selected=key: self.set_skin(selected),
                             key == self.skin)
            column.addStretch(1)
            column.addWidget(button)
            cards.addWidget(item, 1)
        layout.addLayout(cards)
        content.insertWidget(0, panel)
        return page

    def page_shell(self, title, subtitle="", actions=None):
        page, content = original_page_shell(self, title, subtitle, actions)
        if getattr(self, "skin", None) == "cinematic" and title == "Ce văd acum?" and actions:
            outer = page.layout()
            outer.setContentsMargins(24, 12, 24, 12)
            top = outer.itemAt(0).layout()
            if top is not None and top.count() >= 3:
                title_item = top.takeAt(0)
                actions_item = top.takeAt(top.count() - 1)
                title_widget, actions_layout = title_item.widget(), actions_item.layout()
                if title_widget is not None and actions_layout is not None:
                    row = QHBoxLayout()
                    row.addWidget(title_widget, 1)
                    row.addLayout(actions_layout)
                    top.insertLayout(0, row)
        return page, content

    window_cls._build_shell = _build_shell
    window_cls.apply_theme = apply_theme
    window_cls.set_skin = set_skin
    window_cls.page_settings = page_settings
    window_cls.page_shell = page_shell
    window_cls._render_today = render_skin_today
    window_cls._cinecalendar_skin_installed = True
