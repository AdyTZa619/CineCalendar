from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from .recommendation import row_to_movie
from .util import utcnow_iso


CHOICE_SETTING = "chosen_for_today_v1"
_VALID_ACTIONS = {"chosen", "skip_today"}


def record_decision_action(db, movie_id: int, action: str) -> int:
    """Persist an explicit choose/skip at the moment the user clicks it.

    CineCalendar 2.6.0 only changed the `action` column on an older recommendation-history row.
    Watch Intent then used that row's *recommendation* timestamp as if it were the interaction
    timestamp. It also made a repeated action on the same row invisible to the state token.

    Reuse a fresh, action-less exposure row when possible; otherwise append a compact explicit
    event row. This keeps the existing schema/backward compatibility while giving Watch Intent a
    real timestamp and a new row id whenever a second explicit action happens.
    """
    action = str(action or "").strip()
    if action not in _VALID_ACTIONS:
        raise ValueError(f"Unsupported decision action: {action}")
    movie_id = int(movie_id)
    now = utcnow_iso()
    today = date.today().isoformat()
    ignored = 1 if action == "skip_today" else 0

    with db.tx() as con:
        movie = con.execute("SELECT id FROM movies WHERE id=?", (movie_id,)).fetchone()
        if movie is None:
            raise ValueError("Filmul nu mai există în catalog.")

        row = con.execute(
            """SELECT id,slot,final_score,action
               FROM recommendation_history
               WHERE movie_id=? ORDER BY id DESC LIMIT 1""",
            (movie_id,),
        ).fetchone()

        # A normal freshly displayed recommendation has action=NULL. Turn that exposure into the
        # explicit event and refresh its timestamp to the actual click time.
        if row is not None and not str(row["action"] or "").strip():
            con.execute(
                """UPDATE recommendation_history
                   SET action=?, ignored=?, recommended_at=?, context_date=?
                   WHERE id=?""",
                (action, ignored, now, today, int(row["id"])),
            )
            return int(row["id"])

        # If the latest row already contains an action (for example chosen -> changed mind), keep
        # both signals. The new id also invalidates Watch Intent's cached state immediately.
        final_score = float(row["final_score"]) if row is not None and row["final_score"] is not None else None
        cur = con.execute(
            """INSERT INTO recommendation_history(
                   movie_id,recommended_at,context_date,slot,final_score,ignored,action
               ) VALUES(?,?,?,?,?,?,?)""",
            (movie_id, now, today, "decision_action", final_score, ignored, action),
        )
        return int(cur.lastrowid)


def set_today_choice(db, movie_id: int) -> None:
    db.set_setting(
        CHOICE_SETTING,
        {"date": date.today().isoformat(), "movie_id": int(movie_id), "chosen_at": utcnow_iso()},
    )


def clear_today_choice(db, movie_id: int | None = None) -> None:
    current = db.get_setting(CHOICE_SETTING, {})
    if movie_id is not None and isinstance(current, dict):
        try:
            if int(current.get("movie_id") or 0) != int(movie_id):
                return
        except (TypeError, ValueError):
            return
    db.set_setting(CHOICE_SETTING, {})


def current_today_choice(db):
    payload = db.get_setting(CHOICE_SETTING, {})
    if not isinstance(payload, dict) or str(payload.get("date") or "") != date.today().isoformat():
        return None
    try:
        movie_id = int(payload.get("movie_id") or 0)
    except (TypeError, ValueError):
        return None
    if movie_id <= 0:
        return None
    with db.connect() as con:
        row = con.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    return row_to_movie(row) if row is not None else None


def install_decision_action_patch(window_cls) -> None:
    """Make Choose Film a real, visible state transition and timestamp intent correctly."""
    original_page_today = window_cls.page_today

    def _render_chosen_today(self, movie):
        page, content = self.page_shell(
            "Ce văd acum?",
            "Alegerea este confirmată pentru azi. CineCalendar a salvat și semnalul pentru Watch Intent.",
        )
        box = QFrame()
        box.setObjectName("HeroCard")
        main = QHBoxLayout(box)
        main.setContentsMargins(26, 26, 26, 26)
        main.setSpacing(26)

        if hasattr(self, "poster_label"):
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
            meta.append(f"{movie.runtime_min} min")
        if movie.genres:
            meta.append(", ".join(movie.genres[:4]))
        label = QLabel("  •  ".join(meta) or "Alegere salvată")
        label.setObjectName("Muted")
        label.setWordWrap(True)
        right.addWidget(label)

        confirm = QLabel(
            "Filmul a fost înregistrat ca alegere explicită. Acest semnal este pozitiv pentru "
            "intenția de vizionare, separat de nota ta estimată 1–10."
        )
        confirm.setObjectName("BodyStrong")
        confirm.setWordWrap(True)
        right.addWidget(confirm)

        actions = QHBoxLayout()
        if movie.imdb_id:
            imdb = QPushButton("Deschide IMDb")
            imdb.setProperty("accent", True)
            imdb.clicked.connect(
                lambda _, iid=movie.imdb_id: QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))
            )
            actions.addWidget(imdb)
        change = QPushButton("M-am răzgândit — alt film")
        change.clicked.connect(lambda _, mid=movie.id: self.skip_decision(mid))
        actions.addWidget(change)
        seen = QPushButton("L-am văzut")

        def mark_seen(_checked=False, mid=movie.id):
            clear_today_choice(self.db, mid)
            self.feedback(mid, "seen")
            self.show_page("today")

        seen.clicked.connect(mark_seen)
        actions.addWidget(seen)
        actions.addStretch(1)
        right.addLayout(actions)
        right.addStretch(1)
        main.addLayout(right, 1)
        content.addWidget(box)
        content.addStretch(1)
        return page

    def page_today(self):
        movie = current_today_choice(self.db)
        if movie is not None:
            return _render_chosen_today(self, movie)
        return original_page_today(self)

    def choose_decision(self, movie_id: int):
        try:
            record_decision_action(self.db, int(movie_id), "chosen")
            set_today_choice(self.db, int(movie_id))
            # Prevent a metadata worker finishing a moment later from repainting the old chooser
            # over the confirmed-choice page.
            if hasattr(self, "today_result"):
                self.today_result = None
            self.set_status("Filmul a fost ales pentru azi și semnalul a fost salvat.", False)

            modal = QApplication.activeModalWidget()
            if isinstance(modal, QDialog) and modal is not self:
                modal.accept()
            self.show_page("today")
        except Exception as exc:
            QMessageBox.critical(self, "CineCalendar", f"Nu am putut salva alegerea:\n{exc}")

    def skip_decision(self, movie_id: int):
        movie_id = int(movie_id)
        try:
            self.session_skips.add(movie_id)
            record_decision_action(self.db, movie_id, "skip_today")
            clear_today_choice(self.db, movie_id)
            if hasattr(self, "today_result"):
                self.today_result = None
            self.set_status("Am trecut peste el pentru moment. Caut următorul film.", False)
            self.show_page("today")
        except Exception as exc:
            QMessageBox.critical(self, "CineCalendar", f"Nu am putut salva schimbarea:\n{exc}")

    window_cls.page_today = page_today
    window_cls.choose_decision = choose_decision
    window_cls.skip_decision = skip_decision
