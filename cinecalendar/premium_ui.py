from __future__ import annotations

import sys
from datetime import date
from typing import Iterable

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import __version__ as APP_VERSION
from .candidate_metadata_v48 import (
    CandidateMetadataPreflight,
    MAX_PREFLIGHT_TITLES,
    PREFLIGHT_POOL_SIZE,
    coverage_report,
    metadata_snapshot,
    missing_metadata_labels,
    reset_metadata_cache,
)
from .feedback import daily_contextual_feedback
from .open_metadata import OpenMovieMetadataProvider
from .profile import get_profile, top_profile_features
from .qt_ui import WorkerThread
from .qt_ui_v2 import DecisionWindow
from .recommendation import Recommendation
from .learning_insight_v43 import comparison_reason
from .metadata_provenance import metadata_sources_for_movie
from .reliability_gate_v412 import MIN_MEASURED_OUTCOMES, RecommendationReliabilityGate
from .tmdb import TmdbProvider


class ResponsiveRecommendationGrid(QWidget):
    """Keep cards readable without making the page scroll sideways."""

    def __init__(self, cards: list[QWidget], breakpoint: int = 1120, parent=None):
        super().__init__(parent)
        self._cards = cards
        self._breakpoint = int(breakpoint)
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(14)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._relayout(2)

    def _relayout(self, columns: int) -> None:
        columns = max(1, min(2, int(columns)))
        if columns == self._columns:
            return
        for card in self._cards:
            self._grid.removeWidget(card)
        self._columns = columns
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // columns, index % columns)
        for column in range(2):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:
        self._relayout(2 if event.size().width() >= self._breakpoint else 1)
        super().resizeEvent(event)


class MovieDetailDialog(QDialog):
    def __init__(self, rec: Recommendation, owner: "PremiumDecisionWindow"):
        super().__init__(owner)
        self.rec = rec
        self.owner = owner
        self.setWindowTitle(rec.movie.title)
        self.resize(1040, 720)
        self.setMinimumSize(860, 620)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        body = QVBoxLayout(inner)
        body.setContentsMargins(30, 28, 30, 28)
        body.setSpacing(20)

        top = QFrame()
        top.setObjectName("DetailHero")
        top_l = QHBoxLayout(top)
        top_l.setContentsMargins(24, 24, 24, 24)
        top_l.setSpacing(26)

        poster = owner.poster_label(244, 356)
        top_l.addWidget(poster, 0, Qt.AlignTop)
        if rec.movie.poster_url:
            owner.load_poster_async(poster, rec.movie.poster_url, rec.movie.imdb_id or str(rec.movie.id))

        info = QVBoxLayout()
        info.setSpacing(10)
        kicker = QLabel("CINECALENDAR • DETALII")
        kicker.setObjectName("Kicker")
        info.addWidget(kicker)
        title = QLabel(rec.movie.title + (f"  ({rec.movie.year})" if rec.movie.year else ""))
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        info.addWidget(title)

        score_row = QHBoxLayout()
        personal = owner.score_badge(rec.score.predicted_rating, "pentru tine")
        score_row.addWidget(personal)
        confidence = owner.metric_badge(f"{round(rec.score.confidence*100)}%", "dovezi personale")
        score_row.addWidget(confidence)
        if rec.movie.imdb_rating is not None:
            score_row.addWidget(owner.metric_badge(f"{rec.movie.imdb_rating:.1f}", "IMDb"))
        score_row.addStretch(1)
        info.addLayout(score_row)
        info.addWidget(owner.reliability_widget(rec))

        chips = QHBoxLayout()
        for text in owner.movie_chips(rec.movie, limit=6):
            chips.addWidget(owner.pill(text))
        chips.addStretch(1)
        info.addLayout(chips)

        overview = QLabel(owner.overview_text(rec.movie, long=True))
        overview.setObjectName("Overview")
        overview.setWordWrap(True)
        overview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(overview)
        sources = metadata_sources_for_movie(owner.db, int(rec.movie.id)) if rec.movie.id is not None else {}
        source_text = owner.metadata_source_text(sources)
        if source_text:
            source = QLabel(source_text)
            source.setObjectName("Muted")
            source.setWordWrap(True)
            info.addWidget(source)
        info.addStretch(1)

        actions = QHBoxLayout()
        choose = QPushButton("Aleg filmul")
        choose.setProperty("accent", True)
        choose.clicked.connect(lambda: owner.choose_decision(rec.movie.id))
        actions.addWidget(choose)
        watch = QPushButton("Vreau să-l văd")
        watch.clicked.connect(lambda: owner.feedback(rec.movie.id, "want_to_watch"))
        actions.addWidget(watch)
        if rec.movie.imdb_id:
            imdb = QPushButton("Deschide IMDb")
            imdb.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{rec.movie.imdb_id}/")))
            actions.addWidget(imdb)
        actions.addStretch(1)
        info.addLayout(actions)
        top_l.addLayout(info, 1)
        body.addWidget(top)

        why = QFrame()
        why.setObjectName("PremiumCard")
        wl = QVBoxLayout(why)
        wl.setContentsMargins(22, 20, 22, 20)
        wh = QLabel("De ce ți se potrivește")
        wh.setObjectName("SectionTitle")
        wl.addWidget(wh)
        concise = QLabel(owner.human_reason(rec))
        concise.setWordWrap(True)
        concise.setObjectName("BodyStrong")
        wl.addWidget(concise)
        exact = QLabel(rec.score.personal_reason)
        exact.setWordWrap(True)
        exact.setObjectName("Muted")
        exact.setTextInteractionFlags(Qt.TextSelectableByMouse)
        wl.addWidget(exact)
        if rec.score.calendar_reason:
            calendar = QLabel("Contextul perioadei: " + rec.score.calendar_reason)
            calendar.setWordWrap(True)
            calendar.setObjectName("Muted")
            wl.addWidget(calendar)
        body.addWidget(why)

        if str(getattr(rec.score, "why_not", "") or "").strip():
            caution = QFrame()
            caution.setObjectName("PremiumCard")
            cl = QVBoxLayout(caution)
            cl.setContentsMargins(22, 18, 22, 18)
            ch = QLabel("Compromisuri / de ce nu e o alegere perfectă")
            ch.setObjectName("SectionTitle")
            cl.addWidget(ch)
            ct = QLabel(str(rec.score.why_not))
            ct.setObjectName("Muted")
            ct.setWordWrap(True)
            cl.addWidget(ct)
            body.addWidget(caution)

        factors = dict(getattr(rec.score, "score_factors", {}) or {})
        if factors:
            factor_box = QFrame()
            factor_box.setObjectName("PremiumCard")
            fl = QVBoxLayout(factor_box)
            fl.setContentsMargins(22, 18, 22, 18)
            fh = QLabel("Scor explicabil")
            fh.setObjectName("SectionTitle")
            fl.addWidget(fh)
            for name, value in factors.items():
                row = QHBoxLayout()
                label = QLabel(str(name).capitalize())
                label.setObjectName("BodyStrong")
                row.addWidget(label, 1)
                number = QLabel(f"{float(value):+.3f}")
                number.setObjectName("SignalPositive" if float(value) >= 0 else "SignalNegative")
                row.addWidget(number)
                fl.addLayout(row)
            body.addWidget(factor_box)

        signals = QFrame()
        signals.setObjectName("PremiumCard")
        sl = QVBoxLayout(signals)
        sl.setContentsMargins(22, 20, 22, 20)
        sh = QLabel("Cum a ajuns aici")
        sh.setObjectName("SectionTitle")
        sl.addWidget(sh)
        for name, pts, reason in rec.score.contributions[:8]:
            row = QHBoxLayout()
            n = QLabel(name)
            n.setObjectName("BodyStrong")
            row.addWidget(n, 1)
            val = QLabel(f"{pts:+.1f}")
            val.setObjectName("SignalPositive" if pts >= 0 else "SignalNegative")
            row.addWidget(val)
            sl.addLayout(row)
            if reason:
                r = QLabel(reason)
                r.setWordWrap(True)
                r.setObjectName("Muted")
                sl.addWidget(r)
        body.addWidget(signals)
        body.addStretch(1)

        scroll.setWidget(inner)
        root.addWidget(scroll)


class PremiumDecisionWindow(DecisionWindow):
    """Cinematic, non-blocking UI built on the proven rating-first engine."""

    def __init__(self, service):
        self.today_worker: WorkerThread | None = None
        self.browse_worker: WorkerThread | None = None
        self.metadata_worker: WorkerThread | None = None
        self.metadata_attempted: set[int] = set()
        self.today_content = None
        self.browse_content = None
        self.today_result: tuple[Recommendation | None, list[Recommendation]] | None = None
        self.browse_result: list[Recommendation] = []
        self.browse_generation = 0
        self.recommendation_metadata_report: dict = {"state": "idle"}
        super().__init__(service)
        engine_identity = str((getattr(self.s, "production_stack", {}) or {}).get("recommendation_engine_identity") or "")
        self.reliability_gate = RecommendationReliabilityGate(self.db, engine_identity)
        self.setWindowTitle(f"CineCalendar {APP_VERSION} — Premium")

    # ---------- premium visual language ----------
    def apply_theme(self):
        dark = self.theme != "light"
        if dark:
            bg, surface, card, card2 = "#090A0D", "#0F1116", "#151820", "#1B1F29"
            text, muted, border = "#F7F7F4", "#9CA4B3", "#282E3A"
            accent, accent2, good, bad = "#D7AA55", "#7EA2FF", "#6ED6A0", "#FF7E87"
        else:
            bg, surface, card, card2 = "#F4F2ED", "#FAF9F6", "#FFFFFF", "#F0EEE9"
            text, muted, border = "#17181C", "#687180", "#DFDCD4"
            accent, accent2, good, bad = "#9A6B18", "#315FD6", "#1B8751", "#C74650"
        QApplication.instance().setStyleSheet(f"""
            QWidget {{ background:{bg}; color:{text}; font-family:'Segoe UI'; font-size:14px; }}
            QMainWindow, QScrollArea, QScrollArea>QWidget>QWidget {{ background:{bg}; }}
            QFrame#Sidebar {{ background:{surface}; border-right:1px solid {border}; }}
            QFrame#PremiumCard, QFrame#Card {{ background:{card}; border:1px solid {border}; border-radius:18px; }}
            QFrame#HeroCard {{
                background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {card2},stop:.58 {card},stop:1 {surface});
                border:1px solid {border}; border-radius:24px;
            }}
            QFrame#DetailHero {{
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {card2},stop:1 {card});
                border:1px solid {border}; border-radius:22px;
            }}
            QLabel#Brand {{ font-size:25px; font-weight:800; letter-spacing:.4px; color:{accent}; }}
            QLabel#PageTitle {{ font-size:34px; font-weight:800; }}
            QLabel#HeroTitle {{ font-size:34px; font-weight:800; }}
            QLabel#SectionTitle {{ font-size:20px; font-weight:750; }}
            QLabel#CardTitle {{ font-size:17px; font-weight:750; }}
            QLabel#Kicker {{ color:{accent}; font-size:12px; font-weight:800; letter-spacing:1.2px; }}
            QLabel#Muted {{ color:{muted}; }}
            QLabel#Overview {{ color:{text}; font-size:15px; line-height:1.4; }}
            QLabel#BodyStrong {{ font-size:15px; font-weight:600; }}
            QLabel#Score, QLabel#ScoreLarge {{ color:{good}; font-weight:800; }}
            QLabel#ScoreLarge {{ font-size:26px; }}
            QLabel#SignalPositive {{ color:{good}; font-weight:800; }}
            QLabel#SignalNegative {{ color:{bad}; font-weight:800; }}
            QFrame#ReliabilityGood {{ background:rgba(55,175,112,.10); border:1px solid {good}; border-radius:11px; }}
            QFrame#ReliabilityCaution {{ background:rgba(215,170,85,.09); border:1px solid {accent}; border-radius:11px; }}
            QFrame#ReliabilityBad {{ background:rgba(255,126,135,.09); border:1px solid {bad}; border-radius:11px; }}
            QLabel#Pill {{ background:{card2}; color:{muted}; border:1px solid {border}; border-radius:10px; padding:5px 9px; }}
            QLabel#ScoreBadge {{ background:{accent}; color:#101114; border-radius:38px; font-size:20px; font-weight:900; }}
            QLabel#MetricValue {{ color:{accent2}; font-size:22px; font-weight:850; }}
            QPushButton {{ background:{card2}; border:1px solid {border}; border-radius:11px; padding:10px 14px; font-weight:600; }}
            QPushButton:hover {{ border-color:{accent}; background:{card}; }}
            QPushButton[accent='true'] {{ background:{accent}; color:#111217; border-color:{accent}; font-weight:800; }}
            QPushButton[nav='true'] {{ text-align:left; padding:12px 15px; background:transparent; border:0; color:{muted}; }}
            QPushButton[nav='true']:hover {{ background:{card2}; color:{text}; }}
            QPushButton[navActive='true'] {{ text-align:left; padding:12px 15px; background:{card2}; border:1px solid {border}; color:{text}; font-weight:750; }}
            QLineEdit, QSpinBox, QComboBox {{ background:{card2}; border:1px solid {border}; border-radius:10px; padding:9px; }}
            QProgressBar {{ border:1px solid {border}; border-radius:7px; background:{card2}; text-align:center; min-height:12px; }}
            QProgressBar::chunk {{ background:{accent2}; border-radius:6px; }}
            QScrollBar:vertical {{ background:transparent; width:12px; margin:2px; }}
            QScrollBar::handle:vertical {{ background:{border}; min-height:34px; border-radius:5px; }}
            QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
            QScrollBar::handle:horizontal {{ background:{border}; min-width:48px; border-radius:4px; }}
        """)

    def pill(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Pill")
        label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        return label

    def poster_label(self, width: int, height: int) -> QLabel:
        p = QLabel("Imagine\nîn curs de încărcare")
        p.setAlignment(Qt.AlignCenter)
        p.setObjectName("Muted")
        p.setFixedSize(width, height)
        p.setStyleSheet("border-radius:16px; border:1px solid rgba(128,138,155,.28); background:rgba(255,255,255,.025);")
        return p

    def score_badge(self, value: float, caption: str = "") -> QWidget:
        wrap = QWidget()
        l = QVBoxLayout(wrap)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(4)
        score = QLabel(f"{value:.1f}")
        score.setObjectName("ScoreBadge")
        score.setAlignment(Qt.AlignCenter)
        score.setFixedSize(76, 76)
        l.addWidget(score, alignment=Qt.AlignCenter)
        if caption:
            c = QLabel(caption)
            c.setObjectName("Muted")
            c.setAlignment(Qt.AlignCenter)
            l.addWidget(c)
        return wrap

    def metric_badge(self, value: str, caption: str) -> QWidget:
        wrap = QWidget()
        l = QVBoxLayout(wrap)
        l.setContentsMargins(10, 2, 10, 2)
        l.setSpacing(1)
        v = QLabel(value)
        v.setObjectName("MetricValue")
        v.setAlignment(Qt.AlignCenter)
        c = QLabel(caption)
        c.setObjectName("Muted")
        c.setAlignment(Qt.AlignCenter)
        l.addWidget(v)
        l.addWidget(c)
        return wrap

    def reliability_widget(self, rec: Recommendation, *, compact: bool = False) -> QFrame:
        verdict = self.reliability_gate.evaluate(rec)
        box = QFrame()
        if verdict.status == "verified":
            box.setObjectName("ReliabilityGood")
        elif verdict.status == "reject":
            box.setObjectName("ReliabilityBad")
        else:
            box.setObjectName("ReliabilityCaution")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10 if compact else 13, 7 if compact else 9, 10 if compact else 13, 7 if compact else 9)
        layout.setSpacing(2)
        title = QLabel(verdict.label)
        title.setObjectName("BodyStrong")
        layout.addWidget(title)
        interval_kind = "interval măsurat" if verdict.empirical_interval else "interval conservator"
        interval = QLabel(f"Estimare {rec.score.predicted_rating:.1f}/10 • {interval_kind} {verdict.interval_low:.1f}–{verdict.interval_high:.1f}")
        interval.setObjectName("Muted")
        interval.setWordWrap(True)
        layout.addWidget(interval)
        if not compact:
            reason = QLabel(verdict.reason)
            reason.setObjectName("Muted")
            reason.setWordWrap(True)
            layout.addWidget(reason)
        return box

    @staticmethod
    def runtime_text(minutes: int | None) -> str:
        if not minutes:
            return ""
        h, m = divmod(int(minutes), 60)
        return f"{h} h {m:02d} min" if h else f"{m} min"

    def movie_chips(self, movie, limit: int = 7) -> list[str]:
        out = []
        if movie.year:
            out.append(str(movie.year))
        if movie.runtime_min:
            out.append(self.runtime_text(movie.runtime_min))
        out.extend(movie.genres[:4])
        if movie.countries:
            out.append(self.localized_country(movie.countries[0]))
        return out[:limit]

    @staticmethod
    def localized_country(country: str) -> str:
        names = {
            "United States of America": "SUA", "United States": "SUA",
            "United Kingdom": "Regatul Unit", "Romania": "România",
            "Germany": "Germania", "France": "Franța", "Italy": "Italia",
            "Spain": "Spania", "Japan": "Japonia", "South Korea": "Coreea de Sud",
        }
        return names.get(str(country or "").strip(), str(country or "").strip())

    @staticmethod
    def metadata_source_text(sources: dict[str, str]) -> str:
        labels = {
            "tmdb-ro": "TMDb (română)", "tmdb-en": "TMDb (engleză)", "tmdb": "TMDb",
            "wikimedia": "Wikidata/Wikipedia", "wikidata": "Wikidata/Wikipedia",
            "imdb": "IMDb",
        }
        parts = []
        for field, caption in (("overview", "descriere"), ("poster_url", "poster")):
            provider = str(sources.get(field, "") or "")
            label = labels.get(provider, provider)
            if label:
                parts.append(f"{caption}: {label}")
        return "Surse • " + " • ".join(parts) + " • cache local" if parts else ""

    def overview_text(self, movie, long: bool = False) -> str:
        text = (movie.overview or "").strip()
        if text:
            if long or len(text) <= 360:
                return text
            return text[:357].rsplit(" ", 1)[0] + "…"
        return "Descrierea și imaginea se completează automat din surse deschise. Nu trebuie să adaugi nimic manual."

    def apply_contextual_feedback(self, movie_id: int, kind: str) -> None:
        # The shared feedback method excludes the title only after persistence succeeds.
        self.feedback(int(movie_id), str(kind))

    def active_contextual_feedback(self) -> tuple[tuple[str, int], ...]:
        # Rebuild from today's persisted events so closing/reopening the app cannot forget the
        # current viewing context. The helper resets automatically on the next local date.
        return daily_contextual_feedback(self.db)

    def contextual_feedback_menu(self, movie_id: int, button: QPushButton) -> None:
        menu = QMenu(self)
        choices = (
            ("Nu acum", "not_now"),
            ("Prea lung pentru moment", "too_long"),
            ("Nu am chef de genul ăsta acum", "mood_mismatch"),
            ("Prea similar cu ce am văzut/recomandat", "too_similar"),
            ("Ascunde doar filmul (nu schimbă gustul)", "not_interested"),
            ("Nu-mi recomanda similare", "never_similar"),
        )
        for label, kind in choices:
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, mid=int(movie_id), k=kind: self.apply_contextual_feedback(mid, k)
            )
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def human_reason(self, rec: Recommendation) -> str:
        m, s = rec.movie, rec.score
        # Later engines already compute evidence-rich explanations, including concrete
        # rated-film examples from ALS when available. Prefer that over a generic UI summary.
        detailed = str(getattr(s, "personal_reason", "") or "").strip()
        if detailed:
            return detailed

        pieces = []
        if m.genres:
            pieces.append("mixul " + " / ".join(m.genres[:2]))
        if m.directors:
            pieces.append("regia lui " + m.directors[0])
        if s.confidence >= .70:
            lead = "Potrivire puternică cu istoricul tău de ratinguri"
        elif s.confidence >= .52:
            lead = "Potrivire bună, cu suficiente semnale din gustul tău"
        else:
            lead = "O alegere mai exploratorie, cu dovezi personale moderate"
        if pieces:
            return f"{lead}, în special prin {', '.join(pieces)}. Estimare personală: {s.predicted_rating:.1f}/10."
        return f"{lead}. Estimarea personală este {s.predicted_rating:.1f}/10."

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child is not None:
                PremiumDecisionWindow._clear_layout(child)

    def loading_panel(self, title: str, subtitle: str) -> QFrame:
        box = QFrame()
        box.setObjectName("HeroCard")
        l = QVBoxLayout(box)
        l.setContentsMargins(26, 26, 26, 26)
        l.setSpacing(12)
        h = QLabel(title)
        h.setObjectName("SectionTitle")
        l.addWidget(h)
        s = QLabel(subtitle)
        s.setObjectName("Muted")
        s.setWordWrap(True)
        l.addWidget(s)
        bar = QProgressBar()
        bar.setRange(0, 0)
        l.addWidget(bar)
        return box

    # ---------- non-blocking home ----------
    def page_today(self):
        page, content = self.page_shell(
            "Ce văd acum?",
            "O singură alegere bine argumentată, construită din ratingurile tale. Fără listă infinită.",
        )
        self.today_content = content
        _total, rated, cand = self.catalog_count()
        if cand <= 0:
            box = QFrame(); box.setObjectName("HeroCard")
            l = QVBoxLayout(box); l.setContentsMargins(26,26,26,26); l.setSpacing(12)
            h = QLabel("Catalogul personal nu este încă pregătit"); h.setObjectName("SectionTitle"); l.addWidget(h)
            d = QLabel(f"Am {rated:,} ratinguri de învățat. Catalogul IMDb și regizorii se descarcă și se leagă automat — nu introduci filme manual.")
            d.setObjectName("Muted"); d.setWordWrap(True); l.addWidget(d)
            b = QPushButton("Pregătește automat catalogul"); b.setProperty("accent",True); b.clicked.connect(lambda:self.bootstrap_catalog(False)); l.addWidget(b, alignment=Qt.AlignLeft)
            content.addWidget(box); content.addStretch(1); return page
        content.addWidget(self.loading_panel("Îți aleg filmul…", "Analizez profilul, istoricul, calitatea titlurilor și contextul zilei. Fereastra rămâne utilizabilă în timp ce motorul lucrează."))
        content.addStretch(1)
        QTimer.singleShot(0, self._load_today_async)
        return page

    def _load_today_async(self):
        if self.today_worker and self.today_worker.isRunning():
            return
        self.set_status("Calculez alegerea zilei…", True)
        contextual_feedback = self.active_contextual_feedback()
        contextual_exclusions = {movie_id for _kind, movie_id in contextual_feedback}
        exclude_ids = set(self.session_skips) | contextual_exclusions
        worker = WorkerThread(
            lambda progress: self.s.recommender.decision_pick(
                date.today(),
                exclude_ids,
                self.decision_mode,
                contextual_feedback=contextual_feedback,
            ),
            self,
        )
        self.today_worker = worker
        def success(result):
            self.today_worker = None
            self.set_status("Alegerea este gata.", False)
            self.today_result = result
            if self.current_page == "today":
                self._render_today(*result)
                recs = ([result[0]] if result[0] else []) + list(result[1] or [])
                self._ensure_metadata(recs, "today")
        def failure(message):
            self.today_worker = None
            self.set_status("Recomandarea a eșuat.", False)
            if self.current_page == "today" and self.today_content is not None:
                self._clear_layout(self.today_content)
                x = QLabel("Nu am putut calcula recomandarea: " + message); x.setWordWrap(True); self.today_content.addWidget(x)
        worker.success.connect(success); worker.failure.connect(failure); worker.start()

    def _render_today(self, primary: Recommendation | None, backups: list[Recommendation]):
        if self.today_content is None:
            return
        self._clear_layout(self.today_content)
        if primary is None:
            x = QLabel("Nu am găsit momentan un titlu suficient de bun după filtrele tale.")
            x.setObjectName("Muted"); self.today_content.addWidget(x); return
        self.reliability_gate.refresh()
        self._schedule_retrieval_shadow([primary, *list(backups or [])[:2]], "decision")
        self.record_once([primary], date.today(), "decision")
        self.today_content.addWidget(self.decision_hero(primary))

        mode = QFrame(); mode.setObjectName("PremiumCard")
        ml = QHBoxLayout(mode); ml.setContentsMargins(16,12,16,12); ml.setSpacing(8)
        label = QLabel("Reglaj rapid")
        label.setObjectName("BodyStrong"); ml.addWidget(label)
        for text, value in (("Echilibrat","decide"),("Mai sigur","safe"),("Surprinde-mă","surprise"),("Mai scurt","short")):
            b=QPushButton(text)
            if value==self.decision_mode: b.setProperty("accent",True)
            b.clicked.connect(lambda _,v=value:self.set_decision_mode(v)); ml.addWidget(b)

        ml.addWidget(QLabel("Timp"))
        runtime = QComboBox()
        for text, key in (("Orice","all"),("≤60 min","60"),("≤90 min","90"),("≤120 min","120"),("180+ min","180plus")):
            runtime.addItem(text,key)
        wanted_runtime = str(self.db.get_setting("chooser_runtime_bucket","all") or "all")
        runtime.setCurrentIndex(max(0, runtime.findData(wanted_runtime)))
        ml.addWidget(runtime)

        ml.addWidget(QLabel("Dispoziție"))
        mood = QComboBox()
        for text, key in (("Neutru","neutral"),("Lejer","light"),("Intens","intense"),("Contemplativ","contemplative"),("Ușor de urmărit","easy")):
            mood.addItem(text,key)
        wanted_mood = str(self.db.get_setting("chooser_mood","neutral") or "neutral")
        mood.setCurrentIndex(max(0, mood.findData(wanted_mood)))
        ml.addWidget(mood)
        ml.addStretch(1)

        def chooser_changed():
            self.db.set_setting("chooser_runtime_bucket", str(runtime.currentData() or "all"))
            self.db.set_setting("chooser_mood", str(mood.currentData() or "neutral"))
            self.show_page("today")

        runtime.currentIndexChanged.connect(lambda _i: chooser_changed())
        mood.currentIndexChanged.connect(lambda _i: chooser_changed())
        self.today_content.addWidget(mode)

        if backups:
            h=QLabel("Alternative bune, dacă prima alegere nu te prinde")
            h.setObjectName("SectionTitle"); self.today_content.addWidget(h)
            grid=QGridLayout(); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(14)
            for i, rec in enumerate(backups[:2]): grid.addWidget(self.backup_card(rec, primary),0,i)
            wrap=QFrame(); wrap.setLayout(grid); self.today_content.addWidget(wrap)
        self.today_content.addStretch(1)

    def decision_hero(self, rec: Recommendation):
        m, s = rec.movie, rec.score
        box = QFrame(); box.setObjectName("HeroCard")
        main = QHBoxLayout(box); main.setContentsMargins(26,26,26,26); main.setSpacing(28)
        poster = self.poster_label(222, 326)
        main.addWidget(poster, 0, Qt.AlignTop)
        if m.poster_url: self.load_poster_async(poster,m.poster_url,m.imdb_id or str(m.id))

        right=QVBoxLayout(); right.setSpacing(11)
        kicker=QLabel("ALEGEREA ZILEI"); kicker.setObjectName("Kicker"); right.addWidget(kicker)
        title=QLabel(m.title + (f"  ({m.year})" if m.year else "")); title.setObjectName("HeroTitle"); title.setWordWrap(True); right.addWidget(title)

        metric=QHBoxLayout(); metric.addWidget(self.score_badge(s.predicted_rating,"pentru tine"))
        metric.addWidget(self.metric_badge(f"{round(s.confidence*100)}%","dovezi personale"))
        if m.imdb_rating is not None: metric.addWidget(self.metric_badge(f"{m.imdb_rating:.1f}","IMDb"))
        metric.addStretch(1); right.addLayout(metric)
        right.addWidget(self.reliability_widget(rec))

        chips=QHBoxLayout()
        for text in self.movie_chips(m): chips.addWidget(self.pill(text))
        chips.addStretch(1); right.addLayout(chips)

        overview=QLabel(self.overview_text(m)); overview.setObjectName("Overview"); overview.setWordWrap(True); overview.setMaximumHeight(118); right.addWidget(overview)
        reason=QLabel(self.human_reason(rec)); reason.setWordWrap(True); reason.setObjectName("BodyStrong"); right.addWidget(reason)
        if str(getattr(s, "why_not", "") or "").strip():
            caution=QLabel("Compromisuri: "+str(s.why_not)); caution.setObjectName("Muted"); caution.setWordWrap(True); right.addWidget(caution)
        if s.calendar_reason and s.calendar >= .48:
            now=QLabel("De ce acum: "+s.calendar_reason); now.setObjectName("Muted"); now.setWordWrap(True); right.addWidget(now)

        actions=QHBoxLayout()
        choose=QPushButton("Aleg filmul ăsta"); choose.setProperty("accent",True); choose.clicked.connect(lambda _,mid=m.id:self.choose_decision(mid)); actions.addWidget(choose)
        detail=QPushButton("Detalii"); detail.clicked.connect(lambda _,r=rec:self.open_details(r)); actions.addWidget(detail)
        why_no=QPushButton("Nu acum / motiv")
        why_no.clicked.connect(lambda _checked=False, mid=m.id, b=why_no: self.contextual_feedback_menu(mid, b))
        actions.addWidget(why_no)
        other=QPushButton("Alt film"); other.clicked.connect(lambda _,mid=m.id:self.skip_decision(mid)); actions.addWidget(other)
        actions.addStretch(1); right.addLayout(actions)
        main.addLayout(right,1)
        return box

    def backup_card(self, rec: Recommendation, primary: Recommendation | None = None):
        m,s=rec.movie,rec.score
        box=QFrame(); box.setObjectName("PremiumCard"); box.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Minimum)
        main=QHBoxLayout(box); main.setContentsMargins(16,16,16,16); main.setSpacing(14)
        poster=self.poster_label(88,132); main.addWidget(poster,0,Qt.AlignTop)
        if m.poster_url:self.load_poster_async(poster,m.poster_url,m.imdb_id or str(m.id))
        l=QVBoxLayout(); t=QLabel(m.title+(f" ({m.year})" if m.year else "")); t.setObjectName("CardTitle"); t.setWordWrap(True); l.addWidget(t)
        p=QLabel(f"{s.predicted_rating:.1f}/10 pentru tine • {round(s.confidence*100)}% dovezi personale"); p.setObjectName("Score"); l.addWidget(p)
        l.addWidget(self.reliability_widget(rec,compact=True))
        meta=" • ".join(self.movie_chips(m,4)); x=QLabel(meta); x.setObjectName("Muted"); x.setWordWrap(True); l.addWidget(x)
        if primary is not None:
            compare=QLabel("Față de alegerea #1: "+comparison_reason(primary, rec))
            compare.setObjectName("Muted"); compare.setWordWrap(True); l.addWidget(compare)
        row=QHBoxLayout(); d=QPushButton("Detalii"); d.clicked.connect(lambda _,r=rec:self.open_details(r)); row.addWidget(d)
        c=QPushButton("Aleg"); c.clicked.connect(lambda _,mid=m.id:self.choose_decision(mid)); row.addWidget(c); row.addStretch(1); l.addLayout(row)
        main.addLayout(l,1); return box

    # ---------- premium browse ----------
    def page_recommendations(self):
        page, content = self.page_shell(
            "Recomandări pentru tine",
            "Selecție personală, nu top IMDb. Gustul tău conduce scorul; calendarul doar rafinează.",
            [("Recalculează", lambda:self.show_page("recommendations"), True)],
        )
        self.browse_content=content
        if self.catalog_count()[2] <= 0:
            x=QLabel("Catalogul nu este încă pregătit."); x.setObjectName("Muted"); content.addWidget(x); return page
        content.addWidget(self.loading_panel("Construiesc selecția…","Caut printre filme nevăzute și evit titlurile deja evaluate, respinse sau repetate prea des."))
        content.addStretch(1)
        QTimer.singleShot(0,self._load_browse_async)
        return page

    def _load_browse_async(self):
        if self.browse_worker and self.browse_worker.isRunning(): return
        if self.metadata_worker and self.metadata_worker.isRunning():
            self.set_status("Finalizez verificarea datelor listei curente…",True)
            QTimer.singleShot(250,self._load_browse_async)
            return
        if self.current_page != "recommendations": return
        self.browse_generation += 1
        self.set_status("Calculez recomandările…",True)
        worker=WorkerThread(lambda progress:self.s.recommender.recommend(date.today(),PREFLIGHT_POOL_SIZE,exclude_ids=set(self.session_skips),record=False,slot="browse",candidate_limit=45000,mode="decide"),self)
        self.browse_worker=worker
        def success(recs):
            self.browse_worker=None; self.browse_result=list(recs); self.set_status("Recomandările sunt gata.",False)
            coverage=coverage_report(self.browse_result[:12])
            self.recommendation_metadata_report={
                "state":"checking", "before":coverage, "after":coverage,
                "attempted":0, "changed_titles":0, "ranking_fields_added":{},
                "ranking_change":False, "reranked":False, "io_limit":MAX_PREFLIGHT_TITLES,
                "pool_size":len(self.browse_result),
            }
            if self.current_page=="recommendations":
                # Keep the loading panel until preflight finishes. Rendering this provisional
                # list and then the reranked list would persist two exposure sets for one action.
                self._ensure_metadata(self.browse_result,"recommendations")
        def failure(message):
            self.browse_worker=None; self.set_status("Recomandările au eșuat.",False)
            if self.current_page=="recommendations" and self.browse_content is not None:
                self._clear_layout(self.browse_content); x=QLabel(message); x.setWordWrap(True); self.browse_content.addWidget(x)
        worker.success.connect(success); worker.failure.connect(failure); worker.start()

    def _render_browse(self,recs:list[Recommendation]):
        if self.browse_content is None:return
        self._clear_layout(self.browse_content)
        if not recs:
            x=QLabel("Nu am găsit recomandări eligibile."); x.setObjectName("Muted"); self.browse_content.addWidget(x); return
        self.reliability_gate.refresh()
        self._schedule_retrieval_shadow(list(recs)[:12], "browse")
        self.browse_content.addWidget(self.recommendation_protection_card())
        self.browse_content.addWidget(self.recommendation_metadata_card())
        intro=QFrame(); intro.setObjectName("PremiumCard"); il=QHBoxLayout(intro); il.setContentsMargins(18,14,18,14)
        txt=QLabel("Scorul personal estimat este principalul criteriu. IMDb, noutatea și perioada curentă sunt filtre secundare."); txt.setObjectName("Muted"); txt.setWordWrap(True); il.addWidget(txt,1)
        self.browse_content.addWidget(intro)
        cards=[self.compact_recommendation_card(rec,i+1) for i,rec in enumerate(recs)]
        self.browse_content.addWidget(ResponsiveRecommendationGrid(cards))
        self.browse_content.addStretch(1)

    def recommendation_protection_card(self):
        status = self.s.quality_manager.status()
        live = status.get("live_guard") or {}
        policy = status.get("recalibration_policy") or {}
        stack = getattr(self.s, "production_stack", {}) or {}
        als = float(stack.get("als_weight", .70) or .70)
        content_weight = float(stack.get("content_weight", 1.0 - als) or (1.0 - als))
        live_state = str(live.get("status") or "baseline")
        reliability = self.reliability_gate.snapshot
        shadow = self.s.shadow_retrieval.status() if hasattr(self.s,"shadow_retrieval") else {}

        box=QFrame(); box.setObjectName("PremiumCard")
        layout=QVBoxLayout(box); layout.setContentsMargins(20,17,20,17); layout.setSpacing(8)
        top=QHBoxLayout()
        heading=QLabel("PROTECȚIA RECOMANDĂRILOR"); heading.setObjectName("Kicker"); top.addWidget(heading)
        top.addStretch(1)
        formula=QLabel(f"{als*100:.0f}% ALS / {content_weight*100:.0f}% conținut")
        formula.setObjectName("Score"); top.addWidget(formula); layout.addLayout(top)

        if live_state == "rolled_back":
            title="Revenire automată activă"
            detail="Formula personală a regresat pe rezultate reale; lista folosește din nou motorul sigur."
        elif live_state == "protected":
            title="Formula personală este confirmată"
            detail="Alegerile, vizionările și ratingurile tale reale nu indică o regresie."
        elif live_state == "collecting":
            title="Formula personală este verificată în utilizare"
            detail="Programul strânge rezultate reale; nu retrage formula pe baza câtorva cazuri izolate."
        else:
            title="Motor sigur activ"
            detail="70/30 rămâne activ până când istoricul tău dovedește că alt raport este mai bun."
        label=QLabel(title); label.setObjectName("BodyStrong"); layout.addWidget(label)
        note=QLabel(detail); note.setObjectName("Muted"); note.setWordWrap(True); layout.addWidget(note)

        ready = reliability.measurement_ready
        reliability_title = QLabel(
            "Precizia estimărilor este validată" if ready else "Precizia estimărilor este încă în măsurare"
        )
        reliability_title.setObjectName("BodyStrong")
        layout.addWidget(reliability_title)
        measured_progress=QProgressBar()
        measured_progress.setRange(0,MIN_MEASURED_OUTCOMES)
        measured_progress.setValue(min(reliability.rated_outcomes,MIN_MEASURED_OUTCOMES))
        measured_progress.setFormat(
            f"Rezultate reale cu notă: {reliability.rated_outcomes}/{MIN_MEASURED_OUTCOMES} minim"
        )
        measured_progress.setTextVisible(True); measured_progress.setFixedHeight(18)
        layout.addWidget(measured_progress)
        mae = f"{reliability.mae:.2f}" if reliability.mae is not None else "—"
        within = f"{reliability.within_one*100:.0f}%" if reliability.within_one is not None else "—"
        reliability_detail=QLabel(
            f"Eroare medie: {mae} puncte (țintă ≤1.00) • în ±1 punct: {within} (țintă ≥65%)."
        )
        reliability_detail.setObjectName("Muted"); reliability_detail.setWordWrap(True)
        layout.addWidget(reliability_detail)

        if shadow:
            shadow_title=QLabel("Challenger full-catalog rulează în umbră")
            shadow_title.setObjectName("BodyStrong"); layout.addWidget(shadow_title)
            runs=int(shadow.get("runs",0) or 0)
            challenger=dict(shadow.get("challenger") or {})
            rated=int(challenger.get("rated",0) or 0)
            minimum=int(shadow.get("minimum_challenger_ratings",20) or 20)
            paired=dict(shadow.get("paired") or {})
            paired_runs=int(paired.get("runs",0) or 0)
            minimum_paired=int(shadow.get("minimum_paired_runs",8) or 8)
            generator=dict(shadow.get("generator") or {})
            found=int(generator.get("non_als_candidates",generator.get("candidate_count",0)) or 0)
            shadow_note=QLabel(
                f"Nu schimbă lista afișată. {runs} comparații v4.14 • {found} candidați fără ALS • "
                f"{rated}/{minimum} rezultate challenger • {paired_runs}/{minimum_paired} rulări comparabile. "
                "Shadow-ul live este observațional: challengerul ascuns nu are aceeași expunere, "
                "deci nu poate fi promovat numai din aceste rezultate."
            )
            shadow_note.setObjectName("Muted"); shadow_note.setWordWrap(True); layout.addWidget(shadow_note)

        if live_state in {"collecting", "protected"}:
            active=live.get("active") or {}
            comparison=live.get("comparison") or {}
            rated=int(active.get("rated",0) or 0)
            chosen=int(active.get("chosen",0) or 0)
            minimum=int(comparison.get("minimum_rated_each",20) or 20)
            progress=QProgressBar(); progress.setRange(0,max(1,minimum)); progress.setValue(min(rated,minimum))
            progress.setFormat(f"Rezultate cu rating: {rated}/{minimum} minim")
            progress.setTextVisible(True); progress.setFixedHeight(18); layout.addWidget(progress)
            measured=QLabel(f"Rezultate atribuite formulei active: {chosen} alegeri • {rated} cu rating")
            measured.setObjectName("Muted"); measured.setWordWrap(True); layout.addWidget(measured)

        changed=int(policy.get("changed_ratings",0) or 0)
        required=int(policy.get("required_ratings",0) or 0)
        policy_state=str(policy.get("state") or "")
        calibration_state=str(status.get("status") or "")
        if calibration_state == "running":
            step=int(status.get("progress_step",0) or 0); total=int(status.get("progress_total",0) or 0)
            stage=str(status.get("progress_label") or "Calibrez motorul")
            schedule=f"Calibrare în curs: {stage} • {step}/{total} etape terminate."
        elif policy_state == "deferred":
            schedule=f"Backtest economisit: {changed}/{required} ratinguri noi sau modificate; feedbackul temporar nu îl repornește."
        elif policy_state in {"first_calibration","ranking_changed","ready"}:
            schedule="Calibrarea personală este pregătită sau rulează în fundal; motorul validat rămâne activ până la verdict."
        else:
            schedule="Calibrarea este la zi; deschiderea programului nu repetă backtestul."
        schedule_label=QLabel(schedule); schedule_label.setObjectName("Muted")
        schedule_label.setWordWrap(True); layout.addWidget(schedule_label)
        try:
            collaborative=dict(self.s.recommender.collaborative.status() or {})
        except Exception:
            collaborative={}
        mapped=int(collaborative.get("mapped_ratings",0) or 0)
        total_ratings=int(collaborative.get("total_ratings",0) or 0)
        if total_ratings:
            percent=round(100.0*mapped/total_ratings)
            mapping=QLabel(f"Acoperire ALS: {mapped:,}/{total_ratings:,} ratinguri mapate ({percent}%).")
            mapping.setObjectName("Muted"); mapping.setWordWrap(True); layout.addWidget(mapping)
        return box

    def _schedule_retrieval_shadow(self, recs: Iterable[Recommendation], slot: str) -> None:
        observer=getattr(self.s,"shadow_retrieval",None)
        if observer is None:
            return
        ids=[int(rec.movie.id) for rec in recs if rec is not None and rec.movie.id]
        if ids:
            observer.schedule(date.today(),slot,ids)

    def recommendation_metadata_card(self):
        report=dict(self.recommendation_metadata_report or {})
        coverage=dict(report.get("after") or report.get("before") or {})
        total=int(coverage.get("total",0) or 0)
        complete=int(coverage.get("ranking_complete",0) or 0)
        average=int(coverage.get("average_percent",0) or 0)
        posters=int(coverage.get("poster_complete",0) or 0)
        state=str(report.get("state") or "idle")

        box=QFrame(); box.setObjectName("PremiumCard")
        layout=QVBoxLayout(box); layout.setContentsMargins(20,17,20,17); layout.setSpacing(8)
        top=QHBoxLayout()
        heading=QLabel("DATELE RECOMANDĂRILOR"); heading.setObjectName("Kicker"); top.addWidget(heading)
        top.addStretch(1)
        score=QLabel(f"{average}% semnale disponibile" if total else "în așteptare")
        score.setObjectName("Score"); top.addWidget(score); layout.addLayout(top)

        if state == "checking":
            title="Verific metadatele înainte de clasarea finală"
            pool=int(report.get("pool_size",total) or total)
            detail=f"Completez cu prioritate cele 12 filme vizibile, apoi folosesc eventualele verificări rămase pentru finaliștii apropiați dintre {pool} de candidați."
        elif state == "failed":
            title="Clasarea sigură a fost păstrată"
            failed=int(report.get("failed",0) or 0); attempted=int(report.get("attempted",0) or 0)
            detail=(
                f"Sursele publice nu au răspuns pentru {failed}/{attempted} titluri verificate; programul nu a modificat ordinea pe baza unor date incomplete."
                if attempted else
                "Sursele publice nu au răspuns; programul a păstrat clasarea locală și nu a folosit date incomplete."
            )
        elif state == "partial":
            title="Date completate parțial; clasarea rămâne protejată"
            failed=int(report.get("failed",0) or 0); attempted=int(report.get("attempted",0) or 0)
            detail=f"Sursele publice nu au răspuns pentru {failed}/{attempted} titluri verificate. Au fost folosite numai datele factuale confirmate."
        elif bool(report.get("reranked")):
            title="Clasare recalculată după completarea datelor"
            fields=sum(len(value) for value in (report.get("ranking_fields_added") or {}).values())
            detail=f"Au fost adăugate {fields} câmpuri factuale care influențează gustul; ordinea finală a fost calculată o singură dată din nou."
        elif int(report.get("changed_titles",0) or 0) > 0:
            title="Detalii completate; ordinea a rămas neschimbată"
            detail="S-au completat numai elemente vizuale sau informative, fără un motiv factual de reclasare."
        else:
            title="Clasare bazată pe datele disponibile"
            detail="Nu au apărut câmpuri noi care să justifice schimbarea ordinii recomandărilor."
        label=QLabel(title); label.setObjectName("BodyStrong"); layout.addWidget(label)
        note=QLabel(detail); note.setObjectName("Muted"); note.setWordWrap(True); layout.addWidget(note)

        timed_out=bool(report.get("timed_out"))
        timeout_note=" • limita de timp a protejat afișarea" if timed_out else ""
        facts=QLabel(
            f"{complete}/{total} titluri au toate semnalele de clasare urmărite • "
            f"{posters}/{total} au poster • maximum "
            f"{int(report.get('io_limit',MAX_PREFLIGHT_TITLES) or MAX_PREFLIGHT_TITLES)} verificări per listă{timeout_note}"
            if total else "Acoperirea va fi calculată după prima listă."
        )
        facts.setObjectName("Muted"); facts.setWordWrap(True); layout.addWidget(facts)

        results=dict(report.get("title_results") or {})
        unresolved=[]
        for movie_id,result in results.items():
            if str(result.get("status") or "") not in {"complete"}:
                unresolved.append(result)
        if unresolved:
            counts={}
            labels={
                "partial":"completate parțial", "not_found":"negăsite în surse",
                "error":"cu eroare temporară", "timeout":"oprite de limita de timp",
                "unavailable":"fără toate câmpurile", "deferred":"amânate",
            }
            for result in unresolved:
                key=str(result.get("status") or "unavailable")
                counts[key]=counts.get(key,0)+1
            summary=" • ".join(f"{count} {labels.get(key,key)}" for key,count in counts.items())
            diagnosis=QLabel("Diagnostic: "+summary)
            diagnosis.setObjectName("Muted"); diagnosis.setWordWrap(True); layout.addWidget(diagnosis)

        retryable=self._incomplete_visible_recommendations()
        if retryable and state != "checking":
            retry=QPushButton(f"Reîncearcă doar lipsurile ({len(retryable)})")
            retry.clicked.connect(self.retry_missing_recommendation_metadata)
            layout.addWidget(retry,0,Qt.AlignLeft)
        return box

    def _incomplete_visible_recommendations(self) -> list[Recommendation]:
        return [
            rec for rec in list(self.browse_result or [])[:12]
            if rec.movie.id and rec.movie.imdb_id and not all(metadata_snapshot(rec.movie).values())
        ]

    def retry_missing_recommendation_metadata(self) -> None:
        if self.metadata_worker and self.metadata_worker.isRunning():
            self.set_status("Verificarea metadatelor este deja în curs.",True)
            return
        retryable=self._incomplete_visible_recommendations()
        ids={int(rec.movie.id) for rec in retryable if rec.movie.id}
        if not ids:
            self.set_status("Nu există metadate lipsă care pot fi reverificate.",False)
            return
        self.metadata_attempted.difference_update(ids)
        reset_metadata_cache(self.db,ids)
        coverage=coverage_report(list(self.browse_result or [])[:12])
        self.recommendation_metadata_report={
            "state":"checking", "before":coverage, "after":coverage,
            "attempted":0, "changed_titles":0, "ranking_fields_added":{},
            "ranking_change":False, "reranked":False, "io_limit":MAX_PREFLIGHT_TITLES,
            "pool_size":len(self.browse_result), "retrying":True,
        }
        self.set_status(f"Reverific {len(ids)} filme cu date lipsă…",True)
        if self.current_page=="recommendations":
            self._render_browse(self.browse_result)
        self._ensure_recommendation_metadata(list(self.browse_result))

    def compact_recommendation_card(self,rec:Recommendation,index:int):
        m,s=rec.movie,rec.score
        box=QFrame(); box.setObjectName("PremiumCard"); box.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Minimum)
        main=QHBoxLayout(box); main.setContentsMargins(15,15,15,15); main.setSpacing(14)
        poster=self.poster_label(104,156); main.addWidget(poster,0,Qt.AlignTop)
        if m.poster_url:self.load_poster_async(poster,m.poster_url,m.imdb_id or str(m.id))
        l=QVBoxLayout(); l.setSpacing(7)
        head=QHBoxLayout(); title=QLabel(f"{index}. {m.title}"+(f" ({m.year})" if m.year else "")); title.setObjectName("CardTitle"); title.setWordWrap(True); head.addWidget(title,1)
        score=QLabel(f"{s.predicted_rating:.1f}/10"); score.setObjectName("Score"); head.addWidget(score); l.addLayout(head)
        l.addWidget(self.reliability_widget(rec,compact=True))
        meta=QLabel(" • ".join(self.movie_chips(m,5))); meta.setObjectName("Muted"); meta.setWordWrap(True); l.addWidget(meta)
        overview=QLabel(self.overview_text(m)); overview.setWordWrap(True); overview.setMaximumHeight(66); overview.setObjectName("Muted"); l.addWidget(overview)
        result=dict((self.recommendation_metadata_report or {}).get("title_results") or {}).get(int(m.id or 0),{})
        if result and str(result.get("status") or "") != "complete":
            data_status=QLabel("Date: "+str(result.get("reason") or "metadate incomplete"))
            data_status.setObjectName("Muted"); data_status.setWordWrap(True); l.addWidget(data_status)
        elif not all(metadata_snapshot(m).values()):
            missing=", ".join(missing_metadata_labels(m))
            data_status=QLabel("Date incomplete: "+missing+".")
            data_status.setObjectName("Muted"); data_status.setWordWrap(True); l.addWidget(data_status)
        reason=QLabel(self.human_reason(rec)); reason.setWordWrap(True)
        reason.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Minimum); l.addWidget(reason)
        row=QGridLayout(); row.setHorizontalSpacing(7); row.setVerticalSpacing(7)
        choose=QPushButton("Aleg filmul"); choose.setProperty("accent",True)
        choose.clicked.connect(lambda _checked=False, mid=m.id, eid=getattr(rec,"exposure_history_id",None): self.choose_decision(mid,eid))
        row.addWidget(choose,0,0)
        details=QPushButton("Detalii"); details.clicked.connect(lambda _,r=rec:self.open_details(r)); row.addWidget(details,0,1)
        watch=QPushButton("Watchlist"); watch.clicked.connect(lambda _,mid=m.id:self.feedback(mid,"want_to_watch")); row.addWidget(watch,1,0)
        no=QPushButton("Nu acum / motiv")
        no.clicked.connect(lambda _checked=False, mid=m.id, b=no: self.contextual_feedback_menu(mid, b))
        row.addWidget(no,1,1); row.setColumnStretch(0,1); row.setColumnStretch(1,1); l.addLayout(row)
        main.addLayout(l,1); return box

    # ---------- taste hub ----------
    def page_profile(self):
        p=get_profile(self.db)
        page,content=self.page_shell("Taste Hub","Profilul pe care motorul îl folosește efectiv când îți estimează ratingul pentru un film nevăzut.")
        metrics=QGridLayout(); metrics.setHorizontalSpacing(12); metrics.setVerticalSpacing(12)
        rated=int(p.get("rated_count",0) or 0); mean=float(p.get("global_mean_rating",0) or 0); delta=p.get("mean_user_minus_imdb")
        vals=[(f"{rated:,}","ratinguri analizate"),(f"{mean:.2f}","media ta"),(f"{delta:+.2f}" if delta is not None else "—","tu vs IMDb"),("2.0","motor de gust")]
        for i,(value,label) in enumerate(vals):
            card=QFrame(); card.setObjectName("PremiumCard"); l=QVBoxLayout(card); l.setContentsMargins(18,16,18,16)
            v=QLabel(value); v.setObjectName("MetricValue"); l.addWidget(v); t=QLabel(label); t.setObjectName("Muted"); l.addWidget(t); metrics.addWidget(card,0,i)
        mw=QFrame(); mw.setLayout(metrics); content.addWidget(mw)

        sections=[("Genurile tale","genre:"),("Regizori care îți merg","director:"),("Teme / atmosferă","theme:")]
        for title,prefix in sections:
            h=QLabel(title); h.setObjectName("SectionTitle"); content.addWidget(h)
            box=QFrame(); box.setObjectName("PremiumCard"); l=QVBoxLayout(box); l.setContentsMargins(20,18,20,18); l.setSpacing(10)
            items=top_profile_features(p,prefix,True,8)
            if not items:
                x=QLabel("Încă nu sunt suficiente date pentru această secțiune."); x.setObjectName("Muted"); l.addWidget(x)
            for name,st in items:
                clean=name.split(":",1)[1].replace("_"," ").title(); pref=max(0.0,float(st.get("preference",0) or 0)); avg=st.get("mean_rating"); count=int(st.get("count",0) or 0)
                row=QHBoxLayout(); lab=QLabel(clean); lab.setObjectName("BodyStrong"); row.addWidget(lab,1)
                meta=QLabel((f"{float(avg):.1f}/10 • {count} filme" if avg is not None else f"{count} filme")); meta.setObjectName("Muted"); row.addWidget(meta); l.addLayout(row)
                bar=QProgressBar(); bar.setRange(0,100); bar.setValue(max(4,min(100,int(pref*100)))); bar.setTextVisible(False); bar.setFixedHeight(9); l.addWidget(bar)
            content.addWidget(box)

        avoided=top_profile_features(p,None,False,8)
        h=QLabel("Ce tinde să nu funcționeze pentru tine"); h.setObjectName("SectionTitle"); content.addWidget(h)
        box=QFrame(); box.setObjectName("PremiumCard"); l=QHBoxLayout(box); l.setContentsMargins(20,18,20,18)
        if avoided:
            for name,st in avoided[:6]: l.addWidget(self.pill(name.split(":",1)[-1].replace("_"," ").title()))
        else:
            x=QLabel("Nu sunt încă semnale negative stabile."); x.setObjectName("Muted"); l.addWidget(x)
        l.addStretch(1); content.addWidget(box); content.addStretch(1)
        return page

    # ---------- metadata enrichment ----------
    def _ensure_metadata(self,recs:Iterable[Recommendation],page_key:str):
        if page_key == "recommendations":
            self._ensure_recommendation_metadata(list(recs))
            return
        if self.metadata_worker and self.metadata_worker.isRunning(): return
        targets=[]
        token=str(self.db.get_setting("tmdb_token","") or "").strip()
        for rec in recs:
            m=rec.movie
            if not m.id or not m.imdb_id or int(m.id) in self.metadata_attempted: continue
            localize = False
            if token and m.overview:
                try:
                    localize = metadata_sources_for_movie(self.db, int(m.id)).get("overview") in {"tmdb", "tmdb-en"}
                except Exception:
                    localize = False
            if m.overview and m.poster_url and m.runtime_min and not localize: continue
            targets.append(rec); self.metadata_attempted.add(int(m.id))
        if not targets:return
        def fn(progress):
            tmdb=None
            if token:
                try: tmdb=TmdbProvider(self.db,token)
                except Exception: tmdb=None
            open_provider=OpenMovieMetadataProvider(self.db)
            for i,rec in enumerate(targets,1):
                progress(f"Completez detaliile filmelor… {i}/{len(targets)}")
                m=rec.movie
                if tmdb:
                    try: tmdb.enrich_by_imdb(m)
                    except Exception: pass
                if not m.overview or not m.poster_url or not m.runtime_min:
                    try: open_provider.enrich_by_imdb(m)
                    except Exception: pass
            return True
        worker=WorkerThread(fn,self); self.metadata_worker=worker; worker.message.connect(lambda m:self.set_status(m,True))
        def done(_):
            self.metadata_worker=None; self.set_status("Detaliile filmelor sunt actualizate.",False)
            if self.current_page==page_key:
                if page_key=="today" and self.today_result:self._render_today(*self.today_result)
                elif page_key=="recommendations":self._render_browse(self.browse_result)
        def fail(_):
            self.metadata_worker=None; self.set_status("Recomandările sunt gata; unele descrieri nu au putut fi completate.",False)
        worker.success.connect(done); worker.failure.connect(fail); worker.start()

    def _ensure_recommendation_metadata(self,recs:list[Recommendation]):
        if self.metadata_worker and self.metadata_worker.isRunning(): return
        generation=int(self.browse_generation)
        token=str(self.db.get_setting("tmdb_token","") or "").strip()

        def fn(progress):
            preflight=CandidateMetadataPreflight(self.db,token)
            report=preflight.run(
                recs,
                attempted_ids=self.metadata_attempted,
                limit=MAX_PREFLIGHT_TITLES,
                progress=progress,
            )
            report["pool_before"]=dict(report.get("before") or {})
            report["before"]=coverage_report(recs[:12])
            final=list(recs[:12])
            report["reranked"]=False
            if report.get("ranking_change"):
                progress("Recalculez ordinea cu metadatele factuale noi…")
                final=list(self.s.recommender.recommend(
                    date.today(),12,
                    exclude_ids=set(self.session_skips),
                    record=False,
                    slot="browse",
                    candidate_limit=45000,
                    mode="decide",
                ))
                report["reranked"]=True
                report["after"]=coverage_report(final)
            else:
                report["after"]=coverage_report(final)
            return {"report":report,"recommendations":final}

        worker=WorkerThread(fn,self); self.metadata_worker=worker
        worker.message.connect(lambda message:self.set_status(message,True))

        def done(payload):
            self.metadata_worker=None
            if generation != int(self.browse_generation):
                if self.current_page=="recommendations" and self.browse_result:
                    self._ensure_recommendation_metadata(list(self.browse_result))
                return
            self.recommendation_metadata_report=dict(payload.get("report") or {})
            self.browse_result=list(payload.get("recommendations") or recs)
            changed=int(self.recommendation_metadata_report.get("changed_titles",0) or 0)
            state=str(self.recommendation_metadata_report.get("state") or "completed")
            failed=int(self.recommendation_metadata_report.get("failed",0) or 0)
            if state == "failed":
                message="Recomandările sunt gata; sursele de metadate nu au răspuns."
            elif state == "partial":
                message=f"Recomandările sunt gata; {failed} titluri nu au putut fi verificate."
            elif changed:
                message="Clasarea finală folosește metadatele verificate."
            else:
                message="Recomandările sunt gata; ordinea nu a necesitat modificări."
            self.set_status(message,False)
            if self.current_page=="recommendations":
                self._render_browse(self.browse_result)

        def fail(message):
            self.metadata_worker=None
            if generation != int(self.browse_generation):
                if self.current_page=="recommendations" and self.browse_result:
                    self._ensure_recommendation_metadata(list(self.browse_result))
                return
            coverage=coverage_report(recs)
            self.recommendation_metadata_report={
                "state":"failed", "before":coverage, "after":coverage,
                "attempted":0, "changed_titles":0, "ranking_fields_added":{},
                "ranking_change":False, "reranked":False, "io_limit":MAX_PREFLIGHT_TITLES,
                "error":str(message),
            }
            self.set_status("Recomandările sunt gata; sursa de metadate nu a răspuns.",False)
            if self.current_page=="recommendations":
                self._render_browse(self.browse_result)

        worker.success.connect(done); worker.failure.connect(fail); worker.start()

    def open_details(self,rec:Recommendation):
        MovieDetailDialog(rec,self).exec()

    # The legacy native updater replaces one standalone EXE. Premium uses a much faster
    # portable onedir package, so pretending that updater is compatible would be unsafe.
    def page_updates(self):
        page,content=self.page_shell("Actualizări","Buildul Premium prioritizează pornirea rapidă și folosește un pachet portabil complet.")
        box=QFrame(); box.setObjectName("PremiumCard"); l=QVBoxLayout(box); l.setContentsMargins(22,20,22,20); l.setSpacing(10)
        h=QLabel(f"CineCalendar {APP_VERSION} Premium"); h.setObjectName("SectionTitle"); l.addWidget(h)
        text=QLabel("Updaterul vechi, care înlocuia un singur EXE, este dezactivat în buildul Premium deoarece aplicația folosește acum un folder runtime pentru pornire mult mai rapidă. Datele tale rămân separat în CineCalendarData. Actualizarea bundle-aware va fi activată numai după ce are backup și rollback complet.")
        text.setObjectName("Muted"); text.setWordWrap(True); l.addWidget(text)
        content.addWidget(box); content.addStretch(1); return page


def run_premium(service,on_ready=None):
    app=QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("CineCalendar"); app.setOrganizationName("CineCalendar")
    try: app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception: pass
    w=PremiumDecisionWindow(service); w.show()
    if on_ready is not None:
        QTimer.singleShot(350,on_ready)
    return app.exec()
