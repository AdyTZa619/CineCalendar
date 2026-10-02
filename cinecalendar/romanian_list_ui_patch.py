from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtWidgets import QScroller
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget, QLineEdit, QProgressBar,
)

from .qt_ui import WorkerThread
from .romanian_films import (
    display_title,
    prepare_romanian_library,
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

    def _library_signature(self):
        entries = romanian_films(self.db)
        linked = sum(1 for item in entries if item.imdb_id)
        watched = sum(1 for item in entries if item.watched)
        posters = sum(1 for item in entries if item.poster_url)
        with self.db.connect() as con:
            ratings = int(con.execute("SELECT COUNT(*) FROM ratings").fetchone()[0])
        return (linked, watched, posters, ratings)

    def _poster_failed(self, item):
        if not item.local_movie_id or not item.poster_url:
            return
        failure_key = (int(item.local_movie_id), str(item.poster_url))
        failed = getattr(self, "_romanian_broken_poster_urls", set())
        first_failure = failure_key not in failed
        failed.add(failure_key)
        self._romanian_broken_poster_urls = failed

        # Persist the broken URL so the next metadata pass skips it and can try
        # IMDb Search/Wikidata/Wikimedia instead of restoring the same failure.
        stored = self.db.get_setting("romanian_broken_poster_urls", [])
        if not isinstance(stored, list):
            stored = []
        url = str(item.poster_url)
        if url not in stored:
            stored.append(url)
            self.db.set_setting("romanian_broken_poster_urls", stored[-250:])

        try:
            with self.db.tx() as con:
                con.execute(
                    "UPDATE movies SET poster_url=NULL WHERE id=? AND poster_url=?",
                    (int(item.local_movie_id), item.poster_url),
                )
        except Exception:
            return

        if not first_failure:
            return
        self._romanian_prepare_signature = None
        self.set_status("Un poster nu s-a încărcat; caut automat o sursă alternativă.", False)
        QTimer.singleShot(350, lambda: _auto_prepare_library(self, force=True))

    def _auto_prepare_library(self, force: bool = False):
        if getattr(self, "_ui_closing", False):
            return
        worker = getattr(self, "romanian_assets_worker", None)
        if worker is not None and worker.isRunning():
            return

        signature = _library_signature(self)
        if not force and getattr(self, "_romanian_prepare_signature", None) == signature:
            return
        self._romanian_prepare_signature = signature

        self.set_status("Verific automat legăturile IMDb și posterele filmelor românești…", True)
        worker = WorkerThread(
            lambda progress: prepare_romanian_library(self.db, progress=progress, force=force),
            self,
        )
        self.romanian_assets_worker = worker
        worker.message.connect(lambda message: self.set_status(message, True))

        def done(result):
            self.romanian_assets_worker = None
            self._romanian_prepare_signature = _library_signature(self)
            linked = int(result.get("linked", 0) or 0)
            posters = int(result.get("posters", 0) or 0)
            watched = int(result.get("watched", 0) or 0)
            unresolved = int(result.get("unresolved", 0) or 0)
            errors = int(result.get("resolver_errors", 0) or 0) + int(result.get("poster_errors", 0) or 0)
            if errors:
                self.set_status(
                    f"Filme RO: {linked}/233 identificate • {posters}/233 postere • "
                    f"{watched} văzute • {errors} erori de sursă.",
                    False,
                )
            else:
                self.set_status(
                    f"Filme RO: {linked}/233 identificate • {posters}/233 postere • "
                    f"{watched} văzute • {unresolved} neidentificate.",
                    False,
                )
            if self.current_page == "romanian_list" and int(result.get("changed", 0) or 0):
                self.show_page("romanian_list")

        def failed(message):
            self.romanian_assets_worker = None
            self._romanian_prepare_signature = None
            detail = str(message or "eroare necunoscută").replace("\n", " ")[:180]
            self.set_status(f"Pregătirea filmelor românești a eșuat: {detail}", False)
            self.s.log.warning("Romanian library preparation failed: %s", message)

        worker.success.connect(done)
        worker.failure.connect(failed)
        worker.finished.connect(worker.deleteLater)
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

    def _poster_card(self, item, parent=None):
        return _StoryPosterCard(
            self,
            item,
            _short,
            lambda x: _detail_dialog(self, x),
            lambda x: _poster_failed(self, x),
            parent,
        )

    def _open_chapter_catalog(self, title, items):
        dialog = QDialog(self)
        dialog.setWindowTitle("Catalog complet • " + title)
        dialog.resize(1180, 820)
        root = QVBoxLayout(dialog)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        head = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("HeroTitle")
        heading.setWordWrap(True)
        head.addWidget(heading, 1)
        total = QLabel(f"{len(items)} titluri")
        total.setObjectName("Muted")
        head.addWidget(total)
        root.addLayout(head)

        hint = QLabel(
            "Catalogul folosește perioada acțiunii și luna/anotimpul din lista cronologică. "
            "Unde informația nu este confirmată, este afișat explicit acest lucru."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        area.setWidget(inner)
        root.addWidget(area, 1)

        loaded = 0
        columns = 4
        chunk = 20

        def append_chunk():
            nonlocal loaded
            end = min(len(items), loaded + chunk)
            for index in range(loaded, end):
                grid.addWidget(
                    _poster_card(self, items[index], inner),
                    index // columns,
                    index % columns,
                )
            loaded = end
            more.setText(f"Încarcă încă ({len(items) - loaded})")
            more.setVisible(loaded < len(items))

        more = QPushButton()
        more.setProperty("accent", True)
        more.clicked.connect(append_chunk)
        root.addWidget(more)
        append_chunk()
        dialog.exec()

    def _open_complete_catalog(self):
        self._open_chapter_catalog("Toate filmele românești", romanian_films(self.db))
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
            self.load_poster_async(
                poster,
                item.poster_url,
                item.imdb_id or str(item.local_movie_id or item.film),
                lambda _message, x=item: _poster_failed(self, x),
            )
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
        outer.setSpacing(7)

        head = QHBoxLayout()
        title = QLabel(chapter["title"])
        title.setObjectName("SectionTitle")
        title.setWordWrap(True)
        head.addWidget(title, 1)

        count = QLabel(f"{len(items)} titluri")
        count.setObjectName("Muted")
        head.addWidget(count)

        see_all = QPushButton("Vezi tot")
        see_all.clicked.connect(
            lambda _checked=False, t=chapter["title"], xs=list(items):
            _open_chapter_catalog(self, t, xs)
        )
        head.addWidget(see_all)
        outer.addLayout(head)

        shelf = _StoryShelf(
            items,
            lambda item: _poster_card(self, item),
            block,
        )
        outer.addWidget(shelf)
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
            [
                ("Sincronizează IMDb", lambda: self.sync_imdb_public(silent=False), False),
                ("Reverifică datele", lambda: _auto_prepare_library(self, force=True), False),
            ],
        )

        hero = QFrame()
        hero.setObjectName("HeroCard")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(20, 16, 20, 16)
        hl.setSpacing(8)

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
        stats.setSpacing(7)
        linked_count = sum(1 for item in all_entries if item.imdb_id)
        poster_count = sum(1 for item in all_entries if item.poster_url)

        def compact_metric(value, caption):
            badge = QFrame()
            badge.setObjectName("MetricCompact")
            badge.setFixedHeight(46)
            badge_layout = QVBoxLayout(badge)
            badge_layout.setContentsMargins(10, 5, 10, 5)
            badge_layout.setSpacing(0)
            number = QLabel(str(value))
            number.setObjectName("BodyStrong")
            caption_label = QLabel(caption)
            caption_label.setObjectName("Muted")
            badge_layout.addWidget(number)
            badge_layout.addWidget(caption_label)
            return badge

        for value, caption in (
            (len(all_entries), "în colecție"),
            (len(unwatched), "de văzut"),
            (len(watched), "văzute"),
            (linked_count, "IMDb"),
            (poster_count, "postere"),
        ):
            stats.addWidget(compact_metric(value, caption), 1)
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
        catalog_button = QPushButton(f"Catalog complet  •  {len(all_entries)}")
        catalog_button.setProperty("accent", True)
        catalog_button.clicked.connect(lambda _checked=False: _open_complete_catalog(self))
        filters.addWidget(catalog_button)
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

        # Resolve missing IMDb identities first, then posters. The signature
        # changes after a ratings sync, so the same session can automatically
        # re-run when new profile data arrives.
        QTimer.singleShot(0, lambda: _auto_prepare_library(self))

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
