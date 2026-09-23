from __future__ import annotations

from datetime import date
import re
from urllib.parse import quote_plus

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from .recommendation_outcomes_v42 import reconcile_recommendation_outcomes
from .decision_action_patch import (
    clear_today_choice,
    current_today_choice_state,
    set_today_choice,
)
from .trust_audit import (
    ensure_trust_audit_schema,
    resolve_exposure_history_id,
    validate_exposure_history_id,
)
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


def _safe_exposure(con, movie_id: int, today: str, exposure_history_id: int | None):
    if exposure_history_id is not None:
        row = validate_exposure_history_id(
            con,
            exposure_history_id,
            movie_id=int(movie_id),
        )
        if row is None:
            raise ValueError("Expunerea recomandării nu mai este validă pentru această acțiune.")
        return row
    resolved = resolve_exposure_history_id(con, int(movie_id), today)
    return validate_exposure_history_id(con, resolved, movie_id=int(movie_id)) if resolved is not None else None


def record_watch_event(
    db,
    movie_id: int,
    action: str,
    exposure_history_id: int | None = None,
) -> int:
    """Append a watch event linked to the concrete exposure when known.

    v3.3 never guesses between multiple same-day exposures and never mutates the exposure root.
    """
    action = str(action or "").strip()
    if action not in _WATCH_ACTIONS:
        raise ValueError(f"Unsupported watch event: {action}")
    movie_id = int(movie_id)
    now = utcnow_iso()
    today = date.today().isoformat()
    with db.tx() as con:
        ensure_trust_audit_schema(con)
        movie = con.execute("SELECT id FROM movies WHERE id=?", (movie_id,)).fetchone()
        if movie is None:
            raise ValueError("Filmul nu mai există în catalog.")

        root = _safe_exposure(con, movie_id, today, exposure_history_id)
        exposure_id = int(root["id"]) if root is not None else None
        event_context_date = str(root["context_date"]) if root is not None else today
        final_score = (
            float(root["final_score"])
            if root is not None and root["final_score"] is not None
            else None
        )

        cur = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action,exposure_history_id
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (movie_id, now, event_context_date, "watch_success_v3", final_score, 0, action, exposure_id),
        )
        event_id = int(cur.lastrowid)
    reconcile_recommendation_outcomes(db)
    return event_id


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


def _contribution_reason(rec, contribution_name: str) -> str:
    for name, _pts, reason in getattr(rec.score, "contributions", []) or []:
        if name == contribution_name and reason:
            return str(reason)
    return ""


def structured_watch_reason(rec) -> str:
    """One coherent, compact explanation for the decision card.

    The displayed rating is always the final blended value. Component estimates remain
    available in Details, but are never presented as a second competing final score here.
    """
    movie, score = rec.movie, rec.score
    lines = [
        f"Potrivire: {float(score.predicted_rating):.1f}/10 estimare finală • "
        f"{round(float(score.confidence) * 100)}% încredere."
    ]
    startability = float(getattr(score, "startability", 0.0) or 0.0)
    if startability > 0:
        facts = []
        if movie.runtime_min:
            facts.append(f"{int(movie.runtime_min)} min")
        if movie.overview:
            facts.append("premisă disponibilă")
        if movie.imdb_rating is not None and float(movie.imdb_rating) >= 7.0:
            facts.append("IMDb solid")
        suffix = f" — {', '.join(facts[:3])}" if facts else ""
        lines.append(f"Pentru acum: ușurință {_startability_label(startability)}{suffix}.")

    intent = _contribution_reason(rec, "Intenție de vizionare acum")
    match = re.search(r"Semnal de intenție de vizionare\s+([^;.,]+)", intent, re.IGNORECASE)
    if match and match.group(1).strip().lower() in {"scăzut", "scăzută"}:
        lines.append("Rezervă: intenția recentă de pornire este scăzută; contează doar la departajare.")
    return "\n".join(lines)


def _window_exposure(window, movie_id: int) -> int | None:
    state = current_today_choice_state(window.db)
    if state is not None and int(state.movie.id or 0) == int(movie_id):
        if state.exposure_history_id is not None:
            return int(state.exposure_history_id)
    mapping = getattr(window, "_visible_exposures", None)
    if isinstance(mapping, dict):
        try:
            value = mapping.get(int(movie_id))
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def install_watch_success_ui_patch(window_cls) -> None:
    """Turn Home into a truthful recommendation → attempt → confirmed playback funnel."""
    original_page_today = window_cls.page_today
    def refresh_guard(self):
        try:
            self.s.quality_manager.refresh_live_guard()
        except Exception as exc:
            self.s.log.warning("Live guard refresh after watch action failed: %s", exc)

    def human_reason(self, rec):
        return structured_watch_reason(rec)

    def open_trailer(self, movie):
        url = trailer_search_url(movie.title, movie.year)
        opened = QDesktopServices.openUrl(QUrl(url))
        if not opened:
            QMessageBox.warning(self, "CineCalendar", "Nu am putut deschide browserul pentru trailer.")
            return False
        exposure_id = _window_exposure(self, int(movie.id))
        try:
            record_watch_event(self.db, int(movie.id), "trailer_opened", exposure_id)
            refresh_guard(self)
            self.set_status(
                "Am deschis căutarea pentru trailer. Este un semnal slab de interes, nu o vizionare.",
                False,
            )
        except Exception as exc:
            self.set_status("Trailerul s-a deschis, dar semnalul local nu a fost salvat.", False)
            QMessageBox.warning(
                self,
                "CineCalendar",
                f"Trailerul s-a deschis, dar nu am putut salva evenimentul local:\n{exc}",
            )
        return True

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
            opened = QDesktopServices.openUrl(QUrl(stremio_web_link(iid)))
        if not opened:
            QMessageBox.warning(self, "CineCalendar", "Nu am putut deschide Stremio sau Stremio Web.")
            return False

        exposure_id = _window_exposure(self, int(movie.id))
        try:
            record_watch_event(self.db, int(movie.id), "stremio_opened", exposure_id)
            set_today_choice(self.db, int(movie.id), exposure_id)
            refresh_guard(self)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "CineCalendar",
                f"Am deschis Stremio, dar nu am putut salva semnalul local:\n{exc}",
            )
        if hasattr(self, "today_result"):
            self.today_result = None
        self.set_status(
            "Am trimis filmul către Stremio. Confirmă «A PORNIT FILMUL» numai dacă redarea chiar începe.",
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
        exposure_id = _window_exposure(self, int(movie.id))
        try:
            record_watch_event(self.db, int(movie.id), "playback_confirmed", exposure_id)
            set_today_choice(self.db, int(movie.id), exposure_id)
            refresh_guard(self)
            self.set_status(
                "Am notat că filmul a pornit. Acesta este semnalul puternic de Watch Success.",
                False,
            )
            if hasattr(self, "today_result"):
                self.today_result = None
            self.show_page("today")
            return True
        except Exception as exc:
            QMessageBox.warning(self, "CineCalendar", f"Nu am putut salva confirmarea de pornire:\n{exc}")
            return False

    def mark_watched_from_choice(self, movie):
        exposure_id = _window_exposure(self, int(movie.id))
        try:
            record_watch_event(self.db, int(movie.id), "watched", exposure_id)
            refresh_guard(self)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "CineCalendar",
                f"Nu am putut salva evenimentul «watched». Alegerea a rămas activă și nu am pierdut starea:\n{exc}",
            )
            return False

        clear_today_choice(self.db, int(movie.id))
        try:
            self.feedback(int(movie.id), "seen")
        except Exception as exc:
            QMessageBox.warning(
                self,
                "CineCalendar",
                f"Vizionarea a fost salvată, dar feedbackul derivat «seen» nu a putut fi recalculat:\n{exc}",
            )
            self.show_page("today")
        return True

    def decision_hero(self, rec):
        m, s = rec.movie, rec.score
        box = QFrame()
        box.setObjectName("HeroCard")
        main = QHBoxLayout(box)
        main.setContentsMargins(26, 26, 26, 26)
        main.setSpacing(28)
        poster = self.poster_label(222, 326)
        main.addWidget(poster, 0, Qt.AlignTop)
        if m.poster_url:
            self.load_poster_async(poster, m.poster_url, m.imdb_id or str(m.id))

        right = QVBoxLayout()
        right.setSpacing(11)
        kicker = QLabel("RECOMANDAREA DE PORNIT ACUM")
        kicker.setObjectName("Kicker")
        right.addWidget(kicker)
        title = QLabel(m.title + (f"  ({m.year})" if m.year else ""))
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        right.addWidget(title)

        metric = QHBoxLayout()
        metric.addWidget(self.score_badge(s.predicted_rating, "pentru tine"))
        metric.addWidget(self.metric_badge(f"{round(s.confidence * 100)}%", "încredere"))
        if float(getattr(s, "startability", 0.0) or 0.0) > 0:
            metric.addWidget(self.metric_badge(_startability_label(s.startability), "ușurință acum"))
        if m.imdb_rating is not None:
            metric.addWidget(self.metric_badge(f"{m.imdb_rating:.1f}", "IMDb"))
        metric.addStretch(1)
        right.addLayout(metric)

        chips = QHBoxLayout()
        for text in self.movie_chips(m):
            chips.addWidget(self.pill(text))
        chips.addStretch(1)
        right.addLayout(chips)

        overview = QLabel(self.overview_text(m))
        overview.setObjectName("Overview")
        overview.setWordWrap(True)
        overview.setMaximumHeight(118)
        right.addWidget(overview)
        pitch = QLabel(human_reason(self, rec))
        pitch.setWordWrap(True)
        pitch.setObjectName("BodyStrong")
        right.addWidget(pitch)
        if s.calendar_reason and s.calendar >= .48:
            now = QLabel("De ce acum: " + s.calendar_reason)
            now.setObjectName("Muted")
            now.setWordWrap(True)
            right.addWidget(now)

        actions = QHBoxLayout()
        play = QPushButton("DESCHIDE ÎN STREMIO")
        play.setProperty("accent", True)
        play.clicked.connect(lambda _, movie=m: watch_now(self, movie))
        actions.addWidget(play)
        trailer = QPushButton("Trailer")
        trailer.clicked.connect(lambda _, movie=m: open_trailer(self, movie))
        actions.addWidget(trailer)
        other = QPushButton("Alt film")
        other.clicked.connect(
            lambda _, mid=m.id, eid=getattr(rec, "exposure_history_id", None): self.skip_decision(mid, eid)
        )
        actions.addWidget(other)
        detail = QPushButton("Detalii")
        detail.clicked.connect(lambda _, r=rec: self.open_details(r))
        actions.addWidget(detail)
        actions.addStretch(1)
        right.addLayout(actions)

        keep = QPushButton("Îl păstrez pentru azi, fără să-l pornesc încă")
        keep.clicked.connect(
            lambda _, mid=m.id, eid=getattr(rec, "exposure_history_id", None): self.choose_decision(mid, eid)
        )
        right.addWidget(keep, alignment=Qt.AlignLeft)
        main.addLayout(right, 1)
        return box

    def _render_watch_choice(self, state):
        movie = state.movie
        page, content = self.page_shell(
            "Ce văd acum?",
            "Ai ales filmul. CineCalendar separă deschiderea Stremio de pornirea reală a filmului.",
        )
        box = QFrame()
        box.setObjectName("HeroCard")
        main = QHBoxLayout(box)
        main.setContentsMargins(26, 26, 26, 26)
        main.setSpacing(26)
        poster = self.poster_label(190, 278)
        main.addWidget(poster, 0, Qt.AlignTop)
        if movie.poster_url:
            self.load_poster_async(poster, movie.poster_url, movie.imdb_id or str(movie.id))

        right = QVBoxLayout()
        right.setSpacing(11)
        kicker = QLabel("ALES PENTRU AZI")
        kicker.setObjectName("Kicker")
        right.addWidget(kicker)
        title = QLabel(movie.title + (f"  ({movie.year})" if movie.year else ""))
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        right.addWidget(title)

        meta = []
        if movie.imdb_rating is not None:
            meta.append(f"IMDb {movie.imdb_rating:.1f}")
        if movie.runtime_min:
            meta.append(self.runtime_text(movie.runtime_min))
        if movie.genres:
            meta.append(", ".join(movie.genres[:4]))
        ml = QLabel("  •  ".join(meta) or "Alegere salvată")
        ml.setObjectName("Muted")
        ml.setWordWrap(True)
        right.addWidget(ml)

        explain = QLabel(
            "Deschiderea Stremio înseamnă doar încercare. «A PORNIT FILMUL» confirmă redarea; "
            "toate aceste evenimente rămân legate de aceeași expunere de recomandare."
        )
        explain.setObjectName("BodyStrong")
        explain.setWordWrap(True)
        right.addWidget(explain)

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
        actions.addStretch(1)
        right.addLayout(actions)

        secondary = QHBoxLayout()
        change = QPushButton("Nu l-am pornit — alt film")
        change.clicked.connect(
            lambda _, mid=movie.id, eid=state.exposure_history_id: self.skip_decision(mid, eid)
        )
        secondary.addWidget(change)
        seen = QPushButton("L-am văzut")
        seen.clicked.connect(lambda _, movie=movie: mark_watched_from_choice(self, movie))
        secondary.addWidget(seen)
        secondary.addStretch(1)
        right.addLayout(secondary)
        right.addStretch(1)
        main.addLayout(right, 1)
        content.addWidget(box)
        content.addStretch(1)
        return page

    def page_today(self):
        state = current_today_choice_state(self.db)
        if state is not None:
            return _render_watch_choice(self, state)
        return original_page_today(self)

    window_cls.human_reason = human_reason
    window_cls.open_trailer = open_trailer
    window_cls.watch_now = watch_now
    window_cls.watch_now_web = watch_now_web
    window_cls.confirm_playback = confirm_playback
    window_cls.mark_watched_from_choice = mark_watched_from_choice
    window_cls.decision_hero = decision_hero
    window_cls.page_today = page_today
