from __future__ import annotations

import sys
from datetime import date
from typing import Iterable

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import __version__ as APP_VERSION
from .open_metadata import OpenMovieMetadataProvider
from .profile import get_profile, top_profile_features
from .qt_ui import WorkerThread
from .qt_ui_v2 import DecisionWindow
from .recommendation import Recommendation
from .tmdb import TmdbProvider


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
        confidence = owner.metric_badge(f"{round(rec.score.confidence*100)}%", "încredere")
        score_row.addWidget(confidence)
        if rec.movie.imdb_rating is not None:
            score_row.addWidget(owner.metric_badge(f"{rec.movie.imdb_rating:.1f}", "IMDb"))
        score_row.addStretch(1)
        info.addLayout(score_row)

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
        super().__init__(service)
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
            out.append(movie.countries[0])
        return out[:limit]

    def overview_text(self, movie, long: bool = False) -> str:
        text = (movie.overview or "").strip()
        if text:
            if long or len(text) <= 360:
                return text
            return text[:357].rsplit(" ", 1)[0] + "…"
        return "Descrierea și imaginea se completează automat din surse deschise. Nu trebuie să adaugi nimic manual."

    def human_reason(self, rec: Recommendation) -> str:
        m, s = rec.movie, rec.score
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
            lead = "O alegere mai exploratorie, cu încredere moderată"
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
        worker = WorkerThread(lambda progress: self.s.recommender.decision_pick(date.today(), self.session_skips, self.decision_mode), self)
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
        self.record_once([primary], date.today(), "decision")
        self.today_content.addWidget(self.decision_hero(primary))

        mode = QFrame(); mode.setObjectName("PremiumCard")
        ml = QHBoxLayout(mode); ml.setContentsMargins(16,12,16,12)
        label = QLabel("Reglaj rapid")
        label.setObjectName("BodyStrong"); ml.addWidget(label); ml.addStretch(1)
        for text, value in (("Echilibrat","decide"),("Mai sigur","safe"),("Surprinde-mă","surprise"),("Mai scurt","short")):
            b=QPushButton(text)
            if value==self.decision_mode: b.setProperty("accent",True)
            b.clicked.connect(lambda _,v=value:self.set_decision_mode(v)); ml.addWidget(b)
        self.today_content.addWidget(mode)

        if backups:
            h=QLabel("Alternative bune, dacă prima alegere nu te prinde")
            h.setObjectName("SectionTitle"); self.today_content.addWidget(h)
            grid=QGridLayout(); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(14)
            for i, rec in enumerate(backups[:2]): grid.addWidget(self.backup_card(rec),0,i)
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
        metric.addWidget(self.metric_badge(f"{round(s.confidence*100)}%","încredere"))
        if m.imdb_rating is not None: metric.addWidget(self.metric_badge(f"{m.imdb_rating:.1f}","IMDb"))
        metric.addStretch(1); right.addLayout(metric)

        chips=QHBoxLayout()
        for text in self.movie_chips(m): chips.addWidget(self.pill(text))
        chips.addStretch(1); right.addLayout(chips)

        overview=QLabel(self.overview_text(m)); overview.setObjectName("Overview"); overview.setWordWrap(True); overview.setMaximumHeight(118); right.addWidget(overview)
        reason=QLabel(self.human_reason(rec)); reason.setWordWrap(True); reason.setObjectName("BodyStrong"); right.addWidget(reason)
        if s.calendar_reason and s.calendar >= .48:
            now=QLabel("De ce acum: "+s.calendar_reason); now.setObjectName("Muted"); now.setWordWrap(True); right.addWidget(now)

        actions=QHBoxLayout()
        choose=QPushButton("Aleg filmul ăsta"); choose.setProperty("accent",True); choose.clicked.connect(lambda _,mid=m.id:self.choose_decision(mid)); actions.addWidget(choose)
        detail=QPushButton("Detalii"); detail.clicked.connect(lambda _,r=rec:self.open_details(r)); actions.addWidget(detail)
        other=QPushButton("Alt film"); other.clicked.connect(lambda _,mid=m.id:self.skip_decision(mid)); actions.addWidget(other)
        actions.addStretch(1); right.addLayout(actions)
        main.addLayout(right,1)
        return box

    def backup_card(self, rec: Recommendation):
        m,s=rec.movie,rec.score
        box=QFrame(); box.setObjectName("PremiumCard"); box.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Minimum)
        main=QHBoxLayout(box); main.setContentsMargins(16,16,16,16); main.setSpacing(14)
        poster=self.poster_label(88,132); main.addWidget(poster,0,Qt.AlignTop)
        if m.poster_url:self.load_poster_async(poster,m.poster_url,m.imdb_id or str(m.id))
        l=QVBoxLayout(); t=QLabel(m.title+(f" ({m.year})" if m.year else "")); t.setObjectName("CardTitle"); t.setWordWrap(True); l.addWidget(t)
        p=QLabel(f"{s.predicted_rating:.1f}/10 pentru tine • {round(s.confidence*100)}% încredere"); p.setObjectName("Score"); l.addWidget(p)
        meta=" • ".join(self.movie_chips(m,4)); x=QLabel(meta); x.setObjectName("Muted"); x.setWordWrap(True); l.addWidget(x)
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
        self.set_status("Calculez recomandările…",True)
        worker=WorkerThread(lambda progress:self.s.recommender.recommend(date.today(),12,record=False,slot="browse",candidate_limit=45000,mode="decide"),self)
        self.browse_worker=worker
        def success(recs):
            self.browse_worker=None; self.browse_result=list(recs); self.set_status("Recomandările sunt gata.",False)
            if self.current_page=="recommendations":
                self._render_browse(self.browse_result); self._ensure_metadata(self.browse_result[:6],"recommendations")
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
        intro=QFrame(); intro.setObjectName("PremiumCard"); il=QHBoxLayout(intro); il.setContentsMargins(18,14,18,14)
        txt=QLabel("Scorul personal estimat este principalul criteriu. IMDb, noutatea și perioada curentă sunt filtre secundare."); txt.setObjectName("Muted"); txt.setWordWrap(True); il.addWidget(txt,1)
        self.browse_content.addWidget(intro)
        grid=QGridLayout(); grid.setHorizontalSpacing(14); grid.setVerticalSpacing(14)
        for i,rec in enumerate(recs): grid.addWidget(self.compact_recommendation_card(rec,i+1),i//2,i%2)
        wrap=QFrame(); wrap.setLayout(grid); self.browse_content.addWidget(wrap); self.browse_content.addStretch(1)

    def compact_recommendation_card(self,rec:Recommendation,index:int):
        m,s=rec.movie,rec.score
        box=QFrame(); box.setObjectName("PremiumCard"); box.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Minimum)
        main=QHBoxLayout(box); main.setContentsMargins(15,15,15,15); main.setSpacing(14)
        poster=self.poster_label(104,156); main.addWidget(poster,0,Qt.AlignTop)
        if m.poster_url:self.load_poster_async(poster,m.poster_url,m.imdb_id or str(m.id))
        l=QVBoxLayout(); l.setSpacing(7)
        head=QHBoxLayout(); title=QLabel(f"{index}. {m.title}"+(f" ({m.year})" if m.year else "")); title.setObjectName("CardTitle"); title.setWordWrap(True); head.addWidget(title,1)
        score=QLabel(f"{s.predicted_rating:.1f}/10"); score.setObjectName("Score"); head.addWidget(score); l.addLayout(head)
        meta=QLabel(" • ".join(self.movie_chips(m,5))); meta.setObjectName("Muted"); meta.setWordWrap(True); l.addWidget(meta)
        overview=QLabel(self.overview_text(m)); overview.setWordWrap(True); overview.setMaximumHeight(66); overview.setObjectName("Muted"); l.addWidget(overview)
        reason=QLabel(self.human_reason(rec)); reason.setWordWrap(True); reason.setMaximumHeight(58); l.addWidget(reason)
        row=QHBoxLayout(); details=QPushButton("Detalii"); details.clicked.connect(lambda _,r=rec:self.open_details(r)); row.addWidget(details)
        watch=QPushButton("Watchlist"); watch.clicked.connect(lambda _,mid=m.id:self.feedback(mid,"want_to_watch")); row.addWidget(watch)
        no=QPushButton("Nu"); no.clicked.connect(lambda _,mid=m.id:self.feedback(mid,"not_interested")); row.addWidget(no); row.addStretch(1); l.addLayout(row)
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
        if self.metadata_worker and self.metadata_worker.isRunning(): return
        targets=[]
        for rec in recs:
            m=rec.movie
            if not m.id or not m.imdb_id or int(m.id) in self.metadata_attempted: continue
            if m.overview and m.poster_url and m.runtime_min: continue
            targets.append(rec); self.metadata_attempted.add(int(m.id))
        if not targets:return
        token=str(self.db.get_setting("tmdb_token","") or "").strip()
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
