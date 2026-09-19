from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget, QLineEdit, QProgressBar,
)

from .qt_ui import WorkerThread
from .romanian_films import (
    backfill_romanian_posters,
    display_title,
    romanian_chapters,
    romanian_films,
)


def install_romanian_list_ui_patch(window_cls) -> None:
    """Replace the legacy Word-table rendering with a streaming-library style browser."""
    if getattr(window_cls, "_romanian_list_ui_patch_installed", False):
        return

    def _short(text: str, limit: int = 58) -> str:
        text = " ".join(str(text or "").split())
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rsplit(" ", 1)[0] + "…"

    def _filter_button(self, label: str, value: str, mode: str, count: int):
        button = QPushButton(f"{label}  {count}")
        button.setProperty("accent", value == mode)
        button.clicked.connect(lambda _checked=False, v=value: _set_filter(self, v))
        return button

    def _set_filter(self, value: str):
        self.db.set_setting("romanian_list_filter", value)
        self.show_page("romanian_list")

    def _set_search(self, field: QLineEdit):
        self.db.set_setting("romanian_list_search", field.text().strip())
        self.show_page("romanian_list")

    def _clear_search(self):
        self.db.set_setting("romanian_list_search", "")
        self.show_page("romanian_list")

    def _matches_search(item, query: str) -> bool:
        if not query:
            return True
        haystack = " ".join(
            (
                display_title(item.film),
                item.period or "",
                item.season or "",
                item.context or "",
            )
        ).casefold()
        tokens = [x for x in query.casefold().split() if x]
        return all(token in haystack for token in tokens)

    def _poster_failed(self, item):
        if not item.local_movie_id or not item.poster_url:
            return
        failed_ids = getattr(self, "_romanian_broken_poster_ids", set())
        if int(item.local_movie_id) in failed_ids:
            return
        failed_ids.add(int(item.local_movie_id))
        self._romanian_broken_poster_ids = failed_ids
        try:
            with self.db.tx() as con:
                con.execute(
                    "UPDATE movies SET poster_url=NULL WHERE id=? AND poster_url=?",
                    (int(item.local_movie_id), item.poster_url),
                )
        except Exception:
            return
        self._romanian_assets_attempted_session = False
        self.set_status("Un poster nu s-a încărcat; caut automat o sursă alternativă.", False)
        QTimer.singleShot(350, lambda: _auto_fill_posters(self))

    def _auto_fill_posters(self):
        worker = getattr(self, "romanian_assets_worker", None)
        if worker is not None and worker.isRunning():
            return
        if getattr(self, "_romanian_assets_attempted_session", False):
            return
        self._romanian_assets_attempted_session = True

        current = romanian_films(self.db)
        missing = [
            item for item in current
            if item.local_movie_id and item.imdb_id and not item.poster_url
        ]
        if not missing:
            return

        self.set_status(f"Completez automat posterele… {len(missing)} lipsă", True)
        worker = WorkerThread(
            lambda progress: backfill_romanian_posters(
                self.db,
                fallback_limit=48,
                progress=progress,
            ),
            self,
        )
        self.romanian_assets_worker = worker
        worker.message.connect(lambda message: self.set_status(message, True))

        def done(result):
            self.romanian_assets_worker = None
            gained = int(result.get("filled", 0) or 0) + int(result.get("fallback", 0) or 0)
            remaining = int(result.get("remaining", 0) or 0)
            if gained:
                self.set_status(
                    f"Postere completate automat: {gained}. Mai lipsesc {remaining} dintre titlurile identificate.",
                    False,
                )
                if self.current_page == "romanian_list":
                    self.show_page("romanian_list")
            else:
                self.set_status(
                    "Posterele disponibile automat au fost verificate; unele titluri nu au imagine în sursele deschise.",
                    False,
                )

        def failed(message):
            self.romanian_assets_worker = None
            self.set_status(
                "Nu am putut completa toate posterele acum; lista rămâne utilizabilă și se reîncearcă la următoarea pornire.",
                False,
            )
            self.s.log.warning("Romanian poster backfill failed: %s", message)

        worker.success.connect(done)
        worker.failure.connect(failed)
        worker.start()

    def _detail_dialog(self, item):
        dialog = QDialog(self)
        dialog.setWindowTitle(display_title(item.film))
        dialog.resize(760, 620)
        root = QVBoxLayout(dialog)
        root.setContentsMargins(26, 24, 26, 24)
        root.setSpacing(14)

        kicker = QLabel("FILME ROMÂNEȘTI • CRONOLOGIE")
        kicker.setObjectName("Kicker")
        root.addWidget(kicker)

        title = QLabel(display_title(item.film))
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        root.addWidget(title)

        badges = QHBoxLayout()
        state = QLabel(
            f"VĂZUT • {item.user_rating}/10"
            if item.watched and item.user_rating
            else ("VĂZUT" if item.watched else "DE VĂZUT")
        )
        state.setObjectName("Pill")
        badges.addWidget(state)
        if item.imdb_rating is not None:
            imdb_score = QLabel(f"IMDb {item.imdb_rating:.1f}")
            imdb_score.setObjectName("Pill")
            badges.addWidget(imdb_score)
        if item.season and "neconfirm" not in item.season.lower():
            season = QLabel(item.season)
            season.setObjectName("Pill")
            badges.addWidget(season)
        badges.addStretch(1)
        root.addLayout(badges)

        period_head = QLabel("Perioada acțiunii")
        period_head.setObjectName("SectionTitle")
        root.addWidget(period_head)
        period = QLabel(item.period or "Neconfirmat")
        period.setWordWrap(True)
        period.setObjectName("BodyStrong")
        root.addWidget(period)

        context_head = QLabel("Context")
        context_head.setObjectName("SectionTitle")
        root.addWidget(context_head)
        context = QLabel(item.context or "—")
        context.setWordWrap(True)
        context.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(context)

        source_head = QLabel("Certitudine / sursă")
        source_head.setObjectName("SectionTitle")
        root.addWidget(source_head)
        source = QLabel(item.certainty_source or "—")
        source.setObjectName("Muted")
        source.setWordWrap(True)
        source.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(source)

        root.addStretch(1)
        actions = QHBoxLayout()
        if item.imdb_id:
            imdb = QPushButton("Deschide IMDb")
            imdb.clicked.connect(
                lambda _checked=False, iid=item.imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))
            )
            actions.addWidget(imdb)
        actions.addStretch(1)
        close = QPushButton("Închide")
        close.setProperty("accent", True)
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _poster_card(self, item):
        card = QFrame()
        card.setObjectName("PremiumCard")
        card.setFixedWidth(198)
        card.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(8)

        state = QLabel(
            f"VĂZUT  {item.user_rating}/10"
            if item.watched and item.user_rating
            else ("VĂZUT" if item.watched else "DE VĂZUT")
        )
        state.setObjectName("Kicker" if not item.watched else "Score")
        state.setAlignment(Qt.AlignLeft)
        layout.addWidget(state)

        if hasattr(self, "poster_label"):
            poster = self.poster_label(176, 258)
        else:
            poster = QLabel()
            poster.setFixedSize(176, 258)
            poster.setAlignment(Qt.AlignCenter)
            poster.setObjectName("Muted")
        if item.poster_url and hasattr(self, "load_poster_async"):
            self.load_poster_async(
                poster,
                item.poster_url,
                item.imdb_id or str(item.local_movie_id or item.film),
                lambda _message, x=item: _poster_failed(self, x),
            )
        else:
            placeholder = display_title(item.film)
            if len(placeholder) > 42:
                placeholder = _short(placeholder, 42)
            poster.setText("CINECALENDAR\n\n" + placeholder)
            poster.setWordWrap(True)
            poster.setAlignment(Qt.AlignCenter)
        layout.addWidget(poster, alignment=Qt.AlignHCenter)

        title = QLabel(display_title(item.film))
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        title.setFixedHeight(46)
        layout.addWidget(title)

        period = QLabel(_short(item.period or "Perioadă neconfirmată", 48))
        period.setObjectName("Muted")
        period.setWordWrap(True)
        period.setFixedHeight(38)
        period.setToolTip(item.period or "")
        layout.addWidget(period)

        meta_bits = []
        if item.release_year:
            meta_bits.append(str(item.release_year))
        if item.imdb_rating is not None:
            meta_bits.append(f"IMDb {item.imdb_rating:.1f}")
        if item.season and "neconfirm" not in item.season.lower():
            meta_bits.append(_short(item.season, 22))
        if meta_bits:
            meta = QLabel(" • ".join(meta_bits))
            meta.setObjectName("Muted")
            meta.setWordWrap(True)
            layout.addWidget(meta)

        row = QHBoxLayout()
        details = QPushButton("Detalii")
        details.clicked.connect(lambda _checked=False, x=item: _detail_dialog(self, x))
        row.addWidget(details)
        if item.imdb_id:
            imdb = QPushButton("IMDb")
            imdb.clicked.connect(
                lambda _checked=False, iid=item.imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))
            )
            row.addWidget(imdb)
        layout.addLayout(row)
        return card

    def _next_up_hero(self, item):
        box = QFrame()
        box.setObjectName("DetailHero")
        main = QHBoxLayout(box)
        main.setContentsMargins(22, 20, 22, 20)
        main.setSpacing(22)

        if hasattr(self, "poster_label"):
            poster = self.poster_label(150, 220)
        else:
            poster = QLabel()
            poster.setFixedSize(150, 220)
            poster.setAlignment(Qt.AlignCenter)
            poster.setObjectName("Muted")
        if item.poster_url and hasattr(self, "load_poster_async"):
            self.load_poster_async(poster, item.poster_url, item.imdb_id or str(item.local_movie_id or item.film))
        else:
            poster.setText("CINECALENDAR\n\n" + _short(display_title(item.film), 34))
            poster.setWordWrap(True)
        main.addWidget(poster, 0, Qt.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(9)
        kicker = QLabel("URMĂTORUL CRONOLOGIC")
        kicker.setObjectName("Kicker")
        right.addWidget(kicker)

        title = QLabel(display_title(item.film))
        title.setObjectName("HeroTitle")
        title.setWordWrap(True)
        right.addWidget(title)

        period = QLabel(item.period or "Perioadă neconfirmată")
        period.setObjectName("BodyStrong")
        period.setWordWrap(True)
        right.addWidget(period)

        if item.context:
            context = QLabel(_short(item.context, 240))
            context.setObjectName("Muted")
            context.setWordWrap(True)
            right.addWidget(context)

        chips = QHBoxLayout()
        state = QLabel("DE VĂZUT")
        state.setObjectName("Pill")
        chips.addWidget(state)
        if item.imdb_rating is not None:
            score = QLabel(f"IMDb {item.imdb_rating:.1f}")
            score.setObjectName("Pill")
            chips.addWidget(score)
        if item.season and "neconfirm" not in item.season.lower():
            season = QLabel(_short(item.season, 30))
            season.setObjectName("Pill")
            chips.addWidget(season)
        chips.addStretch(1)
        right.addLayout(chips)

        actions = QHBoxLayout()
        details = QPushButton("Vezi detalii")
        details.setProperty("accent", True)
        details.clicked.connect(lambda _checked=False, x=item: _detail_dialog(self, x))
        actions.addWidget(details)
        if item.imdb_id:
            imdb = QPushButton("IMDb")
            imdb.clicked.connect(
                lambda _checked=False, iid=item.imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))
            )
            actions.addWidget(imdb)
        actions.addStretch(1)
        right.addLayout(actions)
        right.addStretch(1)

        main.addLayout(right, 1)
        return box

    def _chapter_row(self, chapter, items):
        block = QWidget()
        outer = QVBoxLayout(block)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(chapter["title"])
        title.setObjectName("SectionTitle")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        count = QLabel(f"{len(items)} titluri")
        count.setObjectName("Muted")
        head.addWidget(count)
        outer.addLayout(head)

        rail = QScrollArea()
        rail.setFrameShape(QFrame.NoFrame)
        rail.setWidgetResizable(False)
        rail.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rail.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        rail.setFixedHeight(500)

        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 6, 8)
        row.setSpacing(12)
        for item in items:
            row.addWidget(_poster_card(self, item), 0, Qt.AlignTop)
        row.addStretch(1)
        strip.setMinimumWidth(max(1, len(items)) * 210 + 24)
        strip.setMinimumHeight(470)
        rail.setWidget(strip)
        outer.addWidget(rail)
        return block

    def page_romanian_list(self):
        mode = str(self.db.get_setting("romanian_list_filter", "unwatched") or "unwatched")
        if mode not in {"unwatched", "watched", "all"}:
            mode = "unwatched"

        all_entries = romanian_films(self.db)
        watched = [x for x in all_entries if x.watched]
        unwatched = [x for x in all_entries if not x.watched]
        entries = watched if mode == "watched" else (all_entries if mode == "all" else unwatched)
        query = str(self.db.get_setting("romanian_list_search", "") or "").strip()
        if query:
            entries = [x for x in entries if _matches_search(x, query)]

        page, content = self.page_shell(
            "Cronologia filmului românesc",
            "Filmele sunt așezate după perioada acțiunii, nu după anul lansării.",
            [("Sincronizează IMDb", lambda: self.sync_imdb_public(silent=False), False)],
        )

        hero = QFrame()
        hero.setObjectName("HeroCard")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(24, 22, 24, 22)
        hl.setSpacing(12)

        kicker = QLabel("FILME ROMÂNEȘTI • CRONOLOGIA ACȚIUNII")
        kicker.setObjectName("Kicker")
        hl.addWidget(kicker)
        headline = QLabel("De la epoci istorice la România de azi")
        headline.setObjectName("HeroTitle")
        headline.setWordWrap(True)
        hl.addWidget(headline)
        intro = QLabel(
            "Parcurgi cinematografia românească în ordinea lumii în care se petrece povestea. "
            "Lista ta rămâne sursa cronologiei, iar ratingurile IMDb separă automat ce ai văzut de ce urmează."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        hl.addWidget(intro)

        stats = QHBoxLayout()
        stats.addWidget(self.metric_badge(str(len(all_entries)), "în colecție") if hasattr(self, "metric_badge") else QLabel(str(len(all_entries))))
        stats.addWidget(self.metric_badge(str(len(unwatched)), "de văzut") if hasattr(self, "metric_badge") else QLabel(str(len(unwatched))))
        stats.addWidget(self.metric_badge(str(len(watched)), "văzute") if hasattr(self, "metric_badge") else QLabel(str(len(watched))))
        stats.addStretch(1)
        hl.addLayout(stats)

        progress = QProgressBar()
        progress.setRange(0, max(1, len(all_entries)))
        progress.setValue(len(watched))
        progress.setFormat(f"{len(watched)} / {len(all_entries)} văzute • %p%")
        progress.setTextVisible(True)
        hl.addWidget(progress)

        filters = QHBoxLayout()
        filters.addWidget(_filter_button(self, "De văzut", "unwatched", mode, len(unwatched)))
        filters.addWidget(_filter_button(self, "Văzute", "watched", mode, len(watched)))
        filters.addWidget(_filter_button(self, "Toate", "all", mode, len(all_entries)))
        filters.addStretch(1)
        hl.addLayout(filters)

        search_row = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Caută titlu, perioadă sau context…")
        search.setText(query)
        search.returnPressed.connect(lambda: _set_search(self, search))
        search_row.addWidget(search, 1)
        search_button = QPushButton("Caută")
        search_button.clicked.connect(lambda _checked=False: _set_search(self, search))
        search_row.addWidget(search_button)
        if query:
            clear = QPushButton("Șterge căutarea")
            clear.clicked.connect(lambda _checked=False: _clear_search(self))
            search_row.addWidget(clear)
        hl.addLayout(search_row)
        content.addWidget(hero)

        # Poster metadata is completed in the background on every first visit,
        # regardless of the current filter/search result.
        QTimer.singleShot(0, lambda: _auto_fill_posters(self))

        next_item = next((x for x in unwatched if _matches_search(x, query)), None)
        if next_item and mode != "watched":
            content.addWidget(_next_up_hero(self, next_item))

        if not entries:
            empty = QFrame()
            empty.setObjectName("PremiumCard")
            el = QVBoxLayout(empty)
            et = QLabel("Niciun film în această categorie")
            et.setObjectName("SectionTitle")
            el.addWidget(et)
            ex = QLabel("Schimbă filtrul, șterge căutarea sau sincronizează IMDb.")
            ex.setObjectName("Muted")
            el.addWidget(ex)
            content.addWidget(empty)
            content.addStretch(1)
            return page

        chapters = romanian_chapters()
        for chapter in chapters:
            items = [x for x in entries if x.chapter == chapter["id"]]
            if not items:
                continue
            content.addWidget(_chapter_row(self, chapter, items))

        content.addStretch(1)
        return page

    window_cls.page_romanian_list = page_romanian_list
    window_cls._romanian_list_ui_patch_installed = True
