from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import qt_ui as base_ui


_GROUPS = (
    ("ACUM", ("today", "recommendations", "romanian")),
    ("BIBLIOTECĂ", ("romanian_list", "profile", "ratings", "watchlist")),
    ("CALENDAR", ("calendar", "month")),
    ("ISTORIC ȘI DATE", ("history", "metadata_doctor")),
    ("TEST ȘI SISTEM", ("v5_lab", "updates", "settings")),
)

_NAV_LABELS = {
    "romanian_list": "Cronologie filme RO",
    "month": "Filme pe zile",
    "metadata_doctor": "Reparare date",
    "v5_lab": "Test V16 / V5",
}

_NAV_TIPS = {
    "today": "Alegerea principală pentru azi.",
    "recommendations": "Clasamentul personal complet.",
    "romanian": "Recomandări numai din cinema românesc verificat.",
    "romanian_list": "Explorează filmele românești după perioada acțiunii.",
    "profile": "Vezi ce a învățat CineCalendar din ratingurile tale.",
    "ratings": "Biblioteca ta de ratinguri și sincronizarea IMDb.",
    "watchlist": "Filmele salvate și coada personală Următoarele 5.",
    "calendar": "Repere religioase, istorice, civice și sezoniere.",
    "month": "Program de filme pentru orice zi din lună.",
    "history": "Ce ți-a fost recomandat și ce ai făcut cu recomandarea.",
    "metadata_doctor": "Verifică și repară metadatele și posterele.",
    "v5_lab": "Compară controlat V16 cu V5.",
    "updates": "Verifică și instalează actualizările canalului curent.",
    "settings": "Setări rare, catalog, surse și backup.",
}

_GUIDES = {
    "romanian": (
        "Cum folosești pagina",
        "Lista rămâne neschimbată când pleci și revii. «Recalculează» cere explicit o selecție românească nouă.",
    ),
    "romanian_list": (
        "Cronologie de explorare",
        "Aici explorezi cinema românesc după perioada în care se petrece acțiunea. Filtrele și căutarea nu schimbă profilul tău.",
    ),
    "profile": (
        "Ce a învățat motorul",
        "Pagina este în principal de citire: îți arată gusturile și performanța reală deduse din ratinguri. Nu trebuie să reglezi manual profilul.",
    ),
    "ratings": (
        "Biblioteca ta",
        "Caută și filtrează ratingurile aici. Sincronizarea IMDb completează biblioteca; exportul nu modifică datele.",
    ),
    "watchlist": (
        "Coada ta de vizionare",
        "«Următoarele 5» se păstrează când revii pe pagină. Filtrele schimbă doar ordinea afișată; «Recalculează coada» cere o clasare nouă.",
    ),
    "calendar": (
        "Reperele anului",
        "Deschide un reper pentru filme legate de data respectivă. Calendarul explică contextul; nu îți suprascrie gustul personal.",
    ),
    "month": (
        "Programul pe zile",
        "Alege o zi din lună. Rezultatul zilei selectate este păstrat când navighezi și nu se recalculează inutil.",
    ),
    "history": (
        "Istoricul recomandărilor",
        "Folosește filtrele pentru a vedea ce motor a recomandat un film și ce rezultat real a urmat. Dublu-click deschide detaliile.",
    ),
    "metadata_doctor": (
        "Reparare date",
        "În mod normal lucrează automat în fundal. Folosește «Scanează și repară acum» doar când vezi postere sau metadate lipsă.",
    ),
    "updates": (
        "Actualizări sigure",
        "Canalul Alpha este separat de Stable. Actualizarea verifică pachetul și păstrează datele personale în afara bundle-ului.",
    ),
    "settings": (
        "Setări avansate",
        "Pentru utilizarea normală nu trebuie să modifici nimic aici. Schimbă doar tema, sursele, catalogul sau backupul când ai un motiv concret.",
    ),
}


def install_premium_tabs_v5(window_cls) -> None:
    """Final usability layer for the Premium/Alpha navigation and inherited pages."""
    if getattr(window_cls, "_cinecalendar_premium_tabs_v5", False):
        return

    originals = {}
    for key in _GUIDES:
        method = getattr(window_cls, f"page_{key}", None)
        if callable(method):
            originals[key] = method
    original_v5_lab = getattr(window_cls, "page_v5_lab", None)

    def _build_shell(self):
        root = QWidget()
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(250)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 16, 14, 14)
        sv.setSpacing(5)

        brand = QLabel("CineCalendar")
        brand.setObjectName("Brand")
        sv.addWidget(brand)
        sub = QLabel("calendar cinematografic personal")
        sub.setObjectName("Muted")
        sv.addWidget(sub)
        sv.addSpacing(10)

        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_scroll.setStyleSheet("QScrollArea { background: transparent; border: 0; }")
        nav_inner = QWidget()
        nav_inner.setStyleSheet("background: transparent;")
        nv = QVBoxLayout(nav_inner)
        nv.setContentsMargins(0, 0, 4, 0)
        nv.setSpacing(3)

        available = {key: label for key, label in self.NAV}
        self.nav_buttons = {}
        used = set()
        for group_name, keys in _GROUPS:
            present = [key for key in keys if key in available]
            if not present:
                continue
            header = QLabel(group_name)
            header.setObjectName("NavSection")
            nv.addWidget(header)
            for key in present:
                label = _NAV_LABELS.get(key, available[key])
                b = QPushButton(label)
                b.setProperty("nav", True)
                b.setToolTip(_NAV_TIPS.get(key, ""))
                b.clicked.connect(lambda _checked=False, k=key: self.show_page(k))
                nv.addWidget(b)
                self.nav_buttons[key] = b
                used.add(key)
            nv.addSpacing(7)

        leftovers = [(key, label) for key, label in self.NAV if key not in used]
        if leftovers:
            header = QLabel("ALTELE")
            header.setObjectName("NavSection")
            nv.addWidget(header)
            for key, label in leftovers:
                b = QPushButton(_NAV_LABELS.get(key, label))
                b.setProperty("nav", True)
                b.setToolTip(_NAV_TIPS.get(key, ""))
                b.clicked.connect(lambda _checked=False, k=key: self.show_page(k))
                nv.addWidget(b)
                self.nav_buttons[key] = b

        nv.addStretch(1)
        nav_scroll.setWidget(nav_inner)
        sv.addWidget(nav_scroll, 1)

        self.undo_feedback_button = QPushButton("Anulează ultimul feedback")
        self.undo_feedback_button.setEnabled(False)
        self.undo_feedback_button.setToolTip(
            "Anulează exact ultima acțiune de feedback din sesiunea curentă (Ctrl+Z)."
        )
        self.undo_feedback_button.clicked.connect(self.undo_last_feedback)
        sv.addWidget(self.undo_feedback_button)

        self.status = QLabel("Pregătit")
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        sv.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)
        sv.addWidget(self.progress)
        version = QLabel(f"v{base_ui.APP_VERSION}")
        version.setObjectName("Muted")
        sv.addWidget(version)

        h.addWidget(side)
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)

    def page_shell(self, title: str, subtitle: str = "", actions=None):
        """Shared Premium shell used by every tab."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        header = QFrame()
        header.setObjectName("PageHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(22, 18, 22, 18)
        hl.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(4)
        group_name = next(
            (name for name, keys in _GROUPS if self.current_page in keys),
            "CINECALENDAR",
        )
        kicker = QLabel(group_name)
        kicker.setObjectName("Kicker")
        left.addWidget(kicker)
        t = QLabel(title)
        t.setObjectName("PageTitle")
        t.setWordWrap(True)
        left.addWidget(t)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Muted")
            sub.setWordWrap(True)
            left.addWidget(sub)
        hl.addLayout(left, 1)

        if actions:
            action_box = QHBoxLayout()
            action_box.setSpacing(8)
            for text_value, fn, accent in actions:
                button = QPushButton(text_value)
                button.setProperty("accent", bool(accent))
                button.clicked.connect(fn)
                action_box.addWidget(button)
            hl.addLayout(action_box)

        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        content = QVBoxLayout(inner)
        content.setContentsMargins(0, 2, 6, 10)
        content.setSpacing(14)
        content.setAlignment(Qt.AlignTop)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return page, content

    def _page_metrics(self, key: str):
        try:
            with self.db.connect() as con:
                if key == "ratings":
                    row = con.execute(
                        "SELECT COUNT(*),COALESCE(AVG(rating),0),COALESCE(MAX(updated_at),'') FROM ratings"
                    ).fetchone()
                    return [
                        (f"{int(row[0]):,}", "ratinguri"),
                        (f"{float(row[1]):.2f}/10", "media ta"),
                        ((str(row[2])[:10] or "—"), "ultima modificare"),
                    ]
                if key == "profile":
                    row = con.execute(
                        "SELECT COUNT(*),COALESCE(AVG(rating),0) FROM ratings"
                    ).fetchone()
                    version = con.execute(
                        "SELECT value_json FROM user_profile WHERE profile_key='profile_version'"
                    ).fetchone()
                    return [
                        (f"{int(row[0]):,}", "ratinguri analizate"),
                        (f"{float(row[1]):.2f}/10", "media ta"),
                        (("actualizat" if version else "calculat"), "profil gust"),
                    ]
                if key == "watchlist":
                    row = con.execute(
                        "SELECT COUNT(*),COALESCE(MAX(updated_at),'') FROM watchlist WHERE status='want_to_watch'"
                    ).fetchone()
                    pinned = self.db.get_setting("watchlist_pinned_movie_ids", []) or []
                    return [
                        (f"{int(row[0]):,}", "în Watchlist"),
                        (f"{len(pinned):,}", "prioritare"),
                        ((str(row[1])[:10] or "—"), "ultima schimbare"),
                    ]
                if key == "history":
                    shown = con.execute(
                        "SELECT COUNT(*) FROM recommendation_history WHERE action IS NULL"
                    ).fetchone()[0]
                    actions = con.execute(
                        "SELECT COUNT(*) FROM recommendation_history WHERE action IS NOT NULL"
                    ).fetchone()[0]
                    return [
                        (f"{int(shown):,}", "expuneri"),
                        (f"{int(actions):,}", "acțiuni"),
                        ("filtrabil", "istoric"),
                    ]
                if key == "metadata_doctor":
                    row = con.execute(
                        """SELECT COUNT(*),
                                  SUM(CASE WHEN poster_url IS NULL OR poster_url='' THEN 1 ELSE 0 END),
                                  SUM(CASE WHEN overview IS NULL OR overview='' THEN 1 ELSE 0 END)
                           FROM movies"""
                    ).fetchone()
                    return [
                        (f"{int(row[0] or 0):,}", "filme"),
                        (f"{int(row[1] or 0):,}", "fără poster"),
                        (f"{int(row[2] or 0):,}", "fără descriere"),
                    ]
        except Exception:
            pass

        if key == "settings":
            try:
                total, rated, candidates = self.catalog_count()
                return [
                    (f"{int(total):,}", "catalog"),
                    (f"{int(rated):,}", "evaluate"),
                    (f"{int(candidates):,}", "candidați"),
                ]
            except Exception:
                return []
        if key == "calendar":
            try:
                events = self.s.calendar.relevant_events(__import__("datetime").date.today())
                return [
                    (__import__("datetime").date.today().strftime("%d.%m.%Y"), "azi"),
                    (str(len(events)), "repere active"),
                    ("automat", "context"),
                ]
            except Exception:
                return []
        if key == "month":
            today = __import__("datetime").date.today()
            return [
                (today.strftime("%m/%Y"), "luna curentă"),
                ("pe zile", "program"),
                ("cache", "rezultat reutilizat"),
            ]
        if key == "updates":
            return [
                (str(base_ui.APP_VERSION), "versiune"),
                ("Alpha" if "Alpha" in str(base_ui.APP_VERSION) else "Stable", "canal"),
                ("SHA-256", "verificare"),
            ]
        if key == "romanian":
            count = len(getattr(self, "romanian_result", []) or [])
            return [
                (str(count), "în selecția curentă"),
                ("română", "limbă originală"),
                ("explicit", "recalculare"),
            ]
        if key == "romanian_list":
            try:
                total, rated, _ = self.catalog_count()
                return [
                    (f"{int(total):,}", "catalog local"),
                    (f"{int(rated):,}", "evaluate"),
                    ("cronologic", "mod explorare"),
                ]
            except Exception:
                return []
        return []

    def _insert_guide(self, page, key: str, heading: str, text: str):
        scroll = page.findChild(QScrollArea)
        if scroll is None or scroll.widget() is None:
            return
        layout = scroll.widget().layout()
        if layout is None:
            return
        guide = QFrame()
        guide.setObjectName("HeroCard")
        gl = QVBoxLayout(guide)
        gl.setContentsMargins(20, 18, 20, 18)
        gl.setSpacing(10)

        kicker = QLabel("PE SCURT")
        kicker.setObjectName("Kicker")
        gl.addWidget(kicker)

        title = QLabel(heading)
        title.setObjectName("SectionTitle")
        gl.addWidget(title)

        body = QLabel(text)
        body.setObjectName("Muted")
        body.setWordWrap(True)
        gl.addWidget(body)

        metrics = self._page_metrics(key)
        if metrics:
            row = QHBoxLayout()
            row.setSpacing(10)
            for value, caption in metrics[:3]:
                if hasattr(self, "metric_badge"):
                    row.addWidget(self.metric_badge(str(value), str(caption)))
                else:
                    fallback = QLabel(f"{value} • {caption}")
                    fallback.setObjectName("Muted")
                    row.addWidget(fallback)
            row.addStretch(1)
            gl.addLayout(row)

        layout.insertWidget(0, guide)

    def _wrap_page(key, original):
        def wrapped(self):
            page = original(self)
            heading, text = _GUIDES[key]
            _insert_guide(self, page, key, heading, text)
            return page
        return wrapped

    # Replace both shared construction primitives. Every page calls page_shell(),
    # so this changes the actual tab content surface as well as the sidebar.
    window_cls._build_shell = _build_shell
    window_cls.page_shell = page_shell

    for key, original in originals.items():
        setattr(window_cls, f"page_{key}", _wrap_page(key, original))

    if callable(original_v5_lab):
        def page_v5_lab(self):
            page = original_v5_lab(self)
            # Hide the old duplicate model buttons; the new primary switch lives at the top.
            for button in page.findChildren(QPushButton):
                if button.text() in {"Folosește V16", "Folosește V5 20%"}:
                    button.hide()
            scroll = page.findChild(QScrollArea)
            if scroll is None or scroll.widget() is None or scroll.widget().layout() is None:
                return page
            layout = scroll.widget().layout()
            status = self.s.alpha_trial_status()
            active = str(status.get("mode") or "v16")

            box = QFrame()
            box.setObjectName("HeroCard")
            bl = QVBoxLayout(box)
            bl.setContentsMargins(20, 18, 20, 18)
            bl.setSpacing(8)
            kicker = QLabel("MODEL FOLOSIT ACUM")
            kicker.setObjectName("Kicker")
            bl.addWidget(kicker)
            title = QLabel("V5 20%" if active == "v5_20" else "V16")
            title.setObjectName("SectionTitle")
            bl.addWidget(title)
            note = QLabel(
                "Comutarea schimbă doar modelul activ pentru următoarea afișare. "
                "Comparația controlată păstrează aceeași pereche V16/V5 până când ceri o recalculare nouă."
            )
            note.setObjectName("Muted")
            note.setWordWrap(True)
            bl.addWidget(note)

            row = QHBoxLayout()
            v16 = QPushButton("Folosește V16")
            v16.setProperty("accent", active == "v16")
            v16.clicked.connect(lambda: self.set_v5_trial_mode("v16"))
            row.addWidget(v16)
            v5 = QPushButton("Folosește V5 20%")
            v5.setProperty("accent", active == "v5_20")
            v5.setEnabled(bool(status.get("eligible")))
            v5.clicked.connect(lambda: self.set_v5_trial_mode("v5_20"))
            row.addWidget(v5)
            row.addStretch(1)
            bl.addLayout(row)
            layout.insertWidget(0, box)
            return page

        window_cls.page_v5_lab = page_v5_lab

    old_apply_theme = window_cls.apply_theme

    def apply_theme(self):
        old_apply_theme(self)
        extra = """
            QLabel#NavSection {
                color: rgba(165,174,192,0.78);
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 1.1px;
                padding: 8px 10px 3px 10px;
            }
            QFrame#PageHeader {
                background: rgba(255,255,255,0.025);
                border: 1px solid rgba(170,178,192,0.18);
                border-radius: 18px;
            }
            QFrame#GuideCard {
                background: rgba(215,170,85,0.055);
                border: 1px solid rgba(215,170,85,0.22);
                border-radius: 14px;
            }
        """
        self.setStyleSheet(extra)

    # The application stylesheet lives on QApplication, while setStyleSheet on the window is
    # additive and keeps these two small navigation/guide styles local to this surface.
    window_cls.apply_theme = apply_theme
    window_cls._cinecalendar_premium_tabs_v5 = True
