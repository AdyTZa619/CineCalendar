from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, QTimer, QPoint
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



class _StoryDragScrollArea(QScrollArea):
    """Horizontal shelf with mouse-drag scrolling and no visible scrollbar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        try:
            QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)
        except Exception:
            pass

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        bar = self.horizontalScrollBar()
        if delta and bar.maximum():
            bar.setValue(bar.value() - int(delta))
            event.accept()
            return
        super().wheelEvent(event)


class _StoryPosterCard(QFrame):
    """Poster card with chronology metadata on hover."""

    def __init__(self, window, item, short, detail_callback, poster_failed_callback, parent=None):
        super().__init__(parent)
        self.window = window
        self.item = item
        self.short = short
        self.detail_callback = detail_callback
        self.poster_failed_callback = poster_failed_callback
        self._poster_started = False

        self.setObjectName("PremiumCard")
        self.setFixedWidth(198)
        self.setFixedHeight(462)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 12)
        layout.setSpacing(7)

        state = QLabel(
            f"VĂZUT  {item.user_rating}/10"
            if item.watched and item.user_rating
            else ("VĂZUT" if item.watched else "DE VĂZUT")
        )
        state.setObjectName("Kicker" if not item.watched else "Score")
        layout.addWidget(state)

        if hasattr(window, "poster_label"):
            poster = window.poster_label(176, 258)
        else:
            poster = QLabel()
            poster.setFixedSize(176, 258)
            poster.setAlignment(Qt.AlignCenter)
            poster.setObjectName("Muted")
        self.poster = poster
        poster.setObjectName("StoryPoster")
        poster.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(poster, alignment=Qt.AlignHCenter)

        title = QLabel(display_title(item.film))
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        title.setFixedHeight(42)
        layout.addWidget(title)

        period_text = item.period or "Neconfirmată"
        season_text = item.season or "Neconfirmat"
        period = QLabel("Acțiunea: " + short(period_text, 48))
        period.setObjectName("Muted")
        period.setWordWrap(True)
        period.setFixedHeight(32)
        period.setToolTip(period_text)
        layout.addWidget(period)

        season = QLabel("Luna / anotimpul: " + short(season_text, 40))
        season.setObjectName("Muted")
        season.setWordWrap(True)
        season.setFixedHeight(32)
        season.setToolTip(season_text)
        layout.addWidget(season)

        meta_bits = []
        if item.release_year:
            meta_bits.append(f"Lansare {item.release_year}")
        if item.imdb_rating is not None:
            meta_bits.append(f"IMDb {item.imdb_rating:.1f}")
        meta = QLabel(" • ".join(meta_bits) if meta_bits else "IMDb neidentificat")
        meta.setObjectName("Muted")
        meta.setWordWrap(True)
        meta.setFixedHeight(28)
        layout.addWidget(meta)

        row = QHBoxLayout()
        details = QPushButton("Detalii")
        details.clicked.connect(lambda _checked=False, x=item: detail_callback(x))
        row.addWidget(details)
        if item.imdb_id:
            imdb = QPushButton("IMDb")
            imdb.clicked.connect(
                lambda _checked=False, iid=item.imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))
            )
            row.addWidget(imdb)
        layout.addLayout(row)

        self._overlay = QFrame(self)
        self._overlay.setObjectName("StoryHover")
        self._overlay.setStyleSheet(
            "QFrame#StoryHover{"
            "background:rgba(18,20,24,245);"
            "border:1px solid rgba(255,255,255,45);"
            "border-radius:12px;}"
        )
        overlay = QVBoxLayout(self._overlay)
        overlay.setContentsMargins(15, 14, 15, 15)
        overlay.setSpacing(8)

        hover_title = QLabel(display_title(item.film))
        hover_title.setObjectName("SectionTitle")
        hover_title.setWordWrap(True)
        overlay.addWidget(hover_title)

        hover_period = QLabel("Perioada acțiunii\n" + (item.period or "Neconfirmată"))
        hover_period.setObjectName("BodyStrong")
        hover_period.setWordWrap(True)
        overlay.addWidget(hover_period)

        hover_season = QLabel("Luna / anotimpul\n" + (item.season or "Neconfirmat"))
        hover_season.setObjectName("Muted")
        hover_season.setWordWrap(True)
        overlay.addWidget(hover_season)

        hover_context = QLabel("Descriere\n" + (item.context or "Neconfirmată"))
        hover_context.setObjectName("Muted")
        hover_context.setWordWrap(True)
        hover_context.setTextInteractionFlags(Qt.TextSelectableByMouse)
        overlay.addWidget(hover_context, 1)

        hover_source = QLabel("Certitudine / sursă: " + (item.certainty_source or "Neconfirmată"))
        hover_source.setObjectName("Muted")
        hover_source.setWordWrap(True)
        overlay.addWidget(hover_source)

        actions = QHBoxLayout()
        detail = QPushButton("Vezi detalii")
        detail.setProperty("accent", True)
        detail.clicked.connect(lambda _checked=False, x=item: detail_callback(x))
        actions.addWidget(detail)
        if item.imdb_id:
            imdb = QPushButton("IMDb")
            imdb.clicked.connect(
                lambda _checked=False, iid=item.imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/"))
            )
            actions.addWidget(imdb)
        actions.addStretch(1)
        overlay.addLayout(actions)
        self._overlay.hide()
        self._drag_start = None
        self._drag_origin = 0

    def _story_scroll_area(self):
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, _StoryDragScrollArea):
                return widget
            widget = widget.parentWidget()
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start = event.position().toPoint()
            scroll = self._story_scroll_area()
            self._drag_origin = scroll.horizontalScrollBar().value() if scroll else 0
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start is not None and event.buttons() & Qt.LeftButton:
            delta = event.position().toPoint().x() - self._drag_start.x()
            if abs(delta) >= 8:
                scroll = self._story_scroll_area()
                if scroll is not None:
                    scroll.horizontalScrollBar().setValue(self._drag_origin - delta)
                    event.accept()
                    return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def _start_poster(self):
        if self._poster_started:
            return
        self._poster_started = True
        item = self.item
        if item.poster_url and hasattr(self.window, "load_poster_async"):
            self.window.load_poster_async(
                self.poster,
                item.poster_url,
                item.imdb_id or str(item.local_movie_id or item.film),
                lambda _message, x=item: self.poster_failed_callback(x),
            )
        else:
            self.poster.setText("CINECALENDAR\n\n" + self.short(display_title(item.film), 42))
            self.poster.setWordWrap(True)
            self.poster.setAlignment(Qt.AlignCenter)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._start_poster)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect().adjusted(1, 1, -1, -1))

    def enterEvent(self, event):
        super().enterEvent(event)
        self._overlay.setGeometry(self.rect().adjusted(1, 1, -1, -1))
        self._overlay.raise_()
        self._overlay.show()

    def leaveEvent(self, event):
        self._overlay.hide()
        super().leaveEvent(event)


class _StoryShelf(QWidget):
    """Chronology shelf with edge arrows, drag scrolling and lazy card loading."""

    CARD_STEP = 210
    BATCH_SIZE = 8

    def __init__(self, items, card_factory, parent=None):
        super().__init__(parent)
        self.items = list(items)
        self.card_factory = card_factory
        self._loaded = 0
        self._syncing = False

        outer = QGridLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setHorizontalSpacing(0)
        outer.setVerticalSpacing(0)

        self.scroll = _StoryDragScrollArea(self)
        self.scroll.setFixedHeight(478)
        outer.addWidget(self.scroll, 0, 0)

        self.strip = QWidget()
        self.row = QHBoxLayout(self.strip)
        self.row.setContentsMargins(5, 3, 5, 8)
        self.row.setSpacing(12)
        self.row.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll.setWidget(self.strip)
        self._append_batch()

        self.left = QPushButton("‹", self)
        self.right = QPushButton("›", self)
        for button in (self.left, self.right):
            button.setObjectName("StoryArrow")
            button.setFixedSize(42, 72)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton#StoryArrow{"
                "background:rgba(18,20,24,215);color:white;"
                "border:1px solid rgba(255,255,255,45);"
                "border-radius:12px;font-size:28px;font-weight:600;}"
                "QPushButton#StoryArrow:hover{background:rgba(42,46,54,245);}"
            )
        self.left.clicked.connect(lambda: self._page(-1))
        self.right.clicked.connect(lambda: self._page(1))
        outer.addWidget(self.left, 0, 0, Qt.AlignVCenter | Qt.AlignLeft)
        outer.addWidget(self.right, 0, 0, Qt.AlignVCenter | Qt.AlignRight)

        self.more = QPushButton("Arată încă")
        self.more.setCursor(Qt.PointingHandCursor)
        self.more.clicked.connect(self._show_more)
        outer.addWidget(self.more, 0, 0, Qt.AlignBottom | Qt.AlignHCenter)

        bar = self.scroll.horizontalScrollBar()
        bar.valueChanged.connect(self._sync_arrows)
        bar.rangeChanged.connect(lambda *_args: self._sync_arrows())
        self._sync_arrows()

    def _append_batch(self, count=None):
        remaining = len(self.items) - self._loaded
        if remaining <= 0:
            self._refresh_strip_width()
            return False
        amount = min(int(count or self.BATCH_SIZE), remaining)
        end = self._loaded + amount
        for item in self.items[self._loaded:end]:
            self.row.addWidget(self.card_factory(item), 0, Qt.AlignTop)
        self._loaded = end
        self._refresh_strip_width()
        return True

    def _refresh_strip_width(self):
        width = max(220, self._loaded * self.CARD_STEP + 24)
        self.strip.setMinimumWidth(width)
        self.strip.setMinimumHeight(466)

    def _show_more(self):
        if self._append_batch():
            self._sync_arrows()

    def _page(self, direction):
        bar = self.scroll.horizontalScrollBar()
        if direction > 0 and self._loaded < len(self.items):
            # A right-arrow click is allowed to fetch the next shelf segment
            # even when the currently loaded cards do not overflow the viewport.
            old_max = bar.maximum()
            if old_max == 0 or bar.value() >= old_max - 90:
                self._append_batch()
        step = max(240, int(self.scroll.viewport().width() * 0.82))
        bar.setValue(bar.value() + direction * step)

    def _sync_arrows(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            bar = self.scroll.horizontalScrollBar()
            maximum = bar.maximum()
            value = bar.value()
            self.more.setVisible(self._loaded < len(self.items))
            if self._loaded < len(self.items) and maximum and value >= maximum - 90:
                self._append_batch()
                maximum = bar.maximum()
                value = bar.value()
            self.left.setVisible(value > 0)
            self.right.setVisible(self._loaded < len(self.items) or value < maximum)
        finally:
            self._syncing = False


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

        story_season = QLabel("Luna / anotimpul: " + (item.season or "Neconfirmat"))
        story_season.setObjectName("Muted")
        story_season.setWordWrap(True)
        right.addWidget(story_season)

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
            (linked_count, "IMDb identificate"),
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
