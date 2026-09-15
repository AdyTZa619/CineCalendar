from __future__ import annotations

import sys
from datetime import date
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout,
    QFrame, QSizePolicy, QMessageBox, QCheckBox
)

from . import __version__ as APP_VERSION
from .feedback import apply_feedback
from .qt_ui import CineCalendarWindow, ScoreDialog, WorkerThread
from .recommendation import Recommendation
from .updater import UpdateInfo, check_for_update, stage_and_start_update, update_supported


class DecisionWindow(CineCalendarWindow):
    """Decision-first UI with a safe, DDG-style self updater."""

    NAV = [
        ("today", "Ce văd acum?"),
        ("recommendations", "Recomandări"),
        ("profile", "Profilul meu"),
        ("ratings", "Ratinguri IMDb"),
        ("watchlist", "Watchlist"),
        ("calendar", "Calendar"),
        ("month", "Program calendar"),
        ("history", "Istoric"),
        ("updates", "Actualizări"),
        ("settings", "Setări"),
    ]

    def __init__(self, service):
        self.session_skips: set[int] = set()
        self.decision_mode = str(service.db.get_setting("decision_mode", "decide") or "decide")
        self.available_update: UpdateInfo | None = None
        self.update_worker: WorkerThread | None = None
        super().__init__(service)
        self.setWindowTitle(f"CineCalendar {APP_VERSION} — Decision Engine")
        if bool(self.db.get_setting("auto_update_check", True)):
            QTimer.singleShot(2800, lambda: self.check_updates(False))

    def _confidence_label(self, confidence: float) -> str:
        if confidence >= .82: return "încredere foarte mare"
        if confidence >= .68: return "încredere mare"
        if confidence >= .52: return "încredere medie"
        return "încredere redusă"

    def _mode_name(self, mode: str) -> str:
        return {
            "decide": "Automat",
            "safe": "Sigur",
            "surprise": "Surpriză",
            "short": "Scurt",
        }.get(mode, "Automat")

    def set_decision_mode(self, mode: str):
        self.decision_mode = mode
        self.db.set_setting("decision_mode", mode)
        self.show_page("today")

    def skip_decision(self, movie_id: int):
        self.session_skips.add(int(movie_id))
        try:
            with self.db.tx() as con:
                con.execute("""UPDATE recommendation_history
                    SET ignored=1, action='skip_today'
                    WHERE movie_id=? AND id=(SELECT id FROM recommendation_history WHERE movie_id=? ORDER BY id DESC LIMIT 1)""",
                    (movie_id, movie_id))
        except Exception:
            pass
        self.set_status("Am trecut peste el doar pentru sesiunea asta. Caut următorul.")
        self.show_page("today")

    def choose_decision(self, movie_id: int):
        try:
            with self.db.tx() as con:
                con.execute("""UPDATE recommendation_history
                    SET action='chosen'
                    WHERE movie_id=? AND id=(SELECT id FROM recommendation_history WHERE movie_id=? ORDER BY id DESC LIMIT 1)""",
                    (movie_id, movie_id))
            self.set_status("Alegerea a fost fixată. Gata cu căutatul.")
            QMessageBox.information(self, "CineCalendar", "Ăsta este filmul ales. Nu mai trebuie să compari alte liste.")
        except Exception as exc:
            QMessageBox.critical(self, "CineCalendar", str(exc))

    def page_today(self):
        page, content = self.page_shell(
            "Ce văd acum?",
            "Un singur răspuns, ales în principal din ratingurile tale. Calendarul este doar context secundar.",
        )
        total, rated, cand = self.catalog_count()
        if cand <= 0:
            w = self.card(); wl = QVBoxLayout(w)
            h = QLabel("Catalogul de recomandări nu este încă pregătit"); h.setObjectName("CardTitle"); wl.addWidget(h)
            d = QLabel(f"Ai {rated:,} titluri evaluate. CineCalendar își construiește singur catalogul IMDb; nu introduci filme manual.")
            d.setObjectName("Muted"); d.setWordWrap(True); wl.addWidget(d)
            b = QPushButton("Pregătește catalogul automat"); b.setProperty("accent", True); b.clicked.connect(lambda:self.bootstrap_catalog(False)); wl.addWidget(b, alignment=Qt.AlignLeft)
            content.addWidget(w); content.addStretch(1); return page

        primary, backups = self.s.recommender.decision_pick(date.today(), self.session_skips, self.decision_mode)
        if primary is None:
            x = QLabel("Nu am găsit un titlu eligibil după filtrele curente."); x.setObjectName("Muted"); content.addWidget(x); return page

        self.record_once([primary], date.today(), "decision")
        content.addWidget(self.decision_hero(primary))

        modes = self.card(); ml = QHBoxLayout(modes); ml.setContentsMargins(14,10,14,10)
        mode_text = QLabel(f"Mod: {self._mode_name(self.decision_mode)}")
        mode_text.setObjectName("Muted"); ml.addWidget(mode_text); ml.addStretch(1)
        for label, mode in [("Automat", "decide"), ("Mai sigur", "safe"), ("Surprinde-mă", "surprise"), ("Mai scurt", "short")]:
            b = QPushButton(label)
            if mode == self.decision_mode: b.setProperty("accent", True)
            b.clicked.connect(lambda _, m=mode:self.set_decision_mode(m)); ml.addWidget(b)
        content.addWidget(modes)

        if backups:
            h = QLabel("Rezerve — doar dacă primul chiar nu merge")
            h.setObjectName("CardTitle"); content.addWidget(h)
            grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
            for i, rec in enumerate(backups[:2]):
                grid.addWidget(self.backup_card(rec), 0, i)
            wrap = QFrame(); wrap.setLayout(grid); content.addWidget(wrap)

        content.addStretch(1)
        return page

    def decision_hero(self, rec: Recommendation):
        m, s = rec.movie, rec.score
        box = self.card(); main = QHBoxLayout(box); main.setContentsMargins(22,22,22,22); main.setSpacing(24)
        poster = QLabel("Poster\nindisponibil"); poster.setObjectName("Muted"); poster.setAlignment(Qt.AlignCenter)
        poster.setFixedSize(190, 278); poster.setStyleSheet("border-radius:14px; border:1px solid rgba(120,130,145,0.35);")
        main.addWidget(poster, 0, Qt.AlignTop)
        if m.poster_url: self.load_poster_async(poster, m.poster_url, m.imdb_id or str(m.id))

        right = QVBoxLayout(); right.setSpacing(10)
        badge = QLabel("ALEGEREA MEA PENTRU TINE"); badge.setStyleSheet("font-size:12px; font-weight:800; letter-spacing:1px;"); right.addWidget(badge)
        title = QLabel(m.title + (f" ({m.year})" if m.year else "")); title.setWordWrap(True); title.setStyleSheet("font-size:30px; font-weight:800;"); right.addWidget(title)
        prediction = QLabel(f"{s.predicted_rating:.1f}/10 estimat pentru tine")
        prediction.setObjectName("ScoreLarge"); right.addWidget(prediction)
        conf = QLabel(f"{round(s.confidence*100)}% încredere • {self._confidence_label(s.confidence)}")
        conf.setObjectName("Muted"); right.addWidget(conf)

        meta = []
        if m.imdb_rating is not None: meta.append(f"IMDb {m.imdb_rating:.1f}")
        if m.runtime_min: meta.append(f"{m.runtime_min} min")
        if m.genres: meta.append(", ".join(m.genres[:4]))
        if m.directors: meta.append("Regia: " + ", ".join(m.directors[:2]))
        ml = QLabel("  •  ".join(meta) or "Metadate limitate"); ml.setObjectName("Muted"); ml.setWordWrap(True); right.addWidget(ml)

        why = QLabel(s.personal_reason); why.setWordWrap(True); why.setStyleSheet("font-size:15px;"); right.addWidget(why)
        if s.calendar >= .55 and s.calendar_reason:
            cal = QLabel("Context bun acum: " + s.calendar_reason); cal.setObjectName("Muted"); cal.setWordWrap(True); right.addWidget(cal)

        actions = QHBoxLayout()
        choose = QPushButton("Aleg filmul ăsta"); choose.setProperty("accent", True); choose.clicked.connect(lambda _, mid=m.id:self.choose_decision(mid)); actions.addWidget(choose)
        other = QPushButton("Alt film"); other.clicked.connect(lambda _, mid=m.id:self.skip_decision(mid)); actions.addWidget(other)
        explain = QPushButton("De ce exact?"); explain.clicked.connect(lambda _, r=rec:ScoreDialog(r,self).exec()); actions.addWidget(explain)
        right.addLayout(actions)

        secondary = QHBoxLayout()
        if m.imdb_id:
            imdb = QPushButton("IMDb"); imdb.clicked.connect(lambda _, iid=m.imdb_id:QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))); secondary.addWidget(imdb)
        seen = QPushButton("L-am văzut deja"); seen.clicked.connect(lambda _, mid=m.id:self.feedback(mid,"seen")); secondary.addWidget(seen)
        more = QPushButton("Mai multe ca acesta"); more.clicked.connect(lambda _, mid=m.id:self.feedback(mid,"more_like_this")); secondary.addWidget(more)
        secondary.addStretch(1); right.addLayout(secondary)
        main.addLayout(right, 1)
        return box

    def backup_card(self, rec: Recommendation):
        m, s = rec.movie, rec.score
        box = self.card(); box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum); l = QVBoxLayout(box); l.setContentsMargins(15,15,15,15)
        t = QLabel(m.title + (f" ({m.year})" if m.year else "")); t.setObjectName("CardTitle"); t.setWordWrap(True); l.addWidget(t)
        p = QLabel(f"{s.predicted_rating:.1f}/10 pentru tine • {round(s.confidence*100)}% încredere"); p.setObjectName("Score"); l.addWidget(p)
        meta = []
        if m.runtime_min: meta.append(f"{m.runtime_min} min")
        if m.genres: meta.append(", ".join(m.genres[:3]))
        x = QLabel(" • ".join(meta)); x.setObjectName("Muted"); x.setWordWrap(True); l.addWidget(x)
        row = QHBoxLayout(); choose = QPushButton("Aleg rezerva"); choose.clicked.connect(lambda _, mid=m.id:self.choose_decision(mid)); row.addWidget(choose)
        why = QPushButton("De ce?"); why.clicked.connect(lambda _, r=rec:ScoreDialog(r,self).exec()); row.addWidget(why); row.addStretch(1); l.addLayout(row)
        return box

    def page_recommendations(self):
        page, content = self.page_shell(
            "Recomandări",
            "Clasamentul personal. Ratingurile tale conduc; IMDb și calendarul doar ajustează marginal.",
            [("Recalculează", lambda:self.show_page("recommendations"), True)],
        )
        if self.catalog_count()[2] <= 0:
            x = QLabel("Catalogul nu este încă pregătit."); x.setObjectName("Muted"); content.addWidget(x); return page

        info = self.card(); il = QVBoxLayout(info)
        h = QLabel("Motor rating-first"); h.setObjectName("CardTitle"); il.addWidget(h)
        t = QLabel("80% din scorul de bază vine din profilul tău de gust (rating estimat, afinitate cu filmele apreciate, regizori/cinematografii). Calendar + anotimp au doar 6% în recomandarea normală.")
        t.setObjectName("Muted"); t.setWordWrap(True); il.addWidget(t); content.addWidget(info)

        recs = self.s.recommender.recommend(date.today(), 12, record=False, slot="browse", candidate_limit=80000, mode="decide")
        grid = QGridLayout(); grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12)
        for i, rec in enumerate(recs):
            grid.addWidget(self.compact_recommendation_card(rec, i+1), i//2, i%2)
        wrap = QFrame(); wrap.setLayout(grid); content.addWidget(wrap); content.addStretch(1)
        return page

    def compact_recommendation_card(self, rec: Recommendation, index: int):
        m, s = rec.movie, rec.score
        box = self.card(); box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum); l = QVBoxLayout(box); l.setContentsMargins(15,15,15,15)
        head = QHBoxLayout(); title = QLabel(f"{index}. {m.title}" + (f" ({m.year})" if m.year else "")); title.setObjectName("CardTitle"); title.setWordWrap(True); head.addWidget(title,1)
        score = QLabel(f"{s.predicted_rating:.1f}/10"); score.setObjectName("Score"); head.addWidget(score); l.addLayout(head)
        meta = []
        if m.imdb_rating is not None: meta.append(f"IMDb {m.imdb_rating:.1f}")
        if m.runtime_min: meta.append(f"{m.runtime_min} min")
        if m.genres: meta.append(", ".join(m.genres[:3]))
        ml = QLabel(" • ".join(meta)); ml.setObjectName("Muted"); ml.setWordWrap(True); l.addWidget(ml)
        why = QLabel(s.personal_reason); why.setWordWrap(True); l.addWidget(why)
        row = QHBoxLayout(); exp = QPushButton("De ce?"); exp.clicked.connect(lambda _, r=rec:ScoreDialog(r,self).exec()); row.addWidget(exp)
        no = QPushButton("Nu mă interesează"); no.clicked.connect(lambda _, mid=m.id:self.feedback(mid,"not_interested")); row.addWidget(no)
        if m.imdb_id:
            ib = QPushButton("IMDb"); ib.clicked.connect(lambda _, iid=m.imdb_id:QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))); row.addWidget(ib)
        row.addStretch(1); l.addLayout(row)
        return box

    def page_updates(self):
        page, content = self.page_shell(
            "Actualizări",
            "Updater Stable cu SHA-256, backup, health-check și rollback automat.",
        )
        box = self.card(); l = QVBoxLayout(box); l.setContentsMargins(18,18,18,18); l.setSpacing(10)
        title = QLabel(f"CineCalendar {APP_VERSION}"); title.setObjectName("CardTitle"); l.addWidget(title)
        state = QLabel("Canal: Stable • " + ("updater automat disponibil" if update_supported() else "rulezi sursa Python; update automat doar în EXE"))
        state.setObjectName("Muted"); state.setWordWrap(True); l.addWidget(state)
        auto = QCheckBox("Verifică automat actualizările la pornire")
        auto.setChecked(bool(self.db.get_setting("auto_update_check", True)))
        auto.toggled.connect(lambda v:self.db.set_setting("auto_update_check", bool(v))); l.addWidget(auto)
        row = QHBoxLayout(); check = QPushButton("Caută actualizări"); check.clicked.connect(lambda:self.check_updates(True)); row.addWidget(check)
        if self.available_update:
            install = QPushButton(f"Actualizează la {self.available_update.version}"); install.setProperty("accent", True)
            install.clicked.connect(lambda:self.start_update(self.available_update, True)); row.addWidget(install)
        row.addStretch(1); l.addLayout(row); content.addWidget(box)
        if self.available_update:
            notes = self.card(); nl = QVBoxLayout(notes); nh = QLabel(f"Versiune nouă: {self.available_update.version}"); nh.setObjectName("CardTitle"); nl.addWidget(nh)
            txt = QLabel(self.available_update.notes or "Actualizare disponibilă."); txt.setWordWrap(True); nl.addWidget(txt); content.addWidget(notes)
        safety = self.card(); sl = QVBoxLayout(safety); sh = QLabel("Cum se aplică"); sh.setObjectName("CardTitle"); sl.addWidget(sh)
        st = QLabel("1. Descarcă EXE-ul nou. 2. Verifică SHA-256. 3. Creează backup al EXE-ului curent. 4. Închide aplicația și înlocuiește EXE-ul prin helper separat. 5. Pornește versiunea nouă și așteaptă health-check. 6. Dacă pornirea nu este confirmată, restaurează automat versiunea anterioară. Folderul CineCalendarData nu este înlocuit.")
        st.setObjectName("Muted"); st.setWordWrap(True); sl.addWidget(st); content.addWidget(safety); content.addStretch(1)
        return page

    def check_updates(self, manual: bool = False):
        if self.update_worker and self.update_worker.isRunning():
            if manual: self.set_status("Verificarea update-ului este deja în curs.")
            return
        if not update_supported():
            if manual: QMessageBox.information(self, "Actualizări", "Updaterul automat este activ în versiunea CineCalendar.exe pentru Windows.")
            return
        self.set_status("Verific actualizările…", True)
        worker = WorkerThread(lambda progress: check_for_update(APP_VERSION), self)
        self.update_worker = worker
        def success(info):
            self.update_worker = None; self.set_status("Pregătit", False)
            self.available_update = info
            if info is None:
                if manual: QMessageBox.information(self, "Actualizări", f"Ai deja cea mai nouă versiune: {APP_VERSION}.")
                if self.current_page == "updates": self.show_page("updates")
                return
            self.set_status(f"Actualizare {info.version} disponibilă.")
            if self.current_page == "updates": self.show_page("updates")
            already = str(self.db.get_setting("update_prompted_version", "") or "")
            if manual or already != info.version:
                self.db.set_setting("update_prompted_version", info.version)
                msg = f"CineCalendar {info.version} este disponibil.\n\n{info.notes or 'Actualizare nouă disponibilă.'}\n\nVrei să actualizezi acum?"
                if QMessageBox.question(self, "Actualizare disponibilă", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
                    self.start_update(info, False)
        def failure(message):
            self.update_worker = None; self.set_status("Verificarea update-ului a eșuat.", False)
            if manual: QMessageBox.warning(self, "Actualizări", f"Nu am putut verifica actualizările:\n{message}")
        worker.success.connect(success); worker.failure.connect(failure); worker.start()

    def start_update(self, info: UpdateInfo, confirm: bool = True):
        if not update_supported():
            QMessageBox.information(self, "Actualizări", "Updaterul automat funcționează numai din CineCalendar.exe pe Windows.")
            return
        if confirm:
            text = f"Instalez CineCalendar {info.version}?\n\nEXE-ul curent va fi păstrat ca backup până când noua versiune pornește corect."
            if QMessageBox.question(self, "Confirmă actualizarea", text, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
                return
        if self.update_worker and self.update_worker.isRunning():
            return
        self.set_status(f"Descarc CineCalendar {info.version}…", True)
        worker = WorkerThread(lambda progress: stage_and_start_update(info, self.s.paths.root, progress), self)
        self.update_worker = worker
        worker.message.connect(lambda m:self.set_status(m, True))
        def success(_req):
            self.set_status("Update verificat. Închid aplicația pentru instalare…", True)
            QTimer.singleShot(300, QApplication.instance().quit)
        def failure(message):
            self.update_worker = None; self.set_status("Actualizarea a eșuat; versiunea curentă nu a fost înlocuită.", False)
            QMessageBox.critical(self, "Actualizare eșuată", message)
        worker.success.connect(success); worker.failure.connect(failure); worker.start()


def run_qt(service, on_ready=None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("CineCalendar"); app.setOrganizationName("CineCalendar")
    try:
        app.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    w = DecisionWindow(service); w.show()
    if on_ready is not None:
        def ready():
            try: on_ready()
            except Exception as exc: service.log.exception("Post-update health marker failed: %s", exc)
        QTimer.singleShot(350, ready)
    return app.exec()
