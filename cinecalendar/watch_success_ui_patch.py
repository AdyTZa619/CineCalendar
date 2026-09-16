from __future__ import annotations

from datetime import date
from urllib.parse import quote_plus

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from .decision_action_patch import clear_today_choice, current_today_choice, set_today_choice
from .util import utcnow_iso


_WATCH_ACTIONS = {"trailer_opened", "stremio_opened", "playback_confirmed", "watched"}


def stremio_deep_link(imdb_id: str) -> str:
    iid = str(imdb_id or "").strip()
    return f"stremio:///detail/movie/{iid}/{iid}" if iid else ""


def stremio_web_link(imdb_id: str) -> str:
    iid = str(imdb_id or "").strip()
    return f"https://web.stremio.com/#/detail/movie/{iid}/{iid}" if iid else ""


def trailer_search_url(title: str, year: int | None = None) -> str:
    query = f"{title} {year or ''} official trailer".strip()
    return "https://www.youtube.com/results?search_query=" + quote_plus(query)


def record_watch_event(db, movie_id: int, action: str) -> int:
    action = str(action or "").strip()
    if action not in _WATCH_ACTIONS:
        raise ValueError(f"Unsupported watch event: {action}")
    movie_id = int(movie_id)
    now = utcnow_iso()
    today = date.today().isoformat()
    with db.tx() as con:
        movie = con.execute("SELECT id FROM movies WHERE id=?", (movie_id,)).fetchone()
        if movie is None:
            raise ValueError("Filmul nu mai există în catalog.")
        previous = con.execute(
            """SELECT final_score FROM recommendation_history
               WHERE movie_id=? ORDER BY id DESC LIMIT 1""",
            (movie_id,),
        ).fetchone()
        final_score = (
            float(previous["final_score"])
            if previous is not None and previous["final_score"] is not None
            else None
        )
        cur = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action
               ) VALUES(?,?,?,?,?,?,?)""",
            (movie_id, now, today, "watch_success_v3", final_score, 0, action),
        )
        return int(cur.lastrowid)


def _startability_reason(rec) -> str:
    for name, _pts, reason in rec.score.contributions:
        if name == "Startability" and reason:
            return str(reason)
    return ""


def _startability_label(value: float) -> str:
    score = float(value or 0.0)
    if score >= 0.76:
        return "ridicată"
    if score >= 0.64:
        return "bună"
    if score >= 0.52:
        return "medie"
    return "scăzută"


def install_watch_success_ui_patch(window_cls) -> None:
    """Turn Home into a short, truthful path from recommendation to confirmed playback."""
    original_page_today = window_cls.page_today
    original_human_reason = window_cls.human_reason

    def human_reason(self, rec):
        reason = _startability_reason(rec)
        if reason:
            return f"{reason} Estimarea pentru gustul tău rămâne {rec.score.predicted_rating:.1f}/10."
        return original_human_reason(self, rec)

    def open_trailer(self, movie):
        url = trailer_search_url(movie.title, movie.year)
        opened = QDesktopServices.openUrl(QUrl(url))
        if opened:
            try:
                record_watch_event(self.db, int(movie.id), "trailer_opened")
            except Exception:
                pass
            self.set_status("Am deschis căutarea pentru trailer. Este un semnal slab de interes, nu o vizionare.", False)
        else:
            QMessageBox.warning(self, "CineCalendar", "Nu am putut deschide browserul pentru trailer.")
        return bool(opened)

    def _open_stremio(self, movie, web_only: bool = False):
        iid = str(movie.imdb_id or "").strip()
        if not iid:
            QMessageBox.information(
                self,
                "CineCalendar",
                "Filmul nu are IMDb ID, deci nu îl pot deschide direct în Stremio.",
            )
            return False

        target = stremio_web_link(iid) if web_only else stremio_deep_link(iid)
        opened = QDesktopServices.openUrl(QUrl(target))
        if not opened and not web_only:
            # No desktop URL handler: use Stremio Web rather than leaving a dead button.
            opened = QDesktopServices.openUrl(QUrl(stremio_web_link(iid)))
        if not opened:
            QMessageBox.warning(self, "CineCalendar", "Nu am putut deschide Stremio sau Stremio Web.")
            return False

        try:
            # A successful URL handoff proves intent, not actual playback. Playback becomes a
            # strong signal only after the user explicitly confirms that the film really started.
            record_watch_event(self.db, int(movie.id), "stremio_opened")
            set_today_choice(self.db, int(movie.id))
        except Exception as exc:
            QMessageBox.warning(self, "CineCalendar", f"Am deschis Stremio, dar nu am putut salva semnalul local:\n{exc}")
        if hasattr(self, "today_result"):
            self.today_result = None
        self.set_status(
            "Am trimis filmul către Stremio. Asta nu înseamnă încă că a pornit; confirmă «A PORNIT FILMUL» dacă începe redarea.",
            False,
        )
        return True

    def watch_now(self, movie):
        opened = _open_stremio(self, movie, False)
        if opened:
            self.show_page("today")
        return opened

    def watch_now_web(self, movie):
        opened = _open_stremio(self, movie, True)
        if opened:
            self.show_page("today")
        return opened

    def confirm_playback(self, movie):
        try:
            record_watch_event(self.db, int(movie.id), "playback_confirmed")
            set_today_choice(self.db, int(movie.id))
            self.set_status("Am notat că filmul a pornit. Acesta este semnalul puternic de Watch Success.", False)
            if hasattr(self, "today_result"):
                self.today_result = None
            self.show_page("today")
            return True
        except Exception as exc:
            QMessageBox.warning(self, "CineCalendar", f"Nu am putut salva confirmarea de pornire:\n{exc}")
            return False

    def mark_watched_from_choice(self, movie):
        try:
            record_watch_event(self.db, int(movie.id), "watched")
        except Exception:
            pass
        # The Watch Success wrapper must clear the persisted choice too. Without this, the old
        # 2.7.0 screen could keep showing the same film even after "L-am văzut".
        clear_today_choice(self.db, int(movie.id))
        self.feedback(int(movie.id), "seen")
        self.show_page("today")

    def decision_hero(self, rec):
        m, s = rec.movie, rec.score
        box = QFrame(); box.setObjectName("HeroCard")
        main = QHBoxLayout(box); main.setContentsMargins(26, 26, 26, 26); main.setSpacing(28)
        poster = self.poster_label(222, 326)
        main.addWidget(poster, 0, Qt.AlignTop)
        if m.poster_url:
            self.load_poster_async(poster, m.poster_url, m.imdb_id or str(m.id))

        right = QVBoxLayout(); right.setSpacing(11)
        kicker = QLabel("RECOMANDAREA DE PORNIT ACUM")
        kicker.setObjectName("Kicker"); right.addWidget(kicker)
        title = QLabel(m.title + (f"  ({m.year})" if m.year else ""))
        title.setObjectName("HeroTitle"); title.setWordWrap(True); right.addWidget(title)

        metric = QHBoxLayout()
        metric.addWidget(self.score_badge(s.predicted_rating, "pentru tine"))
        metric.addWidget(self.metric_badge(f"{round(s.confidence * 100)}%", "încredere"))
        if float(getattr(s, "startability", 0.0) or 0.0) > 0:
            metric.addWidget(self.metric_badge(_startability_label(s.startability), "ușurință acum"))
        if m.imdb_rating is not None:
            metric.addWidget(self.metric_badge(f"{m.imdb_rating:.1f}", "IMDb"))
        metric.addStretch(1); right.addLayout(metric)

        chips = QHBoxLayout()
        for text in self.movie_chips(m):
            chips.addWidget(self.pill(text))
        chips.addStretch(1); right.addLayout(chips)

        overview = QLabel(self.overview_text(m))
        overview.setObjectName("Overview"); overview.setWordWrap(True); overview.setMaximumHeight(118)
        right.addWidget(overview)
        pitch = QLabel(human_reason(self, rec))
        pitch.setWordWrap(True); pitch.setObjectName("BodyStrong"); right.addWidget(pitch)
        if s.calendar_reason and s.calendar >= .48:
            now = QLabel("De ce acum: " + s.calendar_reason)
            now.setObjectName("Muted"); now.setWordWrap(True); right.addWidget(now)

        actions = QHBoxLayout()
        play = QPushButton("DESCHIDE ÎN STREMIO")
        play.setProperty("accent", True)
        play.clicked.connect(lambda _, movie=m: watch_now(self, movie))
        actions.addWidget(play)
        trailer = QPushButton("Trailer")
        trailer.clicked.connect(lambda _, movie=m: open_trailer(self, movie))
        actions.addWidget(trailer)
        other = QPushButton("Alt film")
        other.clicked.connect(lambda _, mid=m.id: self.skip_decision(mid))
        actions.addWidget(other)
        detail = QPushButton("Detalii")
        detail.clicked.connect(lambda _, r=rec: self.open_details(r))
        actions.addWidget(detail)
        actions.addStretch(1)
        right.addLayout(actions)

        keep = QPushButton("Îl păstrez pentru azi, fără să-l pornesc încă")
        keep.clicked.connect(lambda _, mid=m.id: self.choose_decision(mid))
        right.addWidget(keep, alignment=Qt.AlignLeft)
        main.addLayout(right, 1)
        return box

    def _render_watch_choice(self, movie):
        page, content = self.page_shell(
            "Ce văd acum?",
            "Ai ales filmul. CineCalendar separă acum deschiderea Stremio de pornirea reală a filmului.",
        )
        box = QFrame(); box.setObjectName("HeroCard")
        main = QHBoxLayout(box); main.setContentsMargins(26, 26, 26, 26); main.setSpacing(26)
        poster = self.poster_label(190, 278)
        main.addWidget(poster, 0, Qt.AlignTop)
        if movie.poster_url:
            self.load_poster_async(poster, movie.poster_url, movie.imdb_id or str(movie.id))

        right = QVBoxLayout(); right.setSpacing(11)
        kicker = QLabel("ALES PENTRU AZI")
        kicker.setObjectName("Kicker"); right.addWidget(kicker)
        title = QLabel(movie.title + (f"  ({movie.year})" if movie.year else ""))
        title.setObjectName("HeroTitle"); title.setWordWrap(True); right.addWidget(title)

        meta = []
        if movie.imdb_rating is not None: meta.append(f"IMDb {movie.imdb_rating:.1f}")
        if movie.runtime_min: meta.append(self.runtime_text(movie.runtime_min))
        if movie.genres: meta.append(", ".join(movie.genres[:4]))
        ml = QLabel("  •  ".join(meta) or "Alegere salvată")
        ml.setObjectName("Muted"); ml.setWordWrap(True); right.addWidget(ml)

        overview = QLabel(self.overview_text(movie))
        overview.setObjectName("Overview"); overview.setWordWrap(True); overview.setMaximumHeight(118)
        right.addWidget(overview)

        explain = QLabel(
            "Deschiderea Stremio înseamnă că ai încercat recomandarea, nu că redarea a început. "
            "Dacă filmul chiar pornește, apasă «A PORNIT FILMUL»; abia acela este semnalul puternic pentru motor."
        )
        explain.setObjectName("BodyStrong"); explain.setWordWrap(True); right.addWidget(explain)

        actions = QHBoxLayout()
        play = QPushButton("DESCHIDE ÎN STREMIO")
        play.setProperty("accent", True)
        play.clicked.connect(lambda _, movie=movie: watch_now(self, movie))
        actions.addWidget(play)
        started = QPushButton("A PORNIT FILMUL")
        started.clicked.connect(lambda _, movie=movie: confirm_playback(self, movie))
        actions.addWidget(started)
        trailer = QPushButton("Trailer")
        trailer.clicked.connect(lambda _, movie=movie: open_trailer(self, movie))
        actions.addWidget(trailer)
        web = QPushButton("Stremio Web")
        web.clicked.connect(lambda _, movie=movie: watch_now_web(self, movie))
        actions.addWidget(web)
        actions.addStretch(1); right.addLayout(actions)

        secondary = QHBoxLayout()
        change = QPushButton("Nu l-am pornit — alt film")
        change.clicked.connect(lambda _, mid=movie.id: self.skip_decision(mid))
        secondary.addWidget(change)
        seen = QPushButton("L-am văzut")
        seen.clicked.connect(lambda _, movie=movie: mark_watched_from_choice(self, movie))
        secondary.addWidget(seen)
        secondary.addStretch(1); right.addLayout(secondary)
        right.addStretch(1)
        main.addLayout(right, 1)
        content.addWidget(box); content.addStretch(1)
        return page

    def page_today(self):
        movie = current_today_choice(self.db)
        if movie is not None:
            return _render_watch_choice(self, movie)
        return original_page_today(self)

    window_cls.human_reason = human_reason
    window_cls.open_trailer = open_trailer
    window_cls.watch_now = watch_now
    window_cls.watch_now_web = watch_now_web
    window_cls.confirm_playback = confirm_playback
    window_cls.mark_watched_from_choice = mark_watched_from_choice
    window_cls.decision_hero = decision_hero
    window_cls.page_today = page_today
