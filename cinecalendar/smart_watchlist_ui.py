from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from .qt_ui import WorkerThread
from .recommendation import row_to_movie
from .smart_watchlist import (
    pinned_watchlist_ids,
    rank_watchlist,
    remove_from_watchlist,
    set_watchlist_pinned,
    watchlist_entries,
)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child is not None:
            _clear_layout(child)


def install_smart_watchlist_ui(window_cls) -> None:
    """Replace the chronological Watchlist page with a production-ranked decision surface."""
    if getattr(window_cls, "_cinecalendar_smart_watchlist_v4", False):
        return

    def _loading_card(self):
        box = QFrame()
        box.setObjectName("HeroCard")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(24, 22, 24, 22)
        title = QLabel("Ordonez Watchlist-ul după gustul tău…")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        text = QLabel(
            "Folosesc același motor de producție ca la «Ce văd acum?»: ratingurile tale, "
            "ALS, preferințele adaptive, contextul și poarta de încredere."
        )
        text.setObjectName("Muted")
        text.setWordWrap(True)
        layout.addWidget(text)
        return box

    def _filters_card(self):
        box = QFrame()
        box.setObjectName("PremiumCard")
        row = QHBoxLayout(box)
        row.setContentsMargins(16, 12, 16, 12)

        row.addWidget(QLabel("Durată"))
        runtime = QComboBox()
        for label, key in (
            ("Orice", "all"),
            ("≤ 90 min", "short"),
            ("91–120 min", "medium"),
            ("> 120 min", "long"),
        ):
            runtime.addItem(label, key)
        wanted_runtime = str(self.db.get_setting("watchlist_runtime_filter", "all") or "all")
        idx = runtime.findData(wanted_runtime)
        runtime.setCurrentIndex(idx if idx >= 0 else 0)
        row.addWidget(runtime)

        row.addWidget(QLabel("Tip"))
        content_type = QComboBox()
        for label, key in (
            ("Toate", "all"),
            ("Filme", "movie"),
            ("Scurtmetraje", "short"),
            ("Documentare", "documentary"),
        ):
            content_type.addItem(label, key)
        wanted_type = str(self.db.get_setting("watchlist_type_filter", "all") or "all")
        idx = content_type.findData(wanted_type)
        content_type.setCurrentIndex(idx if idx >= 0 else 0)
        row.addWidget(content_type)
        row.addStretch(1)

        def changed():
            self.db.set_setting("watchlist_runtime_filter", str(runtime.currentData() or "all"))
            self.db.set_setting("watchlist_type_filter", str(content_type.currentData() or "all"))
            self.show_page("watchlist")

        runtime.currentIndexChanged.connect(lambda _i: changed())
        content_type.currentIndexChanged.connect(lambda _i: changed())
        return box

    def _toggle_pin(self, movie_id: int):
        ids = pinned_watchlist_ids(self.db)
        new_state = int(movie_id) not in ids
        set_watchlist_pinned(self.db, int(movie_id), new_state)
        self.set_status(
            "Marcat «Vreau să-l văd curând»." if new_state else "Prioritatea din Watchlist a fost scoasă.",
            False,
        )
        self.show_page("watchlist")

    def _remove(self, movie_id: int):
        try:
            removed = remove_from_watchlist(self.db, int(movie_id))
            self.set_status(
                "Filmul a fost scos din Watchlist fără feedback negativ."
                if removed else "Filmul nu mai era în Watchlist.",
                False,
            )
            self.show_page("watchlist")
        except Exception as exc:
            QMessageBox.warning(self, "Watchlist", f"Nu am putut scoate filmul din Watchlist:\n{exc}")

    def _rank_card(self, rec, index: int):
        movie, score = rec.movie, rec.score
        box = QFrame()
        box.setObjectName("PremiumCard")
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        main = QHBoxLayout(box)
        main.setContentsMargins(16, 16, 16, 16)
        main.setSpacing(15)

        poster = self.poster_label(100, 150)
        main.addWidget(poster, 0, Qt.AlignTop)
        if movie.poster_url:
            self.load_poster_async(poster, movie.poster_url, movie.imdb_id or str(movie.id))

        right = QVBoxLayout()
        right.setSpacing(7)
        head = QHBoxLayout()
        display_title = movie.original_title or movie.title
        title = QLabel(f"{index}. {display_title}" + (f" ({movie.year})" if movie.year else ""))
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        predicted = QLabel(f"{score.predicted_rating:.1f}/10")
        predicted.setObjectName("Score")
        head.addWidget(predicted)
        right.addLayout(head)

        meta = []
        if movie.runtime_min:
            meta.append(self.runtime_text(movie.runtime_min))
        if movie.genres:
            meta.append(", ".join(movie.genres[:3]))
        if movie.imdb_rating is not None:
            meta.append(f"IMDb {movie.imdb_rating:.1f}")
        meta_label = QLabel(" • ".join(meta) or "Metadate limitate")
        meta_label.setObjectName("Muted")
        meta_label.setWordWrap(True)
        right.addWidget(meta_label)

        reason_fn = getattr(self, "human_reason", None)
        reason = reason_fn(rec) if callable(reason_fn) else (score.personal_reason or "")
        why = QLabel(reason)
        why.setWordWrap(True)
        why.setMaximumHeight(62)
        right.addWidget(why)

        if score.calendar_reason and float(score.calendar or 0.0) >= 0.30:
            now = QLabel("Acum: " + score.calendar_reason)
            now.setObjectName("Muted")
            now.setWordWrap(True)
            now.setMaximumHeight(44)
            right.addWidget(now)

        actions = QHBoxLayout()
        choose = QPushButton("Aleg pentru azi")
        choose.setProperty("accent", index == 1)
        choose.clicked.connect(
            lambda _checked=False, mid=movie.id, r=rec:
            self.choose_decision(int(mid), getattr(r, "exposure_history_id", None))
        )
        actions.addWidget(choose)

        details = QPushButton("Detalii")
        details.clicked.connect(lambda _checked=False, r=rec: self.open_details(r))
        actions.addWidget(details)

        if callable(getattr(self, "watch_now", None)):
            play = QPushButton("Stremio")
            play.clicked.connect(lambda _checked=False, m=movie: self.watch_now(m))
            actions.addWidget(play)

        pinned = int(movie.id) in pinned_watchlist_ids(self.db)
        pin = QPushButton("Prioritar" if not pinned else "Prioritar ✓")
        pin.clicked.connect(lambda _checked=False, mid=movie.id: _toggle_pin(self, int(mid)))
        actions.addWidget(pin)

        remove = QPushButton("Scoate")
        remove.clicked.connect(lambda _checked=False, mid=movie.id: _remove(self, int(mid)))
        actions.addWidget(remove)
        actions.addStretch(1)
        right.addLayout(actions)

        main.addLayout(right, 1)
        return box

    def _plain_card(self, row: dict):
        movie = row_to_movie(row)
        box = QFrame()
        box.setObjectName("PremiumCard")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(15, 12, 15, 12)

        text = (movie.original_title or movie.title) + (f" ({movie.year})" if movie.year else "")
        if row.get("is_rated"):
            text += "  •  deja evaluat"
        title = QLabel(text)
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        layout.addWidget(title, 1)

        added = str(row.get("watchlist_added_at") or "")[:10]
        if added:
            date_label = QLabel("adăugat " + added)
            date_label.setObjectName("Muted")
            layout.addWidget(date_label)

        pinned = int(movie.id) in pinned_watchlist_ids(self.db)
        pin = QPushButton("Prioritar ✓" if pinned else "Prioritar")
        pin.clicked.connect(lambda _checked=False, mid=movie.id: _toggle_pin(self, int(mid)))
        layout.addWidget(pin)

        remove = QPushButton("Scoate")
        remove.clicked.connect(lambda _checked=False, mid=movie.id: _remove(self, int(mid)))
        layout.addWidget(remove)
        return box

    def _render_result(self, result):
        content = getattr(self, "smart_watchlist_content", None)
        if content is None or self.current_page != "watchlist":
            return
        _clear_layout(content)
        content.addWidget(_filters_card(self))

        summary = QFrame()
        summary.setObjectName("HeroCard")
        sl = QVBoxLayout(summary)
        sl.setContentsMargins(22, 20, 22, 20)
        h = QLabel("Watchlist inteligent")
        h.setObjectName("SectionTitle")
        sl.addWidget(h)
        facts = [
            f"{result.total} filme salvate",
            f"{result.scored} cu scor personal calculabil",
        ]
        if result.future_hidden:
            facts.append(f"{result.future_hidden} cu lansare viitoare ascunse din Top 5")
        info = QLabel(" • ".join(facts))
        info.setObjectName("Muted")
        info.setWordWrap(True)
        sl.addWidget(info)
        content.addWidget(summary)

        recs = list(result.recommendations)
        if recs:
            self.record_once(recs, date.today(), "watchlist_next")
            quick = QHBoxLayout()
            choose_now = QPushButton("Alege-mi unul acum")
            choose_now.setProperty("accent", True)
            first = recs[0]
            choose_now.clicked.connect(
                lambda _checked=False, mid=first.movie.id, r=first:
                self.choose_decision(int(mid), getattr(r, "exposure_history_id", None))
            )
            quick.addWidget(choose_now)
            quick.addStretch(1)
            sl.addLayout(quick)
            title = QLabel("Următoarele 5")
            title.setObjectName("SectionTitle")
            content.addWidget(title)
            subtitle = QLabel(
                "Ordinea se recalculează din Watchlist-ul tău; nu este un top IMDb și nu "
                "introduce un al doilea motor de recomandări."
            )
            subtitle.setObjectName("Muted")
            subtitle.setWordWrap(True)
            content.addWidget(subtitle)

            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(14)
            for i, rec in enumerate(recs, 1):
                grid.addWidget(_rank_card(self, rec, i), (i - 1) // 2, (i - 1) % 2)
            wrap = QFrame()
            wrap.setLayout(grid)
            content.addWidget(wrap)
        else:
            empty = QLabel(
                "Nu există momentan suficiente filme eligibile din Watchlist pentru un Top 5 personal."
            )
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            content.addWidget(empty)

        all_rows = watchlist_entries(self.db)
        top_ids = {int(rec.movie.id) for rec in recs}
        remaining = [row for row in all_rows if int(row["id"]) not in top_ids]
        if remaining:
            title = QLabel("Restul Watchlist-ului")
            title.setObjectName("SectionTitle")
            content.addWidget(title)
            for row in remaining:
                content.addWidget(_plain_card(self, row))

        content.addStretch(1)

    def _render_failure(self, message: str):
        content = getattr(self, "smart_watchlist_content", None)
        if content is None or self.current_page != "watchlist":
            return
        _clear_layout(content)
        content.addWidget(_filters_card(self))
        warning = QFrame()
        warning.setObjectName("PremiumCard")
        wl = QVBoxLayout(warning)
        wl.setContentsMargins(20, 18, 20, 18)
        h = QLabel("Clasarea inteligentă nu a putut fi calculată")
        h.setObjectName("SectionTitle")
        wl.addWidget(h)
        detail = QLabel(str(message))
        detail.setObjectName("Muted")
        detail.setWordWrap(True)
        wl.addWidget(detail)
        content.addWidget(warning)

        for row in watchlist_entries(self.db):
            content.addWidget(_plain_card(self, row))
        content.addStretch(1)

    def page_watchlist(self):
        page, content = self.page_shell(
            "Watchlist",
            "Filmele salvate de tine, cu o coadă «Următoarele 5» ordonată de motorul personal.",
        )
        self.smart_watchlist_content = content
        content.addWidget(_filters_card(self))
        content.addWidget(_loading_card(self))

        worker = getattr(self, "smart_watchlist_worker", None)
        if worker is not None and worker.isRunning():
            return page

        worker = WorkerThread(
            lambda progress: rank_watchlist(
                self.s.recommender,
                date.today(),
                5,
                getattr(self, "decision_mode", "decide"),
                runtime_bucket=str(self.db.get_setting("watchlist_runtime_filter", "all") or "all"),
                content_type=str(self.db.get_setting("watchlist_type_filter", "all") or "all"),
            ),
            self,
        )
        self.smart_watchlist_worker = worker

        def success(result):
            self.smart_watchlist_worker = None
            self.smart_watchlist_result = result
            _render_result(self, result)

        def failure(message):
            self.smart_watchlist_worker = None
            _render_failure(self, message)

        worker.success.connect(success)
        worker.failure.connect(failure)
        worker.start()
        return page

    window_cls.page_watchlist = page_watchlist
    window_cls._cinecalendar_smart_watchlist_v4 = True
