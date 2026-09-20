from __future__ import annotations

import csv
from datetime import date, timedelta
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, QSize
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .library_repair import rated_library_health
from .metadata_provenance import metadata_sources_for_movie
from .profile import build_profile, get_profile
from .recommendation_outcomes_v42 import recommendation_performance
from .util import json_loads


class SmartItem(QTableWidgetItem):
    """Table item with a display string and an independent sort value."""

    def __init__(self, text: str, sort_value: Any = None):
        super().__init__(text)
        self.sort_value = text.casefold() if sort_value is None else sort_value

    def __lt__(self, other):
        if isinstance(other, SmartItem):
            a = self.sort_value
            b = other.sort_value
            if a is None and b is None:
                return False
            if a is None:
                return True
            if b is None:
                return False
            try:
                return a < b
            except TypeError:
                return str(a).casefold() < str(b).casefold()
        return super().__lt__(other)


class RatingTitleDelegate(QStyledItemDelegate):
    """Paint a two-line title without creating thousands of child widgets."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        original = str(index.data(Qt.UserRole + 1) or index.data(Qt.DisplayRole) or "")
        localized = str(index.data(Qt.UserRole + 2) or "")
        rect = option.rect.adjusted(12, 5, -8, -5)
        selected = bool(option.state & QStyle.State_Selected)
        primary_color = option.palette.highlightedText().color() if selected else QColor("#f4f7fb")
        secondary_color = option.palette.highlightedText().color() if selected else QColor("#9aa9bc")

        primary_font = QFont(option.font)
        primary_font.setBold(True)
        primary_font.setPointSize(max(10, option.font.pointSize() + 1))
        painter.setFont(primary_font)
        painter.setPen(primary_color)
        fm = QFontMetrics(primary_font)
        first = fm.elidedText(original, Qt.ElideRight, rect.width())
        painter.drawText(rect.x(), rect.y() + fm.ascent() + 1, first)

        if localized:
            secondary_font = QFont(option.font)
            secondary_font.setPointSize(max(9, option.font.pointSize()))
            painter.setFont(secondary_font)
            painter.setPen(secondary_color)
            sfm = QFontMetrics(secondary_font)
            second = sfm.elidedText(f"({localized})", Qt.ElideRight, rect.width())
            painter.drawText(rect.x(), rect.bottom() - 4, second)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(70, hint.height()))


_GENRE_COLORS = {
    "Action": ("#12345a", "#8fc6ff"),
    "Comedy": ("#12345a", "#8fc6ff"),
    "Drama": ("#34205f", "#c6a9ff"),
    "Horror": ("#562027", "#ff9aa5"),
    "Mystery": ("#164b4a", "#8fe6df"),
    "Crime": ("#5a3518", "#ffb36d"),
    "Romance": ("#5a1f49", "#ff9bd7"),
    "Short": ("#2a3442", "#d7e0eb"),
    "Documentary": ("#254a34", "#9ee6b4"),
}


class GenreBadgeDelegate(QStyledItemDelegate):
    """Compact painted badges; keeps the full 2,445-row table responsive."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        raw = str(index.data(Qt.UserRole + 3) or "")
        genres = [x for x in raw.split("|") if x]
        x = option.rect.x() + 10
        y = option.rect.center().y()
        right = option.rect.right() - 8
        font = QFont(option.font)
        font.setPointSize(max(9, option.font.pointSize()))
        painter.setFont(font)
        fm = QFontMetrics(font)

        shown = 0
        for genre in genres:
            text_w = fm.horizontalAdvance(genre) + 18
            if x + text_w > right:
                break
            bg, fg = _GENRE_COLORS.get(genre, ("#263445", "#c8d5e4"))
            badge = option.rect.adjusted(0, 0, 0, 0)
            badge.setLeft(x)
            badge.setWidth(text_w)
            badge.setTop(y - 15)
            badge.setHeight(30)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(badge, 10, 10)
            painter.setPen(QColor(fg))
            painter.drawText(badge, Qt.AlignCenter, genre)
            x += text_w + 7
            shown += 1

        if shown < len(genres) and x < right:
            painter.setPen(QColor("#9aa9bc"))
            painter.drawText(x, y + fm.ascent() // 2, f"+{len(genres) - shown}")
        painter.restore()


def load_rating_rows(db) -> list[dict]:
    """Return the complete rated library. Intentionally no arbitrary LIMIT."""
    with db.connect() as con:
        rows = con.execute(
            """
            SELECT
                r.id AS rating_id,
                r.rating AS user_rating,
                r.date_rated,
                r.source AS rating_source,
                m.id AS movie_id,
                m.imdb_id,
                m.title,
                m.original_title,
                m.year,
                m.imdb_rating,
                m.poster_url,
                m.runtime_min,
                m.genres_json,
                m.directors_json
            FROM ratings r
            JOIN movies m ON m.id=r.movie_id
            ORDER BY COALESCE(r.date_rated,'') DESC, r.id DESC
            """
        ).fetchall()

    out: list[dict] = []
    for row in rows:
        genres = json_loads(row["genres_json"], []) or []
        directors = json_loads(row["directors_json"], []) or []
        imdb_rating = float(row["imdb_rating"]) if row["imdb_rating"] is not None else None
        user_rating = int(row["user_rating"])
        out.append(
            {
                "rating_id": int(row["rating_id"]),
                "movie_id": int(row["movie_id"]),
                "imdb_id": str(row["imdb_id"] or ""),
                "title": str(row["title"] or ""),
                "original_title": str(row["original_title"] or ""),
                "year": int(row["year"]) if row["year"] is not None else None,
                "user_rating": user_rating,
                "imdb_rating": imdb_rating,
                "poster_url": str(row["poster_url"] or ""),
                "runtime_min": int(row["runtime_min"]) if row["runtime_min"] is not None else None,
                "delta": (user_rating - imdb_rating) if imdb_rating is not None else None,
                "genres": [str(x) for x in genres],
                "directors": [str(x) for x in directors],
                "date_rated": str(row["date_rated"] or ""),
                "source": str(row["rating_source"] or ""),
            }
        )
    return out


def _display_title(row: dict) -> str:
    """Prefer the work's original-language title, falling back to the stored display title."""
    original = str(row.get("original_title") or "").strip()
    return original or str(row.get("title") or "").strip()


def _rating_sort_key(row: dict, mode: str):
    title = _display_title(row).casefold()
    if mode == "rating_desc":
        return (-int(row["user_rating"]), title)
    if mode == "rating_asc":
        return (int(row["user_rating"]), title)
    if mode == "imdb_desc":
        return (-(row["imdb_rating"] if row["imdb_rating"] is not None else -999.0), title)
    if mode == "delta_desc":
        return (-(row["delta"] if row["delta"] is not None else -999.0), title)
    if mode == "year_desc":
        return (-(row["year"] if row["year"] is not None else -1), title)
    if mode == "title_asc":
        return (title, -(row["year"] or 0))
    return ((row["date_rated"] or ""), int(row["rating_id"]))


def _feature_category(name: str) -> str:
    for prefix, category in (
        ("genre:", "genres"),
        ("director:", "directors"),
        ("theme:", "themes"),
        ("country:", "countries"),
        ("decade:", "decades"),
        ("runtime:", "runtime"),
        ("popularity:", "popularity"),
        ("combo:", "combos"),
    ):
        if name.startswith(prefix):
            return category
    return "other"


def _pretty_feature(name: str) -> str:
    if name.startswith("combo:"):
        value = name[len("combo:"):]
        value = value.replace("genre:", "").replace("director:", "")
        value = value.replace("|", " + ")
    else:
        value = name.split(":", 1)[-1]
    value = value.replace("_", " ").strip()
    return value.title()


def _profile_feature_rows(profile: dict) -> list[dict]:
    rows = []
    for name, stats in (profile.get("features") or {}).items():
        rows.append(
            {
                "name": name,
                "category": _feature_category(name),
                "label": _pretty_feature(name),
                "preference": float(stats.get("preference", 0.0) or 0.0),
                "mean_rating": float(stats["mean_rating"]) if stats.get("mean_rating") is not None else None,
                "count": int(stats.get("count", 0) or 0),
                "std_rating": float(stats["std_rating"]) if stats.get("std_rating") is not None else None,
            }
        )
    return rows



def _rating_activity(db) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Return rating activity by year and by recent month from IMDb date_rated."""
    with db.connect() as con:
        rows = con.execute(
            """SELECT substr(date_rated,1,10) AS d
               FROM ratings
               WHERE date_rated IS NOT NULL AND length(date_rated) >= 7"""
        ).fetchall()
    years: dict[str, int] = {}
    months: dict[str, int] = {}
    for row in rows:
        value = str(row["d"] or "")
        if len(value) >= 4:
            years[value[:4]] = years.get(value[:4], 0) + 1
        if len(value) >= 7:
            months[value[:7]] = months.get(value[:7], 0) + 1
    by_year = sorted(years.items(), key=lambda item: item[0], reverse=True)
    by_month = sorted(months.items(), key=lambda item: item[0], reverse=True)
    return by_year, by_month


def _stable_extremes(features: list[dict], category: str, *, positive: bool, limit: int = 5) -> list[dict]:
    rows = [
        row for row in features
        if row["category"] == category
        and row["mean_rating"] is not None
        and int(row["count"]) >= 3
    ]
    rows.sort(
        key=lambda row: (
            float(row["mean_rating"]),
            int(row["count"]),
            row["label"].casefold(),
        ),
        reverse=positive,
    )
    return rows[:limit]

def install_library_ui(window_cls) -> None:
    """Install the full library/profile explorer on the Premium window class."""

    def page_ratings(self):
        rows = load_rating_rows(self.db)

        def show_more_actions():
            button = self.sender()
            menu = QMenu(self)
            menu.addAction("Import IMDb ratings.csv", self.import_ratings)
            menu.addAction("Adaugă rating manual", self.manual_rating)
            if button is not None:
                menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
            else:
                menu.exec(QCursor.pos())

        def export_current():
            selected = filtered_rows()
            if not selected:
                return
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Export ratinguri",
                "CineCalendar-ratinguri.csv",
                "CSV (*.csv)",
            )
            if not path:
                return
            if not path.lower().endswith(".csv"):
                path += ".csv"
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "Title", "Localized Title", "Year", "Your Rating", "IMDb Rating",
                    "Delta", "Genres", "Directors", "Date Rated", "IMDb ID", "Source",
                ])
                for row in selected:
                    writer.writerow([
                        _display_title(row),
                        row["title"],
                        row["year"] or "",
                        row["user_rating"],
                        row["imdb_rating"] if row["imdb_rating"] is not None else "",
                        f"{row['delta']:+.1f}" if row["delta"] is not None else "",
                        ", ".join(row["genres"]),
                        ", ".join(row["directors"]),
                        row["date_rated"][:10] if row["date_rated"] else "",
                        row["imdb_id"],
                        row["source"],
                    ])
            self.set_status(f"Exportate {len(selected):,} ratinguri în {path}.", False)

        page, content = self.page_shell(
            "Ratinguri IMDb",
            f"Afișate {len(rows):,} din {len(rows):,} ratinguri",
            [
                ("Sincronizează + completează", lambda: self.sync_imdb_public(silent=False), True),
                ("Repară biblioteca", self.repair_library, False),
                ("Export", lambda: export_current(), False),
                ("⋯", show_more_actions, False),
            ],
        )

        all_genres = sorted({g for row in rows for g in row["genres"]}, key=str.casefold)
        all_directors = sorted({d for row in rows for d in row["directors"]}, key=str.casefold)
        all_years = sorted({int(row["year"]) for row in rows if row["year"] is not None}, reverse=True)

        controls = QFrame()
        controls.setObjectName("PremiumCard")
        grid = QGridLayout(controls)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        search = QLineEdit()
        search.setPlaceholderText("Caută filme…")
        grid.addWidget(search, 0, 0, 1, 2)

        genre_filter = QComboBox()
        genre_filter.addItem("Toate genurile", "")
        for genre in all_genres:
            genre_filter.addItem(genre, genre)
        grid.addWidget(genre_filter, 0, 2)

        director_filter = QComboBox()
        director_filter.addItem("Toți regizorii", "")
        for director in all_directors:
            director_filter.addItem(director, director)
        grid.addWidget(director_filter, 0, 3)

        year_filter = QComboBox()
        year_filter.addItem("Toți anii", None)
        for year in all_years:
            year_filter.addItem(str(year), year)
        grid.addWidget(year_filter, 0, 4)

        rating_filter = QComboBox()
        rating_filter.addItem("Toate notele", None)
        for n in range(10, 0, -1):
            rating_filter.addItem(f"{n}/10", n)
        grid.addWidget(rating_filter, 0, 5)

        date_wrap = QWidget()
        date_layout = QHBoxLayout(date_wrap)
        date_layout.setContentsMargins(0, 0, 0, 0)
        date_layout.setSpacing(6)
        date_group = QButtonGroup(page)
        date_group.setExclusive(True)
        date_buttons: dict[str, QPushButton] = {}
        for label, key in (
            ("Toate", "all"),
            ("Azi", "today"),
            ("7 zile", "7d"),
            ("30 zile", "30d"),
            ("Anul acesta", "year"),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("compact", True)
            if key == "all":
                button.setChecked(True)
                button.setProperty("accent", True)
            date_group.addButton(button)
            date_layout.addWidget(button)
            date_buttons[key] = button
        date_layout.addStretch(1)
        grid.addWidget(date_wrap, 1, 0, 1, 4)

        sort_label = QLabel("Sortare:")
        sort_label.setObjectName("Muted")
        grid.addWidget(sort_label, 1, 4, alignment=Qt.AlignRight)

        sort_combo = QComboBox()
        for label, key in (
            ("Ultimul rating primul", "date_desc"),
            ("Nota mea: mare → mică", "rating_desc"),
            ("Nota mea: mică → mare", "rating_asc"),
            ("IMDb: mare → mic", "imdb_desc"),
            ("Diferența mea vs IMDb", "delta_desc"),
            ("An: nou → vechi", "year_desc"),
            ("Titlu A–Z", "title_asc"),
        ):
            sort_combo.addItem(label, key)
        grid.addWidget(sort_combo, 1, 5)
        content.addWidget(controls)

        status = QFrame()
        status.setObjectName("PremiumCard")
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(14, 9, 14, 9)
        count_label = QLabel("")
        count_label.setObjectName("Muted")
        status_layout.addWidget(count_label)
        status_layout.addStretch(1)
        health = rated_library_health(self.db)
        health_label = QLabel(
            f"Bibliotecă IMDb: {health.complete:,}/{health.total:,} metadate esențiale complete"
        )
        health_label.setObjectName("Muted")
        status_layout.addWidget(health_label)
        content.addWidget(status)

        table = QTableWidget(0, 11)
        table.setHorizontalHeaderLabels([
            "#", "Poster", "Titlu (original / localizat)", "An", "Nota ta",
            "IMDb", "Δ", "Genuri", "Regizor", "Data", "⋯",
        ])
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(72)
        table_font = QFont(table.font())
        table_font.setPointSize(max(10, table.font().pointSize() + 1))
        table.setFont(table_font)
        table.setSortingEnabled(False)
        table.setMinimumHeight(650)
        table.setShowGrid(True)
        table.setItemDelegateForColumn(2, RatingTitleDelegate(table))
        table.setItemDelegateForColumn(7, GenreBadgeDelegate(table))

        header = table.horizontalHeader()
        header.setMinimumHeight(46)
        header_font = QFont(header.font())
        header_font.setPointSize(max(10, header.font().pointSize() + 1))
        header_font.setBold(True)
        header.setFont(header_font)
        for col in (0, 1, 3, 4, 5, 6, 9, 10):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(7, QHeaderView.Interactive)
        header.setSectionResizeMode(8, QHeaderView.Interactive)
        table.setColumnWidth(7, 250)
        table.setColumnWidth(8, 220)
        content.addWidget(table)

        poster_cells: dict[int, tuple[QLabel, str, str]] = {}
        loaded_posters: set[int] = set()

        def selected_period() -> str:
            for key, button in date_buttons.items():
                if button.isChecked():
                    return key
            return "all"

        def filtered_rows() -> list[dict]:
            query = search.text().strip().casefold()
            wanted_rating = rating_filter.currentData()
            wanted_genre = str(genre_filter.currentData() or "")
            wanted_director = str(director_filter.currentData() or "")
            wanted_year = year_filter.currentData()
            period = selected_period()
            today = date.today()
            cutoff = None
            if period == "today":
                cutoff = today.isoformat()
            elif period == "7d":
                cutoff = (today - timedelta(days=6)).isoformat()
            elif period == "30d":
                cutoff = (today - timedelta(days=29)).isoformat()
            elif period == "year":
                cutoff = date(today.year, 1, 1).isoformat()

            selected = []
            for row in rows:
                if wanted_rating is not None and int(row["user_rating"]) != int(wanted_rating):
                    continue
                if wanted_genre and wanted_genre not in row["genres"]:
                    continue
                if wanted_director and wanted_director not in row["directors"]:
                    continue
                if wanted_year is not None and row["year"] != int(wanted_year):
                    continue
                if cutoff and (not row["date_rated"] or row["date_rated"][:10] < cutoff):
                    continue
                if query:
                    haystack = " | ".join([
                        row["title"],
                        row["original_title"],
                        str(row["year"] or ""),
                        row["imdb_id"],
                        " ".join(row["genres"]),
                        " ".join(row["directors"]),
                    ]).casefold()
                    if query not in haystack:
                        continue
                selected.append(row)

            mode = str(sort_combo.currentData() or "date_desc")
            if mode == "date_desc":
                selected.sort(key=lambda r: _rating_sort_key(r, mode), reverse=True)
            else:
                selected.sort(key=lambda r: _rating_sort_key(r, mode))
            return selected

        def refresh_visible_posters():
            if not poster_cells or table.rowCount() <= 0:
                return
            top = table.rowAt(0)
            bottom = table.rowAt(max(0, table.viewport().height() - 1))
            if top < 0:
                top = 0
            if bottom < 0:
                bottom = min(table.rowCount() - 1, top + 20)
            start_row = max(0, top - 4)
            end_row = min(table.rowCount() - 1, bottom + 6)
            for row_index in range(start_row, end_row + 1):
                if row_index in loaded_posters:
                    continue
                info = poster_cells.get(row_index)
                if not info:
                    continue
                label, url, key = info
                loaded_posters.add(row_index)
                if url:
                    self.load_poster_async(label, url, key)

        def show_row_menu(row_index: int):
            if row_index < 0 or row_index >= table.rowCount():
                return
            item = table.item(row_index, 2)
            if item is None:
                return
            imdb_id = str(item.data(Qt.UserRole) or "")
            source = str(item.data(Qt.UserRole + 4) or "")
            movie_id = int(item.data(Qt.UserRole + 5) or 0)
            menu = QMenu(self)
            if imdb_id:
                menu.addAction(
                    "Deschide IMDb",
                    lambda iid=imdb_id: QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{iid}/")),
                )
                menu.addAction(
                    "Copiază IMDb ID",
                    lambda iid=imdb_id: QApplication.clipboard().setText(iid),
                )
            source_action = menu.addAction(f"Sursă rating: {source or '—'}")
            source_action.setEnabled(False)

            if movie_id:
                provenance = metadata_sources_for_movie(self.db, movie_id)
                if provenance:
                    providers = {
                        "imdb_graphql": "IMDb GraphQL",
                        "imdb_dataset": "IMDb dataset oficial",
                        "tmdb": "TMDb",
                        "wikimedia": "Wikidata/Wikipedia",
                    }
                    fields = {
                        "original_title": "Titlu original",
                        "runtime_min": "Durată",
                        "genres": "Genuri",
                        "directors": "Regizor",
                        "countries": "Țară",
                        "overview": "Sinopsis",
                        "poster_url": "Poster",
                        "imdb_rating": "Rating IMDb",
                        "num_votes": "Voturi IMDb",
                        "keywords": "Cuvinte-cheie",
                        "tmdb_id": "TMDb ID",
                        "year": "An",
                        "title_type": "Tip",
                    }
                    sources_menu = menu.addMenu("Surse metadate")
                    for field, provider in sorted(provenance.items()):
                        action = sources_menu.addAction(
                            f"{fields.get(field, field)}: {providers.get(provider, provider)}"
                        )
                        action.setEnabled(False)
            menu.exec(QCursor.pos())

        def render():
            selected = filtered_rows()
            table.clearContents()
            table.setRowCount(len(selected))
            poster_cells.clear()
            loaded_posters.clear()

            for i, row in enumerate(selected):
                imdb_id = row["imdb_id"] or ""
                original = _display_title(row)
                localized = str(row["title"] or "").strip()
                title_item = SmartItem(original, original.casefold())
                title_item.setData(Qt.UserRole, imdb_id)
                title_item.setData(Qt.UserRole + 1, original)
                title_item.setData(Qt.UserRole + 2, localized)
                title_item.setData(Qt.UserRole + 4, row["source"])
                title_item.setData(Qt.UserRole + 5, int(row["movie_id"]))
                title_item.setToolTip(
                    f"{original}"
                    + (f"\nTitlu localizat: {localized}" if localized else "")
                    + f"\nIMDb ID: {imdb_id or '—'}\nSursă: {row['source'] or '—'}"
                )

                number = SmartItem(str(i + 1), i + 1)
                number.setTextAlignment(Qt.AlignCenter)

                poster_item = SmartItem("", "")
                poster_item.setData(Qt.UserRole, imdb_id)
                poster = QLabel("—")
                poster.setAlignment(Qt.AlignCenter)
                poster.setFixedSize(44, 64)
                poster.setObjectName("Muted")
                poster.setStyleSheet(
                    "border:1px solid rgba(115,132,154,0.30); border-radius:5px;"
                    "background:rgba(12,18,28,0.45);"
                )
                poster_cells[i] = (poster, row["poster_url"], imdb_id or str(row["movie_id"]))

                year_item = SmartItem(str(row["year"] or "—"), row["year"])
                year_item.setTextAlignment(Qt.AlignCenter)

                user_item = SmartItem(f"{row['user_rating']}/10", row["user_rating"])
                user_item.setTextAlignment(Qt.AlignCenter)
                user_font = QFont(user_item.font())
                user_font.setBold(True)
                user_item.setFont(user_font)
                if row["user_rating"] >= 7:
                    user_item.setForeground(QColor("#45df83"))
                elif row["user_rating"] <= 3:
                    user_item.setForeground(QColor("#ff5f67"))

                imdb_item = SmartItem(
                    f"{row['imdb_rating']:.1f}" if row["imdb_rating"] is not None else "—",
                    row["imdb_rating"],
                )
                imdb_item.setTextAlignment(Qt.AlignCenter)
                if row["imdb_rating"] is not None:
                    imdb_item.setForeground(QColor("#45df83"))

                delta_item = SmartItem(
                    f"{row['delta']:+.1f}" if row["delta"] is not None else "—",
                    row["delta"],
                )
                delta_item.setTextAlignment(Qt.AlignCenter)
                if row["delta"] is not None:
                    if row["delta"] < 0:
                        delta_item.setForeground(QColor("#ff5f67"))
                    elif row["delta"] > 0:
                        delta_item.setForeground(QColor("#45df83"))

                genres_text = ", ".join(row["genres"]) or "—"
                genres_item = SmartItem(genres_text, genres_text.casefold())
                genres_item.setData(Qt.UserRole + 3, "|".join(row["genres"]))
                genres_item.setToolTip(genres_text)

                directors_text = ", ".join(row["directors"]) or "—"
                directors_item = SmartItem(directors_text, directors_text.casefold())
                directors_item.setToolTip(directors_text)

                date_text = row["date_rated"][:10] if row["date_rated"] else "—"
                date_item = SmartItem(date_text, row["date_rated"])
                date_item.setTextAlignment(Qt.AlignCenter)

                more = SmartItem("⋯", "")
                more.setTextAlignment(Qt.AlignCenter)
                more.setToolTip(
                    f"IMDb ID: {imdb_id or '—'}\nSursă: {row['source'] or '—'}"
                )

                items = [
                    number, poster_item, title_item, year_item, user_item,
                    imdb_item, delta_item, genres_item, directors_item, date_item, more,
                ]
                for column, item in enumerate(items):
                    table.setItem(i, column, item)
                table.setCellWidget(i, 1, poster)

            count_label.setText(f"Afișate {len(selected):,} din {len(rows):,} ratinguri")
            QTimer.singleShot(0, refresh_visible_posters)

        def set_period(key: str):
            for candidate, button in date_buttons.items():
                active = candidate == key
                button.setChecked(active)
                button.setProperty("accent", active)
                button.style().unpolish(button)
                button.style().polish(button)
            render()

        for key, button in date_buttons.items():
            button.clicked.connect(lambda _checked=False, k=key: set_period(k))

        def open_imdb(row_index: int, column: int):
            if column == 10:
                show_row_menu(row_index)
                return
            item = table.item(row_index, 2)
            imdb_id = item.data(Qt.UserRole) if item else None
            if imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{imdb_id}/"))

        table.cellDoubleClicked.connect(open_imdb)
        table.cellClicked.connect(lambda row_index, column: show_row_menu(row_index) if column == 10 else None)
        table.verticalScrollBar().valueChanged.connect(lambda _value: refresh_visible_posters())

        debounce = QTimer(page)
        debounce.setSingleShot(True)
        debounce.setInterval(120)
        debounce.timeout.connect(render)
        search.textChanged.connect(lambda _text: debounce.start())
        rating_filter.currentIndexChanged.connect(lambda _i: render())
        genre_filter.currentIndexChanged.connect(lambda _i: render())
        director_filter.currentIndexChanged.connect(lambda _i: render())
        year_filter.currentIndexChanged.connect(lambda _i: render())
        sort_combo.currentIndexChanged.connect(lambda _i: render())

        hint = QLabel(
            "Sfat: dublu-click pe un film pentru IMDb. Coloana ⋯ păstrează IMDb ID și sursa fără să aglomereze tabelul."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        content.addWidget(hint)

        render()
        return page

    def page_profile(self):
        # Recalculate on opening so statistics never remain stale after a sync/repair.
        profile = build_profile(self.db)
        page, content = self.page_shell(
            "Taste Hub",
            "Profilul calculat din ratingurile tale. Acum poți vedea toate semnalele, le poți căuta, filtra și sorta.",
        )

        rated = int(profile.get("rated_count", 0) or 0)
        mean = float(profile.get("global_mean_rating", 0) or 0)
        delta = profile.get("mean_user_minus_imdb")
        version = int(profile.get("version", 0) or 0)
        health = rated_library_health(self.db)

        metrics = QGridLayout(); metrics.setHorizontalSpacing(12); metrics.setVerticalSpacing(12)
        metric_values = [
            (f"{rated:,}", "ratinguri analizate"),
            (f"{mean:.2f}/10", "media ta ponderată"),
            (f"{float(delta):+.2f}" if delta is not None else "fără bază IMDb", "tu vs IMDb"),
            (f"{health.completion_percent:.1f}%", "metadate esențiale complete"),
            (f"{health.missing_genres}", "fără gen"),
            (f"{health.missing_directors}", "fără regizor"),
            (f"v{version}", "versiune profil gust"),
        ]
        for i, (value, label) in enumerate(metric_values):
            card = QFrame(); card.setObjectName("PremiumCard")
            lay = QVBoxLayout(card); lay.setContentsMargins(18, 16, 18, 16)
            val = QLabel(value); val.setObjectName("MetricValue"); lay.addWidget(val)
            cap = QLabel(label); cap.setObjectName("Muted"); cap.setWordWrap(True); lay.addWidget(cap)
            metrics.addWidget(card, i // 4, i % 4)
        metric_wrap = QFrame(); metric_wrap.setLayout(metrics); content.addWidget(metric_wrap)

        try:
            perf = recommendation_performance(self.db)
        except Exception:
            perf = None
        if perf is not None:
            box = QFrame(); box.setObjectName("PremiumCard")
            pl = QVBoxLayout(box); pl.setContentsMargins(18,16,18,16); pl.setSpacing(8)
            ph = QLabel("Performanța reală a recomandărilor")
            ph.setObjectName("SectionTitle"); pl.addWidget(ph)
            desc = QLabel(
                "Măsoară traseul recomandat → ales → pornit → văzut → nota ta reală. "
                "Predicția este comparată cu ratingul IMDb pe care îl dai ulterior."
            )
            desc.setObjectName("Muted"); desc.setWordWrap(True); pl.addWidget(desc)

            pg = QGridLayout(); pg.setHorizontalSpacing(12); pg.setVerticalSpacing(8)
            perf_values = [
                (f"{perf.chosen:,}", "alese"),
                (f"{perf.start_rate*100:.0f}%", "pornite din cele alese"),
                (f"{perf.watched_rate*100:.0f}%", "confirmate văzute"),
                (f"{perf.rated_outcomes:,}", "cu rating ulterior"),
                (f"{perf.mae:.2f}" if perf.mae is not None else "—", "eroare medie predicție"),
                (f"{perf.within_one*100:.0f}%" if perf.within_one is not None else "—", "predicții la ±1 punct"),
                (f"{perf.liked_rate*100:.0f}%" if perf.rated_outcomes else "—", "recomandări notate ≥8"),
                (f"{perf.bias:+.2f}" if perf.bias is not None else "—", "bias estimat − real"),
            ]
            for idx, (value, caption) in enumerate(perf_values):
                card = QFrame(); card.setObjectName("Card")
                lay = QVBoxLayout(card); lay.setContentsMargins(12,10,12,10)
                val = QLabel(value); val.setObjectName("MetricValue"); lay.addWidget(val)
                cap = QLabel(caption); cap.setObjectName("Muted"); cap.setWordWrap(True); lay.addWidget(cap)
                pg.addWidget(card, idx // 4, idx % 4)
            pl.addLayout(pg)

            if perf.recent:
                recent_title = QLabel("Rezultate recente")
                recent_title.setObjectName("BodyStrong"); pl.addWidget(recent_title)
                recent_table = QTableWidget(0, 6)
                recent_table.setHorizontalHeaderLabels(["Titlu","Data","Poziție","Status","Estimat","Real"])
                recent_table.setEditTriggers(QTableWidget.NoEditTriggers)
                recent_table.setSelectionBehavior(QTableWidget.SelectRows)
                recent_table.verticalHeader().setVisible(False)
                recent_table.setRowCount(len(perf.recent))
                for i, row in enumerate(perf.recent):
                    values = [
                        row["title"],
                        row["context_date"],
                        str(row["rank"] or "—"),
                        row["status"],
                        f"{row['predicted']:.1f}" if row["predicted"] is not None else "—",
                        f"{row['actual']}/10" if row["actual"] is not None else "—",
                    ]
                    for j, value in enumerate(values):
                        recent_table.setItem(i, j, QTableWidgetItem(str(value)))
                recent_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
                for col in range(1, 6):
                    recent_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
                recent_table.setMaximumHeight(300)
                pl.addWidget(recent_table)
            content.addWidget(box)

        brain = getattr(self.s.recommender, "personalization_v41", None)
        if brain is not None:
            try:
                p41 = brain.status()
                q41 = dict(p41.get("quality_gate") or {})
                evolution = list(brain.taste_evolution(10))
            except Exception:
                p41, q41, evolution = {}, {}, []

            card = QFrame(); card.setObjectName("PremiumCard")
            pl = QVBoxLayout(card); pl.setContentsMargins(18, 16, 18, 16); pl.setSpacing(7)
            ph = QLabel("Personalizare 4.1 • evoluția gustului")
            ph.setObjectName("SectionTitle"); pl.addWidget(ph)
            gate_text = (
                "ACTIVĂ — rerankingul temporal a trecut quality-gate-ul local."
                if q41.get("approved")
                else "PROTEJATĂ — explicațiile sunt active, dar rerankingul temporal nu primește voie să modifice scorul până nu demonstrează câștig."
            )
            gate = QLabel(gate_text); gate.setWordWrap(True)
            gate.setObjectName("BodyStrong" if q41.get("approved") else "Muted")
            pl.addWidget(gate)
            if q41:
                stats = QLabel(
                    f"Holdout: {int(q41.get('holdout_count',0) or 0)} • "
                    f"MAE bază: {q41.get('baseline_mae','—')} • MAE 4.1: {q41.get('model_mae','—')} • "
                    f"câștig: {float(q41.get('mae_gain',0.0) or 0.0)*100:+.2f}%"
                )
                stats.setObjectName("Muted"); stats.setWordWrap(True); pl.addWidget(stats)

            content_profiles = dict(p41.get("content_profiles") or {})
            if content_profiles:
                cp = QLabel(
                    "Profiluri separate: " + " • ".join(
                        f"{name}: {float(st.get('mean',0.0)):.2f}/10 ({int(st.get('count',0))}), trend {float(st.get('trend',0.0)):+.2f}"
                        for name, st in sorted(content_profiles.items())
                    )
                )
                cp.setObjectName("Muted"); cp.setWordWrap(True); pl.addWidget(cp)

            if evolution:
                eh2 = QLabel("Cele mai clare schimbări recente")
                eh2.setObjectName("BodyStrong"); pl.addWidget(eh2)
                for item in evolution[:8]:
                    feature = str(item.get("feature") or "").replace(":", " → ", 1)
                    trend = float(item.get("trend", 0.0) or 0.0)
                    line = QLabel(
                        f"{feature}: {trend:+.2f} puncte • {int(item.get('count',0))} ratinguri"
                    )
                    line.setObjectName("Muted"); line.setWordWrap(True); pl.addWidget(line)
            content.addWidget(card)

        explain = QFrame(); explain.setObjectName("PremiumCard")
        el = QVBoxLayout(explain); el.setContentsMargins(18, 16, 18, 16); el.setSpacing(6)
        eh = QLabel("Cum se citesc valorile")
        eh.setObjectName("SectionTitle"); el.addWidget(eh)
        et = QLabel(
            "Media ta este ponderată ușor spre ratingurile mai recente. „Tu vs IMDb” este diferența medie dintre nota ta și nota IMDb; "
            "o valoare negativă înseamnă că, în medie, notezi mai jos decât IMDb. În tabel, „Afinitate” este semnalul învățat de motor "
            "(aprox. −1…+1), nu o notă: combină ratingurile, recența, numărul de exemple și feedbackul, astfel încât un 10/10 dintr-un singur film "
            "să nu bată automat un tipar stabil din zeci de filme."
        )
        et.setObjectName("Muted"); et.setWordWrap(True); el.addWidget(et)
        content.addWidget(explain)

        all_features = _profile_feature_rows(profile)

        by_year, by_month = _rating_activity(self.db)
        activity = QFrame(); activity.setObjectName("PremiumCard")
        al = QVBoxLayout(activity); al.setContentsMargins(18, 16, 18, 16); al.setSpacing(7)
        ah = QLabel("Activitatea ratingurilor")
        ah.setObjectName("SectionTitle"); al.addWidget(ah)
        years_text = " • ".join(f"{year}: {count}" for year, count in by_year[:6]) or "Nu există date calendaristice în ratinguri."
        months_text = " • ".join(f"{month}: {count}" for month, count in by_month[:6]) or "—"
        yl = QLabel("Pe ani: " + years_text); yl.setWordWrap(True); al.addWidget(yl)
        ml = QLabel("Ultimele luni cu activitate: " + months_text); ml.setObjectName("Muted"); ml.setWordWrap(True); al.addWidget(ml)
        content.addWidget(activity)

        extremes = QFrame(); extremes.setObjectName("PremiumCard")
        xl = QVBoxLayout(extremes); xl.setContentsMargins(18, 16, 18, 16); xl.setSpacing(6)
        xh = QLabel("Tipare stabile din ratingurile tale")
        xh.setObjectName("SectionTitle"); xl.addWidget(xh)
        for category, label in (("genres", "Genuri"), ("directors", "Regizori")):
            best = _stable_extremes(all_features, category, positive=True)
            worst = _stable_extremes(all_features, category, positive=False)
            best_text = ", ".join(f"{r['label']} {r['mean_rating']:.1f}/10 ({r['count']})" for r in best) or "insuficiente date"
            worst_text = ", ".join(f"{r['label']} {r['mean_rating']:.1f}/10 ({r['count']})" for r in worst) or "insuficiente date"
            line = QLabel(f"{label} — cel mai bine: {best_text}\n{label} — cel mai slab: {worst_text}")
            line.setWordWrap(True); xl.addWidget(line)
        note = QLabel("Rezumatul rapid cere minimum 3 filme per gen/regizor, ca să nu tragă concluzii dintr-un singur rating.")
        note.setObjectName("Muted"); note.setWordWrap(True); xl.addWidget(note)
        content.addWidget(extremes)
        controls = QFrame(); controls.setObjectName("PremiumCard")
        cl = QGridLayout(controls); cl.setContentsMargins(16, 14, 16, 14); cl.setHorizontalSpacing(10); cl.setVerticalSpacing(8)
        search = QLineEdit(); search.setPlaceholderText("Caută Western, Tarantino, război, România…")
        cl.addWidget(search, 0, 0, 1, 3)

        category = QComboBox()
        for label, key in (
            ("Genuri", "genres"),
            ("Regizori", "directors"),
            ("Teme / atmosferă", "themes"),
            ("Țări", "countries"),
            ("Decenii", "decades"),
            ("Durată", "runtime"),
            ("Popularitate", "popularity"),
            ("Combinații învățate", "combos"),
            ("Toate semnalele", "all"),
        ):
            category.addItem(label, key)
        cl.addWidget(category, 1, 0)

        sort_combo = QComboBox()
        for label, key in (
            ("Afinitate: mare → mică", "preference_desc"),
            ("Media ta: mare → mică", "mean_desc"),
            ("Cele mai multe filme", "count_desc"),
            ("Nume A–Z", "name_asc"),
        ):
            sort_combo.addItem(label, key)
        cl.addWidget(sort_combo, 1, 1)
        reset = QPushButton("Resetează"); cl.addWidget(reset, 1, 2)
        content.addWidget(controls)

        count_label = QLabel(""); count_label.setObjectName("Muted"); content.addWidget(count_label)

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Element", "Media ta", "Filme", "Afinitate"])
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.verticalHeader().setVisible(False)
        table.setMinimumHeight(600)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        content.addWidget(table)

        def render_features():
            q = search.text().strip().casefold()
            cat = str(category.currentData() or "genres")
            selected = [r for r in all_features if (cat == "all" or r["category"] == cat)]
            if q:
                selected = [r for r in selected if q in r["label"].casefold() or q in r["name"].casefold()]
            mode = str(sort_combo.currentData() or "preference_desc")
            if mode == "mean_desc":
                selected.sort(key=lambda r: (-(r["mean_rating"] if r["mean_rating"] is not None else -999.0), -r["count"], r["label"].casefold()))
            elif mode == "count_desc":
                selected.sort(key=lambda r: (-r["count"], -abs(r["preference"]), r["label"].casefold()))
            elif mode == "name_asc":
                selected.sort(key=lambda r: r["label"].casefold())
            else:
                selected.sort(key=lambda r: (-r["preference"], -r["count"], r["label"].casefold()))

            table.setSortingEnabled(False)
            table.clearContents(); table.setRowCount(len(selected))
            for i, row in enumerate(selected):
                cells = [
                    SmartItem(row["label"]),
                    SmartItem(f"{row['mean_rating']:.2f}/10" if row["mean_rating"] is not None else "—", row["mean_rating"]),
                    SmartItem(f"{row['count']:,}", row["count"]),
                    SmartItem(f"{row['preference']:+.3f}", row["preference"]),
                ]
                for j, cell in enumerate(cells):
                    table.setItem(i, j, cell)
            table.setSortingEnabled(True)
            count_label.setText(f"Afișate {len(selected):,} semnale din {len(all_features):,} calculate")

        debounce = QTimer(page); debounce.setSingleShot(True); debounce.setInterval(120); debounce.timeout.connect(render_features)
        search.textChanged.connect(lambda _text: debounce.start())
        category.currentIndexChanged.connect(lambda _i: render_features())
        sort_combo.currentIndexChanged.connect(lambda _i: render_features())

        def reset_profile_filters():
            search.clear(); category.setCurrentIndex(0); sort_combo.setCurrentIndex(0); render_features()

        reset.clicked.connect(reset_profile_filters)
        render_features()
        return page

    window_cls.page_ratings = page_ratings
    window_cls.page_profile = page_profile
