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
from .recommendation import Recommendation, row_to_movie
from .updater import UpdateInfo, check_for_update, stage_and_start_update, update_supported
from .v5_alpha_runtime import is_v5_alpha
from .v5_evaluation import run_v5_evaluation
from .v5_knowledge import V5KnowledgeBase
from .v5_rating_snapshot import report_rating_freshness


class DecisionWindow(CineCalendarWindow):
    """Decision-first UI with a safe, DDG-style self updater."""

    NAV = [
        ("today", "Ce văd acum?"),
        ("recommendations", "Recomandări"),
        ("romanian_list", "Filme RO • Cronologie"),
        ("profile", "Profilul meu"),
        ("ratings", "Ratinguri IMDb"),
        ("watchlist", "Watchlist"),
        ("calendar", "Calendar"),
        ("month", "Program calendar"),
        ("history", "Istoric"),
        ("metadata_doctor", "Metadata Doctor"),
        ("updates", "Actualizări"),
        ("settings", "Setări"),
    ]

    def __init__(self, service):
        self.session_skips: set[int] = set()
        self.decision_mode = str(service.db.get_setting("decision_mode", "decide") or "decide")
        self.available_update: UpdateInfo | None = None
        self.update_worker: WorkerThread | None = None
        self.v5_eval_worker: WorkerThread | None = None
        self.NAV = list(type(self).NAV)
        if is_v5_alpha() and not any(key == "v5_lab" for key, _label in self.NAV):
            self.NAV.insert(-2, ("v5_lab", "V5 Lab"))
        super().__init__(service)
        alpha_mode = is_v5_alpha()
        self.setWindowTitle(f"CineCalendar {APP_VERSION} — Decision Engine")
        if bool(self.db.get_setting("auto_update_check", True)):
            QTimer.singleShot(2800, lambda: self.check_updates(False) if not self._ui_closing else None)

    def set_v5_trial_mode(self, mode: str):
        try:
            status = self.s.set_alpha_trial_mode(mode)
        except Exception as exc:
            QMessageBox.warning(self, "V5 Trial", str(exc))
            return
        label = "V5 20%" if status.get("mode") == "v5_20" else "V16"
        self.set_status(f"Trial vizibil: {label}. Schimbarea se aplică imediat recomandărilor următoare.", False)
        if self.current_page == "v5_lab":
            self.show_page("v5_lab")

    def _trial_audit_text(self, rec) -> str:
        factors = dict(getattr(rec.score, "score_factors", {}) or {})
        if "v5_trial_active" not in factors:
            return ""
        v16_rank = int(float(factors.get("v5_trial_v16_rank", -1) or -1))
        v5_rank = int(float(factors.get("v5_trial_v5_rank", -1) or -1))
        delta = factors.get("v5_trial_delta")
        mode = "V5 20%" if float(factors.get("v5_trial_active", 0.0) or 0.0) >= 0.5 else "V16"
        r16 = f"#{v16_rank}" if v16_rank > 0 else "în afara listei"
        rv5 = f"#{v5_rank}" if v5_rank > 0 else "în afara listei"
        delta_text = ""
        if delta is not None:
            try:
                delta_text = f" • Δ scor V5−V16 {float(delta):+.3f}"
            except (TypeError, ValueError):
                delta_text = ""
        return f"Trial {mode} • V16 {r16} • V5 {rv5}{delta_text}"

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

    def _clear_chosen_decision(self):
        self.db.set_setting("decision_chosen_date", "")
        self.db.set_setting("decision_chosen_movie_id", 0)

    def _chosen_movie_today(self):
        today = date.today().isoformat()
        chosen_date = str(self.db.get_setting("decision_chosen_date", "") or "")
        try:
            movie_id = int(self.db.get_setting("decision_chosen_movie_id", 0) or 0)
        except (TypeError, ValueError):
            movie_id = 0
        if chosen_date != today or movie_id <= 0:
            if chosen_date and chosen_date != today:
                self._clear_chosen_decision()
            return None
        with self.db.connect() as con:
            row = con.execute(
                """SELECT m.*,r.movie_id AS rated_movie_id
                   FROM movies m
                   LEFT JOIN ratings r ON r.movie_id=m.id
                   WHERE m.id=?""",
                (movie_id,),
            ).fetchone()
        if row is None or row["rated_movie_id"] is not None:
            self._clear_chosen_decision()
            return None
        return row_to_movie(row)

    def clear_chosen_decision(self):
        movie = self._chosen_movie_today()
        if movie is not None:
            try:
                with self.db.tx() as con:
                    con.execute(
                        """UPDATE recommendation_history
                           SET action='unselected'
                           WHERE movie_id=? AND context_date=? AND action='chosen'""",
                        (int(movie.id), date.today().isoformat()),
                    )
            except Exception:
                pass
        self._clear_chosen_decision()
        self.set_status("Alegerea pentru azi a fost eliberată.")
        self.show_page("today")

    def choose_decision(self, movie_id: int):
        try:
            today = date.today().isoformat()
            with self.db.tx() as con:
                con.execute(
                    """UPDATE recommendation_history
                       SET action='chosen'
                       WHERE movie_id=? AND id=(
                           SELECT id FROM recommendation_history
                           WHERE movie_id=? ORDER BY id DESC LIMIT 1
                       )""",
                    (movie_id, movie_id),
                )
            self.db.set_setting("decision_chosen_date", today)
            self.db.set_setting("decision_chosen_movie_id", int(movie_id))
            self.session_skips.discard(int(movie_id))
            self.set_status("Alegerea pentru azi este fixată.")
            self.show_page("today")
        except Exception as exc:
            QMessageBox.critical(self, "CineCalendar", str(exc))

    def feedback(self, movie_id: int, kind: str):
        if kind == "seen":
            try:
                if int(self.db.get_setting("decision_chosen_movie_id", 0) or 0) == int(movie_id):
                    self._clear_chosen_decision()
            except (TypeError, ValueError):
                self._clear_chosen_decision()
        super().feedback(movie_id, kind)

    def chosen_decision_card(self, movie):
        box = self.card()
        main = QHBoxLayout(box)
        main.setContentsMargins(22,22,22,22)
        main.setSpacing(24)

        poster = QLabel("Poster\nindisponibil")
        poster.setObjectName("Muted")
        poster.setAlignment(Qt.AlignCenter)
        poster.setFixedSize(190, 278)
        poster.setStyleSheet("border-radius:14px; border:1px solid rgba(120,130,145,0.35);")
        main.addWidget(poster, 0, Qt.AlignTop)
        if movie.poster_url:
            self.load_poster_async(poster, movie.poster_url, movie.imdb_id or str(movie.id))

        right = QVBoxLayout()
        right.setSpacing(10)
        badge = QLabel("ALEGERE FIXATĂ PENTRU AZI")
        badge.setObjectName("Kicker")
        right.addWidget(badge)

        title = QLabel(movie.title + (f" ({movie.year})" if movie.year else ""))
        title.setWordWrap(True)
        title.setStyleSheet("font-size:30px; font-weight:800;")
        right.addWidget(title)

        meta = []
        if movie.imdb_rating is not None:
            meta.append(f"IMDb {movie.imdb_rating:.1f}")
        if movie.runtime_min:
            meta.append(f"{movie.runtime_min} min")
        if movie.genres:
            meta.append(", ".join(movie.genres[:4]))
        info = QLabel(" • ".join(meta) or "Metadate limitate")
        info.setObjectName("Muted")
        info.setWordWrap(True)
        right.addWidget(info)

        note = QLabel("CineCalendar păstrează filmul ales până îl schimbi, îl marchezi văzut sau începe o zi nouă.")
        note.setWordWrap(True)
        right.addWidget(note)

        actions = QHBoxLayout()
        change = QPushButton("Alege alt film")
        change.clicked.connect(self.clear_chosen_decision)
        actions.addWidget(change)
        seen = QPushButton("L-am văzut deja")
        seen.clicked.connect(lambda _checked=False, mid=movie.id: self.feedback(mid, "seen"))
        actions.addWidget(seen)
        if movie.imdb_id:
            imdb = QPushButton("IMDb")
            imdb.clicked.connect(
                lambda _checked=False, iid=movie.imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))
            )
            actions.addWidget(imdb)
        actions.addStretch(1)
        right.addLayout(actions)
        right.addStretch(1)
        main.addLayout(right, 1)
        return box

    def page_today(self):
        page, content = self.page_shell(
            "Ce văd acum?",
            "Un singur răspuns, ales în principal din ratingurile tale. Calendarul este doar context secundar.",
        )
        chosen = self._chosen_movie_today()
        if chosen is not None:
            content.addWidget(self.chosen_decision_card(chosen))
            content.addStretch(1)
            return page

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
        trial_text = self._trial_audit_text(rec)
        if trial_text:
            trial = QLabel(trial_text); trial.setObjectName("Muted"); trial.setWordWrap(True); right.addWidget(trial)

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
        trial_text = self._trial_audit_text(rec)
        if trial_text:
            trial = QLabel(trial_text); trial.setObjectName("Muted"); trial.setWordWrap(True); l.addWidget(trial)
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
        trial_text = self._trial_audit_text(rec)
        if trial_text:
            trial = QLabel(trial_text); trial.setObjectName("Muted"); trial.setWordWrap(True); l.addWidget(trial)
        meta = []
        if m.imdb_rating is not None: meta.append(f"IMDb {m.imdb_rating:.1f}")
        if m.runtime_min: meta.append(f"{m.runtime_min} min")
        if m.genres: meta.append(", ".join(m.genres[:3]))
        ml = QLabel(" • ".join(meta)); ml.setObjectName("Muted"); ml.setWordWrap(True); l.addWidget(ml)
        why = QLabel(s.personal_reason); why.setWordWrap(True); l.addWidget(why)
        row = QHBoxLayout(); exp = QPushButton("De ce?"); exp.clicked.connect(lambda _, r=rec:ScoreDialog(r,self).exec()); row.addWidget(exp)
        no = QPushButton("Ascunde doar filmul"); no.clicked.connect(lambda _, mid=m.id:self.feedback(mid,"not_interested")); row.addWidget(no)
        if m.imdb_id:
            ib = QPushButton("IMDb"); ib.clicked.connect(lambda _, iid=m.imdb_id:QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))); row.addWidget(ib)
        row.addStretch(1); l.addLayout(row)
        return box

    def run_v5_lab_evaluation(self):
        if not is_v5_alpha():
            QMessageBox.information(self, "V5 Lab", "Evaluatorul V5 este disponibil numai în Alpha.")
            return
        if self.v5_eval_worker and self.v5_eval_worker.isRunning():
            self.set_status("Evaluarea V5 rulează deja.", True)
            return

        self.set_status("V5 Lab: pregătesc replay-ul V16 vs V5…", True)
        worker = WorkerThread(
            lambda progress: run_v5_evaluation(self.db, progress=progress),
            self,
        )
        self.v5_eval_worker = worker
        worker.message.connect(lambda message: self.set_status(message, True))

        def success(report):
            self.v5_eval_worker = None
            decision = dict((report or {}).get("decision") or {})
            if bool(decision.get("eligible_for_visible_alpha_trial")):
                self.set_status("V5 Lab: evaluare terminată; V5 poate intra într-un trial vizibil controlat.", False)
            else:
                self.set_status("V5 Lab: evaluare terminată; V5 rămâne shadow.", False)
            if self.current_page == "v5_lab":
                self.show_page("v5_lab")

        def failure(message):
            self.v5_eval_worker = None
            self.set_status("V5 Lab: evaluarea a eșuat; recomandările live nu au fost schimbate.", False)
            QMessageBox.warning(self, "V5 Lab", str(message))
            if self.current_page == "v5_lab":
                self.show_page("v5_lab")

        worker.success.connect(success)
        worker.failure.connect(failure)
        worker.start()

    @staticmethod
    def _v5_metric(value, *, percent=False):
        if value is None:
            return "—"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "—"
        return f"{number * 100:.1f}%" if percent else f"{number:+.4f}"

    def page_v5_lab(self):
        page, content = self.page_shell(
            "V5 Lab",
            "Comparație offline pe istoricul tău: V16 actual vs retrieval V5 vs V5 + ranker personal. "
            "Replay-ul nu modifică Stable și nu schimbă recomandările vizibile.",
            [("Rulează evaluarea V16 vs V5", self.run_v5_lab_evaluation, True)],
        )

        knowledge = V5KnowledgeBase(self.db).status()
        shadow = self.db.get_setting("v5_ranker_shadow_status", {}) or {}

        readiness = self.card(); rl = QVBoxLayout(readiness)
        rh = QLabel("Pregătirea modelului"); rh.setObjectName("CardTitle"); rl.addWidget(rh)
        ready = bool(knowledge.get("ready_for_rich_ranker"))
        rt = QLabel(
            (
                "Datele factuale sunt suficiente pentru evaluare."
                if ready else
                "Datele factuale nu au ajuns încă la pragul necesar."
            )
            + (
                f"  Shadow intern: AUC 8+ {shadow.get('like_auc', '—')} • "
                f"AUC 1–4 {shadow.get('dislike_auc', '—')} • "
                f"NDCG@25 {shadow.get('model_ndcg25', '—')} • "
                f"lift Top20 {shadow.get('top20_lift', '—')}."
                if isinstance(shadow, dict) and shadow.get("state") == "ready"
                else ""
            )
        )
        rt.setWordWrap(True); rt.setObjectName("Muted"); rl.addWidget(rt)
        content.addWidget(readiness)

        report = self.db.get_setting("v5_evaluation_report", {}) or {}
        if not isinstance(report, dict) or not report:
            empty = self.card(); el = QVBoxLayout(empty)
            eh = QLabel("Nicio evaluare completă încă"); eh.setObjectName("CardTitle"); el.addWidget(eh)
            et = QLabel(
                "Apasă butonul de mai sus. Evaluatorul folosește copii SQLite temporare pe rând, "
                "le șterge după fiecare fereastră și păstrează doar raportul final."
            )
            et.setWordWrap(True); et.setObjectName("Muted"); el.addWidget(et)
            content.addWidget(empty); content.addStretch(1)
            return page

        freshness = report_rating_freshness(self.db, report)
        generated = str(report.get("generated_at") or "dată necunoscută")
        history = self.card(); hl = QVBoxLayout(history)
        hh = QLabel("Istoricul evaluat"); hh.setObjectName("CardTitle"); hl.addWidget(hh)
        history_text = (
            "Ratingurile sunt aceleași ca în evaluare."
            if freshness is True else
            "Ratingurile s-au schimbat după evaluare; V5 revine la V16 până la o reevaluare."
            if freshness is False else
            "Raportul vechi nu conține amprenta ratingurilor; actualitatea lui nu poate fi confirmată."
        )
        hinfo = QLabel(f"Raport generat: {generated}. {history_text}")
        hinfo.setObjectName("Muted"); hinfo.setWordWrap(True); hl.addWidget(hinfo)
        content.addWidget(history)

        rolling = dict(report.get("rolling") or {})
        retrieval_agg = dict(((rolling.get("retrieval_only") or {}).get("aggregate") or {}))
        ranked_payload = rolling.get("selected_ranked") or rolling.get("ranked") or {}
        ranked_agg = dict((ranked_payload.get("aggregate") or {}))
        variants = dict(rolling.get("ranked_variants") or {})
        event = dict(report.get("event_replay") or {})
        baseline_event = dict(event.get("baseline") or {})
        ranked_event = dict(event.get("ranked") or {})
        event_guard = dict(event.get("ranked_guard") or {})
        decision = dict(report.get("decision") or {})
        selected_variant = str(
            decision.get("selected_variant")
            or rolling.get("selected_variant")
            or "learned"
        )

        summary = self.card(); sl = QVBoxLayout(summary)
        sh = QLabel("Replay temporal — sweep de pondere ranker"); sh.setObjectName("CardTitle"); sl.addWidget(sh)
        variant_lines = []
        for label, payload in variants.items():
            agg = dict((((payload or {}).get("comparison") or {}).get("aggregate") or {}))
            variant_lines.append(
                f"{label}: Δ {self._v5_metric(agg.get('mean_composite_delta'))} • "
                f"Δ NDCG@25 {self._v5_metric(agg.get('mean_ndcg25_delta'))} • "
                f"{agg.get('positive_folds', '—')}/{agg.get('fold_count', '—')} folduri + • "
                f"{'TRECUT' if agg.get('approved') else 'netrecut'}"
            )
        if not variant_lines:
            variant_lines.append(
                f"learned: Δ {self._v5_metric(ranked_agg.get('mean_composite_delta'))} • "
                f"Δ NDCG@25 {self._v5_metric(ranked_agg.get('mean_ndcg25_delta'))} • "
                f"{ranked_agg.get('positive_folds', '—')}/{ranked_agg.get('fold_count', '—')} folduri + • "
                f"{'TRECUT' if ranked_agg.get('approved') else 'netrecut'}"
            )
        st = QLabel(
            "V5 retrieval-only: "
            f"Δ compozit mediu {self._v5_metric(retrieval_agg.get('mean_composite_delta'))} • "
            f"folduri pozitive {retrieval_agg.get('positive_folds', '—')}/{retrieval_agg.get('fold_count', '—')} • "
            f"gate {'TRECUT' if retrieval_agg.get('approved') else 'netrecut'}\n"
            + "\n".join(variant_lines)
            + f"\nVariantă selectată pentru validarea externă: {selected_variant}."
        )
        st.setWordWrap(True); st.setObjectName("Muted"); sl.addWidget(st)
        content.addWidget(summary)

        events = self.card(); evl = QVBoxLayout(events)
        evh = QLabel("Replay pe zile reale — diagnostic extins"); evh.setObjectName("CardTitle"); evl.addWidget(evh)
        liked = int(baseline_event.get("liked_8_plus", 0) or 0)
        loved = int(baseline_event.get("loved_9_plus", 0) or 0)
        bad = int(baseline_event.get("disliked_4_minus", 0) or 0)
        evt = QLabel(
            f"Zile testate: {int((event.get('selection') or {}).get('window_count', 0) or 0)} • "
            f"ținte: {liked} filme 8+, {loved} filme 9+, {bad} filme 1–4\n"
            f"V16 candidate pool — 8+: {baseline_event.get('candidate_8_plus_hits', 0)}/{liked} "
            f"({self._v5_metric(baseline_event.get('candidate_8_plus_recall'), percent=True)}) • "
            f"9+: {baseline_event.get('candidate_9_plus_hits', 0)}/{loved} "
            f"({self._v5_metric(baseline_event.get('candidate_9_plus_recall'), percent=True)})\n"
            f"V5 candidate pool — 8+: {ranked_event.get('candidate_8_plus_hits', 0)}/{liked} "
            f"({self._v5_metric(ranked_event.get('candidate_8_plus_recall'), percent=True)}) • "
            f"9+: {ranked_event.get('candidate_9_plus_hits', 0)}/{loved} "
            f"({self._v5_metric(ranked_event.get('candidate_9_plus_recall'), percent=True)})\n"
            f"V16 Top25 — 8+: {baseline_event.get('top25_8_plus_hits', 0)}/{liked} "
            f"({self._v5_metric(baseline_event.get('top25_8_plus_recall'), percent=True)}) • "
            f"9+: {baseline_event.get('top25_9_plus_hits', 0)}/{loved} "
            f"({self._v5_metric(baseline_event.get('top25_9_plus_recall'), percent=True)}) • "
            f"1–4: {self._v5_metric(baseline_event.get('top25_dislike_rate'), percent=True)}\n"
            f"V5 {selected_variant} Top25 — 8+: {ranked_event.get('top25_8_plus_hits', 0)}/{liked} "
            f"({self._v5_metric(ranked_event.get('top25_8_plus_recall'), percent=True)}) • "
            f"9+: {ranked_event.get('top25_9_plus_hits', 0)}/{loved} "
            f"({self._v5_metric(ranked_event.get('top25_9_plus_recall'), percent=True)}) • "
            f"1–4: {self._v5_metric(ranked_event.get('top25_dislike_rate'), percent=True)}\n"
            f"Top50 8+: V16 {self._v5_metric(baseline_event.get('top50_8_plus_recall'), percent=True)} vs "
            f"V5 {self._v5_metric(ranked_event.get('top50_8_plus_recall'), percent=True)} • "
            f"NDCG@25 mediu: V16 {self._v5_metric(baseline_event.get('mean_ndcg25'), percent=True)} vs "
            f"V5 {self._v5_metric(ranked_event.get('mean_ndcg25'), percent=True)}\n"
            f"Gard extern: "
            f"{'TRECUT' if event_guard.get('passed') else ('NECONCLUDENT' if not event_guard.get('informative') else 'netrecut')} • "
            f"{event_guard.get('reason', '')}"
        )
        evt.setWordWrap(True); evt.setObjectName("Muted"); evl.addWidget(evt)
        content.addWidget(events)

        visible = dict(report.get("visible_decision_replay") or {})
        visible_guard = dict(visible.get("guard") or {})
        visible_box = self.card(); vbl = QVBoxLayout(visible_box)
        vbh = QLabel("Ce văd acum? — test pe cele trei opțiuni"); vbh.setObjectName("CardTitle"); vbl.addWidget(vbh)
        visible_text = QLabel(
            f"Ferestre istorice: {visible_guard.get('fold_count', 0)} • "
            f"V16: {(visible_guard.get('v16') or {}).get('liked_8_plus', 0)} filme 8+, "
            f"{(visible_guard.get('v16') or {}).get('disliked_4_minus', 0)} filme 1–4 • "
            f"V5: {(visible_guard.get('v5_20') or {}).get('liked_8_plus', 0)} filme 8+, "
            f"{(visible_guard.get('v5_20') or {}).get('disliked_4_minus', 0)} filme 1–4.\n"
            + (str(visible_guard.get("reason")) if visible_guard else
               "Raportul anterior nu a testat traseul «Ce văd acum?». Reevaluează pentru un verdict actual.")
        )
        visible_text.setWordWrap(True); visible_text.setObjectName("Muted"); vbl.addWidget(visible_text)
        content.addWidget(visible_box)

        verdict = self.card(); vl = QVBoxLayout(verdict)
        vh = QLabel("Decizie de siguranță pentru Alpha"); vh.setObjectName("CardTitle"); vl.addWidget(vh)
        eligible = bool(
            decision.get("eligible_for_visible_alpha_trial")
            and decision.get("visible_decision_guard_passed")
            and freshness is True
        )
        vt = QLabel(
            (
                "Eligibil pentru următorul pas: trial vizibil controlat în V5 Alpha."
                if eligible else
                "Rămâne în shadow mode. Nu activăm rankerul în recomandările vizibile."
            )
            + "\n"
            + ("Raportul trebuie refăcut pentru ratingurile actuale." if freshness is not True
               else str(decision.get("reason") or ""))
            + "\nStable 4.14.1 rămâne neatins."
        )
        vt.setWordWrap(True); vt.setObjectName("Muted"); vl.addWidget(vt)
        content.addWidget(verdict)

        trial_status = self.s.alpha_trial_status()
        trial_box = self.card(); tbl = QVBoxLayout(trial_box)
        th = QLabel("Trial vizibil V16 / V5"); th.setObjectName("CardTitle"); tbl.addWidget(th)
        active_mode = str(trial_status.get("mode") or "v16")
        eligible_trial = bool(trial_status.get("eligible"))
        try:
            with self.db.connect() as con:
                audit_rows = int(con.execute("SELECT COUNT(*) FROM v5_trial_audit").fetchone()[0])
        except Exception:
            audit_rows = 0
        tt = QLabel(
            (
                "Activ acum: V5 ranker 20%."
                if active_mode == "v5_20"
                else "Activ acum: V16."
            )
            + f"  Audit comparativ salvat: {audit_rows} recomandări."
            + ("  Poți comuta instant; Stable rămâne neatins." if eligible_trial
               else "  V5 necesită o reevaluare pe ratingurile actuale." if freshness is False
               else "  V5 rămâne blocat până la un raport eligibil.")
        )
        tt.setWordWrap(True); tt.setObjectName("Muted"); tbl.addWidget(tt)
        tr = QHBoxLayout()
        use_v16 = QPushButton("Folosește V16")
        use_v16.setProperty("accent", active_mode == "v16")
        use_v16.clicked.connect(lambda: self.set_v5_trial_mode("v16"))
        tr.addWidget(use_v16)
        use_v5 = QPushButton("Folosește V5 20%")
        use_v5.setProperty("accent", active_mode == "v5_20")
        use_v5.setEnabled(eligible_trial)
        use_v5.clicked.connect(lambda: self.set_v5_trial_mode("v5_20"))
        tr.addWidget(use_v5)
        tr.addStretch(1)
        tbl.addLayout(tr)
        content.addWidget(trial_box)

        content.addStretch(1)
        return page

    def page_updates(self):
        alpha_mode = is_v5_alpha()
        page, content = self.page_shell(
            "Actualizări",
            (
                "V5 Alpha este separat de canalul Stable."
                if alpha_mode else
                "Updater Stable cu SHA-256, backup, health-check și rollback automat."
            ),
        )
        box = self.card(); l = QVBoxLayout(box); l.setContentsMargins(18,18,18,18); l.setSpacing(10)
        title = QLabel(f"CineCalendar {APP_VERSION}"); title.setObjectName("CardTitle"); l.addWidget(title)
        if alpha_mode:
            state = QLabel(
                "Canal: V5 Alpha separat • " +
                ("updater automat disponibil; Stable rămâne neatins."
                 if update_supported() else
                 "rulezi sursa Python; update automat disponibil doar în EXE.")
            )
        else:
            state = QLabel("Canal: Stable • " + ("updater automat disponibil" if update_supported() else "rulezi sursa Python; update automat doar în EXE"))
        state.setObjectName("Muted"); state.setWordWrap(True); l.addWidget(state)
        auto = QCheckBox("Verifică automat actualizările la pornire")
        auto.setChecked(bool(self.db.get_setting("auto_update_check", True)))
        auto.setEnabled(True)
        auto.toggled.connect(lambda v:self.db.set_setting("auto_update_check", bool(v))); l.addWidget(auto)
        row = QHBoxLayout(); check = QPushButton("Caută actualizări"); check.clicked.connect(lambda:self.check_updates(True)); check.setEnabled(True); row.addWidget(check)
        if self.available_update:
            install = QPushButton(f"Actualizează la {self.available_update.version}"); install.setProperty("accent", True)
            install.clicked.connect(lambda:self.start_update(self.available_update, True)); row.addWidget(install)
        row.addStretch(1); l.addLayout(row); content.addWidget(box)
        if self.available_update:
            notes = self.card(); nl = QVBoxLayout(notes); nh = QLabel(f"Versiune nouă: {self.available_update.version}"); nh.setObjectName("CardTitle"); nl.addWidget(nh)
            txt = QLabel(self.available_update.notes or "Actualizare disponibilă."); txt.setWordWrap(True); nl.addWidget(txt); content.addWidget(notes)
        safety = self.card(); sl = QVBoxLayout(safety); sh = QLabel("Cum se aplică"); sh.setObjectName("CardTitle"); sl.addWidget(sh)
        protected_data = "CineCalendarV5AlphaData" if alpha_mode else "CineCalendarData"
        st = QLabel(f"1. Descarcă automat ZIP-ul canalului curent. 2. Verifică SHA-256. 3. Creează backup al aplicației. 4. Închide aplicația și înlocuiește bundle-ul prin helper separat. 5. Pornește versiunea nouă și așteaptă health-check. 6. Dacă pornirea nu este confirmată, restaurează automat versiunea anterioară. Folderul {protected_data} nu este înlocuit.")
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
