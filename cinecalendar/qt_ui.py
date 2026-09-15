from __future__ import annotations

import hashlib
import os
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Any

import requests
from PySide6.QtCore import Qt, QObject, Signal, QThread, QTimer, QUrl, QSize
from PySide6.QtGui import QDesktopServices, QPixmap, QIcon, QColor, QPainter, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QScrollArea, QFrame, QFileDialog, QMessageBox, QDialog, QFormLayout,
    QLineEdit, QSpinBox, QComboBox, QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressBar, QSizePolicy, QGridLayout, QGroupBox, QTextBrowser, QSpacerItem
)

from .backup import export_profile, import_profile
from .catalog import bootstrap_official_imdb_catalog, import_imdb_datasets
from .feedback import apply_feedback
from .imdb_import import import_imdb_csv, add_manual_rating
from .profile import build_profile, get_profile, top_profile_features
from .recommendation import Recommendation
from .tmdb import TmdbProvider, enrich_library
from .util import json_loads
from .watcher import RatingsFolderWatcher


APP_VERSION = "1.0.0"

DARK = {
    "bg": "#0B0F14", "surface": "#121821", "card": "#171F2A", "card2": "#1C2633",
    "text": "#F4F7FB", "muted": "#91A0B3", "border": "#263342", "accent": "#6EA8FE",
    "accent2": "#8B5CF6", "good": "#62D394", "warn": "#F6C453", "bad": "#FF7A7A"
}
LIGHT = {
    "bg": "#F3F6FA", "surface": "#FFFFFF", "card": "#FFFFFF", "card2": "#F8FAFC",
    "text": "#18212F", "muted": "#627083", "border": "#DDE4EC", "accent": "#2563EB",
    "accent2": "#7C3AED", "good": "#178A4B", "warn": "#9A6800", "bad": "#C83737"
}


class WorkerThread(QThread):
    message = Signal(str)
    success = Signal(object)
    failure = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str], None]], Any], parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            result = self.fn(lambda m: self.message.emit(str(m)))
            self.success.emit(result)
        except Exception as exc:
            self.failure.emit(str(exc))


class ManualRatingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Adaugă rating")
        self.setMinimumWidth(430)
        lay = QFormLayout(self)
        self.title = QLineEdit()
        self.year = QSpinBox(); self.year.setRange(1880, 2100); self.year.setValue(date.today().year)
        self.rating = QSpinBox(); self.rating.setRange(1, 10); self.rating.setValue(7)
        self.imdb = QLineEdit(); self.imdb.setPlaceholderText("opțional, ex. tt0050976")
        self.genres = QLineEdit(); self.genres.setPlaceholderText("opțional, separate prin virgulă")
        lay.addRow("Titlu", self.title); lay.addRow("An", self.year); lay.addRow("Rating 1–10", self.rating)
        lay.addRow("IMDb ID", self.imdb); lay.addRow("Genuri", self.genres)
        row = QHBoxLayout(); row.addStretch(1)
        cancel = QPushButton("Renunță"); save = QPushButton("Salvează"); save.setProperty("accent", True)
        cancel.clicked.connect(self.reject); save.clicked.connect(self.accept)
        row.addWidget(cancel); row.addWidget(save); lay.addRow(row)

    def values(self):
        return {
            "title": self.title.text().strip(), "year": self.year.value(), "rating": self.rating.value(),
            "imdb_id": self.imdb.text().strip() or None,
            "genres": [x.strip() for x in self.genres.text().split(",") if x.strip()]
        }


class ScoreDialog(QDialog):
    def __init__(self, rec: Recommendation, parent=None):
        super().__init__(parent)
        self.setWindowTitle("De ce mi-ai recomandat asta?")
        self.resize(720, 560)
        root = QVBoxLayout(self)
        title = QLabel(rec.movie.title); title.setObjectName("DialogTitle")
        root.addWidget(title)
        score = QLabel(f"Scor final: {rec.score.final*100:.1f}%")
        score.setObjectName("ScoreLarge"); root.addWidget(score)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget(); vl = QVBoxLayout(inner)
        for name, pts, reason in rec.score.contributions:
            box = QFrame(); box.setObjectName("Card"); bl = QVBoxLayout(box)
            h = QLabel(f"{pts:+.1f} puncte  •  {name}"); h.setObjectName("CardTitle")
            r = QLabel(reason or "—"); r.setWordWrap(True); r.setObjectName("Muted")
            bl.addWidget(h); bl.addWidget(r); vl.addWidget(box)
        vl.addStretch(1); scroll.setWidget(inner); root.addWidget(scroll)
        close = QPushButton("Închide"); close.clicked.connect(self.accept); root.addWidget(close, alignment=Qt.AlignRight)


class CineCalendarWindow(QMainWindow):
    NAV = [
        ("today", "Azi"), ("calendar", "Calendar"), ("month", "Programul lunii"),
        ("profile", "Profilul meu"), ("ratings", "Ratinguri IMDb"), ("watchlist", "Watchlist"),
        ("history", "Istoric recomandări"), ("settings", "Setări")
    ]

    def __init__(self, service):
        super().__init__()
        self.s = service; self.db = service.db
        self.theme = self.db.get_setting("theme", "dark")
        self.current_page = "today"
        self.worker: WorkerThread | None = None
        self.poster_threads: list[WorkerThread] = []
        self.setWindowTitle(f"CineCalendar {APP_VERSION}")
        self.resize(1440, 900); self.setMinimumSize(1100, 700)
        self._set_icon()
        self._build_shell(); self.apply_theme(); self.show_page("today")
        self.watch_timer = QTimer(self); self.watch_timer.timeout.connect(self.scan_ratings_folder); self.watch_timer.start(15000)
        QTimer.singleShot(1200, self.auto_catalog_if_needed)

    def _set_icon(self):
        try:
            pm = QPixmap(64, 64); pm.fill(Qt.transparent)
            p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QColor("#6EA8FE")); p.setPen(Qt.NoPen); p.drawRoundedRect(4,4,56,56,14,14)
            p.setBrush(QColor("#0B0F14")); p.drawEllipse(17,15,30,30)
            p.setBrush(QColor("#F4F7FB")); p.drawEllipse(23,21,7,7); p.drawEllipse(34,21,7,7); p.drawEllipse(28,32,7,7)
            p.end(); self.setWindowIcon(QIcon(pm))
        except Exception:
            pass

    def colors(self): return DARK if self.theme == "dark" else LIGHT

    def apply_theme(self):
        c = self.colors()
        QApplication.instance().setStyleSheet(f"""
            QWidget {{ background:{c['bg']}; color:{c['text']}; font-family:'Segoe UI'; font-size:14px; }}
            QMainWindow, QScrollArea, QScrollArea>QWidget>QWidget {{ background:{c['bg']}; }}
            QFrame#Sidebar {{ background:{c['surface']}; border-right:1px solid {c['border']}; }}
            QFrame#Topbar {{ background:{c['bg']}; }}
            QFrame#Card {{ background:{c['card']}; border:1px solid {c['border']}; border-radius:14px; }}
            QLabel#Brand {{ font-size:24px; font-weight:700; }}
            QLabel#PageTitle {{ font-size:30px; font-weight:700; }}
            QLabel#CardTitle {{ font-size:17px; font-weight:700; }}
            QLabel#Muted {{ color:{c['muted']}; }}
            QLabel#Score {{ color:{c['good']}; font-size:18px; font-weight:700; }}
            QLabel#ScoreLarge {{ color:{c['good']}; font-size:24px; font-weight:700; }}
            QLabel#DialogTitle {{ font-size:26px; font-weight:700; }}
            QPushButton {{ background:{c['card2']}; border:1px solid {c['border']}; border-radius:9px; padding:9px 13px; }}
            QPushButton:hover {{ border-color:{c['accent']}; background:{c['card']}; }}
            QPushButton[accent='true'] {{ background:{c['accent']}; color:white; border-color:{c['accent']}; font-weight:700; }}
            QPushButton[nav='true'] {{ text-align:left; padding:11px 15px; background:transparent; border:0; }}
            QPushButton[nav='true']:hover {{ background:{c['card2']}; }}
            QPushButton[navActive='true'] {{ text-align:left; padding:11px 15px; background:{c['card2']}; border:1px solid {c['border']}; font-weight:700; }}
            QLineEdit, QSpinBox, QComboBox {{ background:{c['card2']}; border:1px solid {c['border']}; border-radius:8px; padding:8px; }}
            QCheckBox {{ spacing:8px; }}
            QTableWidget {{ background:{c['card']}; alternate-background-color:{c['card2']}; gridline-color:{c['border']}; border:1px solid {c['border']}; border-radius:10px; }}
            QHeaderView::section {{ background:{c['surface']}; color:{c['text']}; border:0; border-bottom:1px solid {c['border']}; padding:8px; font-weight:700; }}
            QProgressBar {{ border:1px solid {c['border']}; border-radius:7px; background:{c['card2']}; text-align:center; }}
            QProgressBar::chunk {{ background:{c['accent']}; border-radius:6px; }}
            QScrollBar:vertical {{ background:transparent; width:12px; margin:2px; }}
            QScrollBar::handle:vertical {{ background:{c['border']}; min-height:30px; border-radius:5px; }}
        """)

    def _build_shell(self):
        root = QWidget(); self.setCentralWidget(root); h = QHBoxLayout(root); h.setContentsMargins(0,0,0,0); h.setSpacing(0)
        side = QFrame(); side.setObjectName("Sidebar"); side.setFixedWidth(235); sv = QVBoxLayout(side); sv.setContentsMargins(16,18,16,16); sv.setSpacing(6)
        brand = QLabel("CineCalendar"); brand.setObjectName("Brand"); sv.addWidget(brand)
        sub = QLabel("calendar cinematografic personal"); sub.setObjectName("Muted"); sv.addWidget(sub); sv.addSpacing(18)
        self.nav_buttons = {}
        for key, label in self.NAV:
            b=QPushButton(label); b.setProperty("nav", True); b.clicked.connect(lambda _, k=key:self.show_page(k)); sv.addWidget(b); self.nav_buttons[key]=b
        sv.addStretch(1)
        self.status = QLabel("Pregătit"); self.status.setObjectName("Muted"); self.status.setWordWrap(True); sv.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.setVisible(False); self.progress.setRange(0,0); sv.addWidget(self.progress)
        version = QLabel(f"v{APP_VERSION}"); version.setObjectName("Muted"); sv.addWidget(version)
        h.addWidget(side)
        self.stack = QStackedWidget(); h.addWidget(self.stack,1)

    def set_status(self, text: str, busy: bool=False):
        self.status.setText(text); self.progress.setVisible(busy)

    def show_page(self, key: str):
        self.current_page = key
        while self.stack.count():
            w=self.stack.widget(0); self.stack.removeWidget(w); w.deleteLater()
        builder=getattr(self,f"page_{key}")
        page=builder(); self.stack.addWidget(page); self.stack.setCurrentWidget(page)
        for k,b in self.nav_buttons.items():
            b.setProperty("navActive", k==key); b.setProperty("nav", k!=key); b.style().unpolish(b); b.style().polish(b)

    def page_shell(self, title:str, subtitle:str="", actions:list[tuple[str,Callable,bool]]|None=None):
        page=QWidget(); outer=QVBoxLayout(page); outer.setContentsMargins(28,24,28,22); outer.setSpacing(14)
        top=QHBoxLayout(); left=QVBoxLayout(); t=QLabel(title); t.setObjectName("PageTitle"); left.addWidget(t)
        if subtitle:
            s=QLabel(subtitle); s.setObjectName("Muted"); left.addWidget(s)
        top.addLayout(left,1)
        if actions:
            for text,fn,accent in actions:
                b=QPushButton(text); b.setProperty("accent",accent); b.clicked.connect(fn); top.addWidget(b)
        outer.addLayout(top)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        inner=QWidget(); content=QVBoxLayout(inner); content.setContentsMargins(0,2,6,10); content.setSpacing(12); content.setAlignment(Qt.AlignTop)
        scroll.setWidget(inner); outer.addWidget(scroll,1)
        return page,content

    def card(self):
        f=QFrame(); f.setObjectName("Card"); f.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Minimum); f.setContentsMargins(0,0,0,0); return f

    def catalog_count(self):
        with self.db.connect() as con:
            total=con.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
            rated=con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
            cand=con.execute("SELECT COUNT(*) FROM movies m LEFT JOIN ratings r ON r.movie_id=m.id WHERE r.movie_id IS NULL").fetchone()[0]
        return total,rated,cand

    def page_today(self):
        page,content=self.page_shell("Azi",date.today().strftime("%d.%m.%Y"),[("Recalculează",lambda:self.show_page('today'),True)])
        events=self.s.calendar.relevant_events(date.today()); phase=self.s.calendar.season_phase(date.today())[0]
        c=self.card(); l=QVBoxLayout(c); ttl=QLabel(f"Contextul zilei: {phase}"); ttl.setObjectName("CardTitle"); l.addWidget(ttl)
        ev=QLabel(" • ".join(e.name for e,_ in events[:3]) if events else "Fără reper major; se folosește atmosfera sezonieră."); ev.setObjectName("Muted"); ev.setWordWrap(True); l.addWidget(ev); content.addWidget(c)
        total,rated,cand=self.catalog_count()
        if cand<=0:
            w=self.card(); wl=QVBoxLayout(w); h=QLabel("Catalogul de recomandări nu este încă pregătit"); h.setObjectName("CardTitle"); wl.addWidget(h)
            d=QLabel(f"Ai {rated:,} titluri evaluate. CineCalendar își poate construi singur catalogul oficial IMDb; nu introduci filme manual."); d.setObjectName("Muted"); d.setWordWrap(True); wl.addWidget(d)
            b=QPushButton("Pregătește catalogul automat"); b.setProperty("accent",True); b.clicked.connect(lambda:self.bootstrap_catalog(False)); wl.addWidget(b,alignment=Qt.AlignLeft); content.addWidget(w)
            content.addStretch(1); return page
        recs=self.s.recommender.recommend(date.today(),3,record=False)
        self.record_once(recs,date.today(),"today")
        if not recs:
            x=QLabel("Nu am găsit trei titluri eligibile după filtrele curente."); x.setObjectName("Muted"); content.addWidget(x)
        for i,r in enumerate(recs,1): content.addWidget(self.recommendation_card(r,i))
        content.addStretch(1); return page

    def record_once(self,recs,ctx,slot):
        now=datetime.now().astimezone().isoformat(timespec="seconds")
        with self.db.tx() as con:
            for r in recs:
                if not con.execute("SELECT 1 FROM recommendation_history WHERE movie_id=? AND context_date=? AND slot=?",(r.movie.id,ctx.isoformat(),slot)).fetchone():
                    con.execute("INSERT INTO recommendation_history(movie_id,recommended_at,context_date,slot,final_score) VALUES(?,?,?,?,?)",(r.movie.id,now,ctx.isoformat(),slot,r.score.final))

    def recommendation_card(self, rec:Recommendation, index:int|None=None):
        m,s=rec.movie,rec.score; box=self.card(); main=QHBoxLayout(box); main.setContentsMargins(16,16,16,16); main.setSpacing(16)
        poster=QLabel("Poster\nindisponibil"); poster.setObjectName("Muted"); poster.setAlignment(Qt.AlignCenter); poster.setFixedSize(126,184); poster.setStyleSheet("border-radius:10px; border:1px solid rgba(120,130,145,0.35);")
        main.addWidget(poster,0,Qt.AlignTop)
        if m.poster_url: self.load_poster_async(poster,m.poster_url,m.imdb_id or str(m.id))
        right=QVBoxLayout(); head=QHBoxLayout(); title=QLabel((f"{index}. " if index else "")+m.title+(f" ({m.year})" if m.year else "")); title.setObjectName("CardTitle"); title.setWordWrap(True); head.addWidget(title,1)
        score=QLabel(f"{round(s.final*100)}% potrivire"); score.setObjectName("Score"); head.addWidget(score,0,Qt.AlignRight); right.addLayout(head)
        meta=[]
        if m.imdb_rating is not None: meta.append(f"IMDb {m.imdb_rating:.1f}")
        if m.runtime_min: meta.append(f"{m.runtime_min} min")
        if m.genres: meta.append(", ".join(m.genres[:4]))
        if m.directors: meta.append("Regia: "+", ".join(m.directors[:2]))
        ml=QLabel("  •  ".join(meta) or "Metadate limitate"); ml.setObjectName("Muted"); ml.setWordWrap(True); right.addWidget(ml)
        p=QLabel("De ce pentru tine: "+s.personal_reason); p.setWordWrap(True); right.addWidget(p)
        now=QLabel("De ce acum: "+s.calendar_reason); now.setWordWrap(True); right.addWidget(now)
        tag=QLabel(f"Relevanță calendaristică: {s.calendar_kind}"); tag.setObjectName("Muted"); right.addWidget(tag)
        buttons=QGridLayout(); actions=[("Am văzut","seen"),("Vreau să văd","want_to_watch"),("Nu mă interesează","not_interested"),("Nu-mi recomanda similare","never_similar"),("Mai multe ca acesta","more_like_this"),("Mai puține ca acesta","less_like_this")]
        for n,(label,kind) in enumerate(actions):
            b=QPushButton(label); b.clicked.connect(lambda _,mid=m.id,k=kind:self.feedback(mid,k)); buttons.addWidget(b,n//3,n%3)
        exp=QPushButton("De ce mi-ai recomandat asta?"); exp.clicked.connect(lambda _,r=rec:ScoreDialog(r,self).exec()); buttons.addWidget(exp,2,0,1,2)
        if m.imdb_id:
            ib=QPushButton("Deschide IMDb"); ib.clicked.connect(lambda _,iid=m.imdb_id:QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))); buttons.addWidget(ib,2,2)
        right.addLayout(buttons); main.addLayout(right,1); return box

    def load_poster_async(self,label:QLabel,url:str,key:str):
        cache=self.s.paths.cache/"posters"; cache.mkdir(parents=True,exist_ok=True); path=cache/(hashlib.sha256((key+url).encode()).hexdigest()+".jpg")
        def fn(progress):
            if not path.exists():
                r=requests.get(url,timeout=15,headers={"User-Agent":"CineCalendar/1.0"}); r.raise_for_status(); path.write_bytes(r.content)
            return str(path)
        w=WorkerThread(fn,self); self.poster_threads.append(w)
        def done(p):
            pm=QPixmap(p)
            if not pm.isNull(): label.setPixmap(pm.scaled(label.size(),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)); label.setText("")
            if w in self.poster_threads:self.poster_threads.remove(w)
        w.success.connect(done); w.failure.connect(lambda _:None); w.start()

    def feedback(self,movie_id:int,kind:str):
        try:
            apply_feedback(self.db,movie_id,kind); self.set_status("Feedback salvat; profilul a fost recalculat.")
            self.show_page(self.current_page)
        except Exception as exc: QMessageBox.critical(self,"Feedback",str(exc))

    def page_calendar(self):
        page,content=self.page_shell("Calendar","Repere ortodoxe, seculare, istorice și sezoniere")
        for ev in self.s.calendar.events_for_year(date.today().year):
            if ev.end < date.today(): continue
            box=self.card(); l=QVBoxLayout(box); d=ev.start.strftime("%d.%m") if ev.start==ev.end else f"{ev.start:%d.%m}–{ev.end:%d.%m}"
            h=QLabel(f"{d}   {ev.name}"); h.setObjectName("CardTitle"); l.addWidget(h)
            s=QLabel(f"{ev.category} • importanță {ev.importance:.2f} • teme: {', '.join(ev.themes.keys())}"); s.setObjectName("Muted"); s.setWordWrap(True); l.addWidget(s); content.addWidget(box)
        content.addStretch(1); return page

    def page_month(self):
        page,content=self.page_shell("Programul lunii","Câte 3 filme pentru fiecare interval decis automat de CalendarEngine",[("Recalculează",lambda:self.show_page('month'),True)])
        if self.catalog_count()[2]<=0:
            x=QLabel("Catalogul nu este încă pregătit. CineCalendar îl poate construi automat din Setări."); x.setObjectName("Muted"); content.addWidget(x); return page
        for a,b,label,recs in self.s.recommender.month_program(date.today(),3,record=False):
            h=QLabel(f"{a:%d}–{b:%d.%m}  •  {label}"); h.setObjectName("CardTitle"); content.addWidget(h)
            for i,r in enumerate(recs,1): content.addWidget(self.recommendation_card(r,i))
        content.addStretch(1); return page

    def page_profile(self):
        p=get_profile(self.db); page,content=self.page_shell("Profilul meu",f"Ce a învățat algoritmul din {p.get('rated_count',0):,} ratinguri")
        summary=self.card(); l=QHBoxLayout(summary); rc=QLabel(f"{p.get('rated_count',0):,}\nratinguri analizate"); rc.setObjectName("CardTitle"); l.addWidget(rc)
        delta=p.get("mean_user_minus_imdb"); d=QLabel((f"{delta:+.2f}\nfață de IMDb" if delta is not None else "—\ndiferență IMDb")); d.setObjectName("CardTitle"); l.addWidget(d); l.addStretch(1); content.addWidget(summary)
        sections=[("Genuri","genre:"),("Teme","theme:"),("Regizori","director:"),("Decenii","decade:"),("Durată","runtime:"),("Popularitate","popularity:"),("Țări / cinematografii","country:")]
        for title,prefix in sections:
            h=QLabel(title); h.setObjectName("CardTitle"); content.addWidget(h)
            items=top_profile_features(p,prefix,True,12)
            box=self.card(); grid=QGridLayout(box)
            if not items:
                x=QLabel("Date insuficiente"); x.setObjectName("Muted"); grid.addWidget(x,0,0)
            for i,(name,st) in enumerate(items):
                clean=name.split(":",1)[1]; mean=st.get("mean_rating"); cnt=st.get("count",0); pref=st.get("preference",0)
                txt=f"{clean}   •   {mean:.2f}/10   •   {cnt} titluri   •   semnal {pref:+.2f}" if mean is not None else f"{clean}   •   semnal {pref:+.2f}"
                lab=QLabel(txt); grid.addWidget(lab,i,0)
            content.addWidget(box)
        content.addStretch(1); return page

    def page_ratings(self):
        page,content=self.page_shell("Ratinguri IMDb","Import, rating instant și detectarea automată a exporturilor",[("Import IMDb ratings.csv",self.import_ratings,True),("Adaugă rating",self.manual_rating,False)])
        total,rated,cand=self.catalog_count(); box=self.card(); l=QVBoxLayout(box)
        h=QLabel(f"{rated:,} ratinguri   •   {total:,} titluri în baza locală   •   {cand:,} candidați nevăzuți"); h.setObjectName("CardTitle"); l.addWidget(h)
        auto=QCheckBox("Detectează automat un export IMDb nou în folderul urmărit"); auto.setChecked(bool(self.db.get_setting("auto_watch_enabled",False))); auto.toggled.connect(lambda v:self.db.set_setting("auto_watch_enabled",bool(v))); l.addWidget(auto)
        folder=QLabel("Folder urmărit: "+str(self.db.get_setting("ratings_folder",str(Path.home()/"Downloads")))); folder.setObjectName("Muted"); l.addWidget(folder)
        scan=QPushButton("Scanează acum"); scan.clicked.connect(self.scan_ratings_folder); l.addWidget(scan,alignment=Qt.AlignLeft); content.addWidget(box)
        table=QTableWidget(0,4); table.setHorizontalHeaderLabels(["Data","Titlu","Rating","Sursă"]); table.setAlternatingRowColors(True); table.setEditTriggers(QTableWidget.NoEditTriggers); table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents); table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch); table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeToContents); table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        with self.db.connect() as con: rows=con.execute("SELECT r.date_rated,m.title,r.rating,r.source FROM ratings r JOIN movies m ON m.id=r.movie_id ORDER BY COALESCE(r.date_rated,'') DESC,r.id DESC LIMIT 500").fetchall()
        table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            for j,v in enumerate((r["date_rated"] or "",r["title"],r["rating"],r["source"])): table.setItem(i,j,QTableWidgetItem(str(v)))
        content.addWidget(table); return page

    def import_ratings(self):
        p,_=QFileDialog.getOpenFileName(self,"Import IMDb ratings.csv","","CSV (*.csv)")
        if not p:return
        try:
            r=import_imdb_csv(self.db,p); build_profile(self.db)
            if r.skipped_same_file: msg="Acest fișier a fost deja importat; nu l-am reimportat."
            else:
                details=[]
                details += [f"Rating nou detectat: {t} — {x}/10" for t,x in r.new_ratings[:8]]
                details += [f"Rating modificat: {t} — {a}/10 → {b}/10" for t,a,b in r.changed_ratings[:8]]
                msg=f"Import finalizat: {r.total_rows:,} ratinguri.\nNoi: {len(r.new_ratings)} • modificate: {len(r.changed_ratings)} • reconciliate: {r.merged_manual}."
                if details: msg += "\n\n"+"\n".join(details)
            QMessageBox.information(self,"IMDb",msg); self.set_status("Profil actualizat."); self.show_page("ratings"); QTimer.singleShot(400,self.auto_catalog_if_needed)
        except Exception as exc: QMessageBox.critical(self,"Import IMDb",str(exc))

    def manual_rating(self):
        d=ManualRatingDialog(self)
        if d.exec()!=QDialog.Accepted:return
        v=d.values()
        try:
            if not v["title"]: raise ValueError("Introdu un titlu.")
            add_manual_rating(self.db,**v); build_profile(self.db); self.set_status("Rating salvat; profil recalculat."); self.show_page("ratings")
        except Exception as exc: QMessageBox.critical(self,"Rating",str(exc))

    def scan_ratings_folder(self):
        if not self.db.get_setting("auto_watch_enabled",False): return
        try:
            w=RatingsFolderWatcher(self.db,self.db.get_setting("ratings_folder",str(Path.home()/"Downloads"))); results=w.scan()
            if results:
                build_profile(self.db); r=results[0]; self.set_status(f"Export IMDb nou importat: {len(r.new_ratings)} ratinguri noi, {len(r.changed_ratings)} modificate.")
                if self.current_page=="ratings": self.show_page("ratings")
        except Exception as exc: self.s.log.exception("ratings watcher failed"); self.set_status("Monitorizarea IMDb a întâmpinat o eroare.")

    def page_watchlist(self):
        page,content=self.page_shell("Watchlist","Filmele marcate «Vreau să văd»")
        with self.db.connect() as con: rows=con.execute("SELECT m.*,w.added_at FROM watchlist w JOIN movies m ON m.id=w.movie_id ORDER BY w.updated_at DESC").fetchall()
        if not rows:
            x=QLabel("Watchlist-ul este gol."); x.setObjectName("Muted"); content.addWidget(x)
        for r in rows:
            box=self.card(); l=QHBoxLayout(box); t=QLabel(r["title"]+(f" ({r['year']})" if r["year"] else "")); t.setObjectName("CardTitle"); l.addWidget(t,1); d=QLabel(r["added_at"][:10]); d.setObjectName("Muted"); l.addWidget(d); content.addWidget(box)
        content.addStretch(1); return page

    def page_history(self):
        page,content=self.page_shell("Istoric recomandări","Ce ți-a fost recomandat, când și ce ai făcut cu recomandarea")
        table=QTableWidget(0,5); table.setHorizontalHeaderLabels(["Data","Titlu","Context","Scor","Acțiune"]); table.setAlternatingRowColors(True); table.setEditTriggers(QTableWidget.NoEditTriggers); table.verticalHeader().setVisible(False); table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        with self.db.connect() as con: rows=con.execute("SELECT h.recommended_at,h.context_date,h.slot,h.final_score,h.action,m.title FROM recommendation_history h JOIN movies m ON m.id=h.movie_id ORDER BY h.id DESC LIMIT 1000").fetchall()
        table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            vals=(r["recommended_at"][:16].replace("T"," "),r["title"],r["slot"],f"{(r['final_score'] or 0)*100:.0f}%",r["action"] or ("ignorat" if r["context_date"]<date.today().isoformat() else "—"))
            for j,v in enumerate(vals): table.setItem(i,j,QTableWidgetItem(str(v)))
        content.addWidget(table); return page

    def page_settings(self):
        page,content=self.page_shell("Setări","Configurare, catalog, metadata și backup")
        general=self.card(); gl=QVBoxLayout(general); gh=QLabel("Preferințe"); gh.setObjectName("CardTitle"); gl.addWidget(gh)
        romance=QCheckBox("Exclude Romance (implicit ON)"); romance.setChecked(bool(self.db.get_setting("exclude_romance",True))); romance.toggled.connect(lambda v:self.db.set_setting("exclude_romance",bool(v))); gl.addWidget(romance)
        theme_row=QHBoxLayout(); theme_row.addWidget(QLabel("Temă")); combo=QComboBox(); combo.addItems(["dark","light"]); combo.setCurrentText(self.theme)
        def change_theme(v): self.theme=v; self.db.set_setting("theme",v); self.apply_theme()
        combo.currentTextChanged.connect(change_theme); theme_row.addWidget(combo); theme_row.addStretch(1); gl.addLayout(theme_row); content.addWidget(general)

        mon=self.card(); ml=QVBoxLayout(mon); mh=QLabel("Monitorizare export IMDb"); mh.setObjectName("CardTitle"); ml.addWidget(mh)
        folder=QLineEdit(str(self.db.get_setting("ratings_folder",str(Path.home()/"Downloads")))); ml.addWidget(folder)
        fr=QHBoxLayout(); choose=QPushButton("Alege folder"); save=QPushButton("Salvează folderul")
        def choose_folder():
            p=QFileDialog.getExistingDirectory(self,"Folder monitorizat",folder.text())
            if p: folder.setText(p)
        choose.clicked.connect(choose_folder); save.clicked.connect(lambda:self.db.set_setting("ratings_folder",folder.text().strip())); fr.addWidget(choose); fr.addWidget(save); fr.addStretch(1); ml.addLayout(fr); content.addWidget(mon)

        cat=self.card(); cl=QVBoxLayout(cat); ch=QLabel("Catalog de filme — automat"); ch.setObjectName("CardTitle"); cl.addWidget(ch)
        total,rated,cand=self.catalog_count(); info=QLabel(f"Catalog local: {total:,} titluri • evaluate: {rated:,} • candidați nevăzuți: {cand:,}. Catalogul se construiește automat din dataseturile oficiale IMDb."); info.setObjectName("Muted"); info.setWordWrap(True); cl.addWidget(info)
        cr=QHBoxLayout(); b1=QPushButton("Pregătește / repară catalogul"); b1.setProperty("accent",True); b1.clicked.connect(lambda:self.bootstrap_catalog(False)); b2=QPushButton("Actualizează de la IMDb"); b2.clicked.connect(lambda:self.bootstrap_catalog(True)); b3=QPushButton("Import manual dataset (avansat)"); b3.clicked.connect(self.import_imdb_dataset_dialog); cr.addWidget(b1); cr.addWidget(b2); cr.addWidget(b3); cr.addStretch(1); cl.addLayout(cr); content.addWidget(cat)

        tm=self.card(); tl=QVBoxLayout(tm); th=QLabel("TMDb — metadata semantică și postere"); th.setObjectName("CardTitle"); tl.addWidget(th)
        desc=QLabel("Opțional. Introdu propriul API Read Access Token pentru overview, keywords, țări, regizori și postere. Fără token, funcția rămâne dezactivată — nu este simulată."); desc.setObjectName("Muted"); desc.setWordWrap(True); tl.addWidget(desc)
        token=QLineEdit(str(self.db.get_setting("tmdb_token",""))); token.setEchoMode(QLineEdit.Password); token.setPlaceholderText("TMDb API Read Access Token"); tl.addWidget(token)
        tr=QHBoxLayout(); sv=QPushButton("Salvează token"); test=QPushButton("Testează"); enr=QPushButton("Îmbogățește 250 titluri"); sv.clicked.connect(lambda:self.db.set_setting("tmdb_token",token.text().strip())); test.clicked.connect(lambda:self.test_tmdb(token.text())); enr.clicked.connect(lambda:self.enrich_tmdb(token.text(),250)); tr.addWidget(sv); tr.addWidget(test); tr.addWidget(enr); tr.addStretch(1); tl.addLayout(tr); content.addWidget(tm)

        bk=self.card(); bl=QVBoxLayout(bk); bh=QLabel("Backup profil"); bh.setObjectName("CardTitle"); bl.addWidget(bh); br=QHBoxLayout(); e=QPushButton("Export profile"); i=QPushButton("Import profile"); e.clicked.connect(self.export_profile); i.clicked.connect(self.import_profile); br.addWidget(e); br.addWidget(i); br.addStretch(1); bl.addLayout(br); content.addWidget(bk)
        credits=self.card(); xl=QVBoxLayout(credits); xh=QLabel("Surse și transparență"); xh.setObjectName("CardTitle"); xl.addWidget(xh); x=QLabel("IMDb nu este screen-scrapat. Catalogul folosește dataseturile oficiale IMDb. TMDb este opțional și folosește API-ul oficial cu tokenul utilizatorului. Updaterul automat rămâne dezactivat până când există o implementare completă și verificată."); x.setObjectName("Muted"); x.setWordWrap(True); xl.addWidget(x); content.addWidget(credits)
        content.addStretch(1); return page

    def auto_catalog_if_needed(self):
        try:
            _,rated,cand=self.catalog_count()
            if rated>0 and cand<=0 and not self.db.get_setting("catalog_bootstrap_running",False): self.bootstrap_catalog(False,auto=True)
        except Exception: self.s.log.exception("auto catalog failed")

    def bootstrap_catalog(self,force=False,auto=False):
        if self.worker and self.worker.isRunning():
            self.set_status("Există deja o operație în curs.",True); return
        self.db.set_setting("catalog_bootstrap_running",True); self.set_status("Pregătesc catalogul oficial IMDb…",True)
        cache=self.s.paths.cache/"imdb_datasets"
        def fn(progress):
            r=bootstrap_official_imdb_catalog(self.db,cache,50,progress,force_download=force); build_profile(self.db); return r
        self.worker=WorkerThread(fn,self); self.worker.message.connect(lambda m:self.set_status(m,True))
        def done(r):
            self.db.set_setting("catalog_bootstrap_running",False); self.db.set_setting("catalog_initialized",True); self.set_status(f"Catalog gata: {r['movies']:,} titluri eligibile.",False)
            if not auto: QMessageBox.information(self,"Catalog CineCalendar",f"Catalog pregătit: {r['movies']:,} titluri eligibile.")
            self.show_page("today")
        def fail(e):
            self.db.set_setting("catalog_bootstrap_running",False); self.set_status("Catalogul automat a eșuat.",False); QMessageBox.critical(self,"Catalog CineCalendar","Nu am putut pregăti catalogul.\n\n"+e)
        self.worker.success.connect(done); self.worker.failure.connect(fail); self.worker.start()

    def import_imdb_dataset_dialog(self):
        basics,_=QFileDialog.getOpenFileName(self,"title.basics.tsv.gz","","GZip (*.gz)")
        if not basics:return
        ratings,_=QFileDialog.getOpenFileName(self,"title.ratings.tsv.gz","","GZip (*.gz)")
        if not ratings:return
        self.set_status("Import dataset IMDb…",True)
        def fn(progress):
            r=import_imdb_datasets(self.db,basics,ratings,50,progress); build_profile(self.db); return r
        self.worker=WorkerThread(fn,self); self.worker.message.connect(lambda m:self.set_status(m,True)); self.worker.success.connect(lambda r:(self.set_status(f"Importate {r['movies']:,} titluri.",False),QMessageBox.information(self,"IMDb",f"Importate {r['movies']:,} titluri."),self.show_page("settings"))); self.worker.failure.connect(lambda e:(self.set_status("Import eșuat.",False),QMessageBox.critical(self,"IMDb",e))); self.worker.start()

    def test_tmdb(self,token):
        token=(token or "").strip()
        if not token: QMessageBox.warning(self,"TMDb","Introdu tokenul TMDb."); return
        self.set_status("Testez conexiunea TMDb…",True)
        self.worker=WorkerThread(lambda progress:TmdbProvider(self.db,token).test_connection(),self); self.worker.success.connect(lambda _:(self.set_status("TMDb conectat.",False),QMessageBox.information(self,"TMDb","Conexiunea este validă."))); self.worker.failure.connect(lambda e:(self.set_status("TMDb: eroare.",False),QMessageBox.critical(self,"TMDb",e))); self.worker.start()

    def enrich_tmdb(self,token,limit):
        token=(token or "").strip()
        if not token: QMessageBox.warning(self,"TMDb","Introdu tokenul TMDb."); return
        self.db.set_setting("tmdb_token",token); self.set_status("Îmbogățire TMDb…",True)
        def fn(progress):
            r=enrich_library(self.db,token,limit,progress); build_profile(self.db); return r
        self.worker=WorkerThread(fn,self); self.worker.message.connect(lambda m:self.set_status(m,True)); self.worker.success.connect(lambda r:(self.set_status("Metadata TMDb actualizate.",False),QMessageBox.information(self,"TMDb",f"Procesate: {r['requested']}\nÎmbogățite: {r['enriched']}\nFără rezultat: {r['missing']}\nErori: {r['failed']}"))); self.worker.failure.connect(lambda e:(self.set_status("TMDb: eroare.",False),QMessageBox.critical(self,"TMDb",e))); self.worker.start()

    def export_profile(self):
        p,_=QFileDialog.getSaveFileName(self,"Export profile",f"CineCalendar-profile-{date.today().isoformat()}.zip","ZIP (*.zip)")
        if p:
            try: export_profile(self.db,p); QMessageBox.information(self,"Backup","Export finalizat.")
            except Exception as exc: QMessageBox.critical(self,"Backup",str(exc))

    def import_profile(self):
        p,_=QFileDialog.getOpenFileName(self,"Import profile","","ZIP (*.zip)")
        if p:
            try:
                r=import_profile(self.db,p); build_profile(self.db); QMessageBox.information(self,"Backup",f"Import/îmbinare finalizată: {r}"); self.show_page("settings")
            except Exception as exc: QMessageBox.critical(self,"Backup",str(exc))


def run_qt(service):
    app=QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("CineCalendar"); app.setOrganizationName("CineCalendar")
    try:
        app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    w=CineCalendarWindow(service); w.show(); return app.exec()
