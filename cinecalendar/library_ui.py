from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QVBoxLayout,
)

from .profile import get_profile
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
                "delta": (user_rating - imdb_rating) if imdb_rating is not None else None,
                "genres": [str(x) for x in genres],
                "directors": [str(x) for x in directors],
                "date_rated": str(row["date_rated"] or ""),
                "source": str(row["rating_source"] or ""),
            }
        )
    return out


def _rating_sort_key(row: dict, mode: str):
    if mode == "rating_desc":
        return (-int(row["user_rating"]), (row["title"] or "").casefold())
    if mode == "rating_asc":
        return (int(row["user_rating"]), (row["title"] or "").casefold())
    if mode == "imdb_desc":
        return (-(row["imdb_rating"] if row["imdb_rating"] is not None else -999.0), (row["title"] or "").casefold())
    if mode == "delta_desc":
        return (-(row["delta"] if row["delta"] is not None else -999.0), (row["title"] or "").casefold())
    if mode == "year_desc":
        return (-(row["year"] if row["year"] is not None else -1), (row["title"] or "").casefold())
    if mode == "title_asc":
        return ((row["title"] or "").casefold(), -(row["year"] or 0))
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


def install_library_ui(window_cls) -> None:
    """Install the full library/profile explorer on the Premium window class."""

    def page_ratings(self):
        page, content = self.page_shell(
            "Ratinguri IMDb",
            "Toată biblioteca ta de ratinguri, fără limita veche de 500. Caută, filtrează și sortează local.",
            [
                ("Import IMDb ratings.csv", self.import_ratings, True),
                ("Adaugă rating", self.manual_rating, False),
            ],
        )

        rows = load_rating_rows(self.db)
        all_genres = sorted({g for row in rows for g in row["genres"]}, key=str.casefold)

        summary = QFrame(); summary.setObjectName("PremiumCard")
        sl = QVBoxLayout(summary); sl.setContentsMargins(18, 16, 18, 16); sl.setSpacing(8)
        sh = QLabel(f"{len(rows):,} ratinguri în biblioteca personală")
        sh.setObjectName("SectionTitle"); sl.addWidget(sh)
        sd = QLabel("Dublu-click pe un film pentru pagina IMDb. Filtrele nu modifică ratingurile; doar organizează afișarea.")
        sd.setObjectName("Muted"); sd.setWordWrap(True); sl.addWidget(sd)
        content.addWidget(summary)

        controls = QFrame(); controls.setObjectName("PremiumCard")
        grid = QGridLayout(controls); grid.setContentsMargins(16, 14, 16, 14); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(8)

        search = QLineEdit(); search.setPlaceholderText("Caută titlu, titlu original, gen, regizor, an sau IMDb ID…")
        grid.addWidget(search, 0, 0, 1, 4)

        rating_filter = QComboBox(); rating_filter.addItem("Toate notele", None)
        for n in range(10, 0, -1):
            rating_filter.addItem(f"Doar {n}/10", n)
        grid.addWidget(rating_filter, 1, 0)

        genre_filter = QComboBox(); genre_filter.addItem("Toate genurile", "")
        for genre in all_genres:
            genre_filter.addItem(genre, genre)
        grid.addWidget(genre_filter, 1, 1)

        sort_combo = QComboBox()
        for label, key in (
            ("Cele mai recente", "date_desc"),
            ("Nota mea: mare → mică", "rating_desc"),
            ("Nota mea: mică → mare", "rating_asc"),
            ("IMDb: mare → mic", "imdb_desc"),
            ("Diferența mea vs IMDb", "delta_desc"),
            ("An: nou → vechi", "year_desc"),
            ("Titlu A–Z", "title_asc"),
        ):
            sort_combo.addItem(label, key)
        grid.addWidget(sort_combo, 1, 2)

        reset = QPushButton("Resetează")
        grid.addWidget(reset, 1, 3)
        content.addWidget(controls)

        count_label = QLabel("")
        count_label.setObjectName("Muted")
        content.addWidget(count_label)

        table = QTableWidget(0, 10)
        table.setHorizontalHeaderLabels([
            "Titlu", "An", "Nota ta", "IMDb", "Δ ta−IMDb", "Genuri",
            "Regizor", "Data", "Sursă", "IMDb ID",
        ])
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.setSortingEnabled(False)
        table.setMinimumHeight(620)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4, 7, 8, 9):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Interactive)
        header.setSectionResizeMode(6, QHeaderView.Interactive)
        table.setColumnWidth(5, 220)
        table.setColumnWidth(6, 190)
        content.addWidget(table)

        def open_imdb(row_index: int, _column: int):
            item = table.item(row_index, 0)
            imdb_id = item.data(Qt.UserRole) if item else None
            if imdb_id:
                QDesktopServices.openUrl(QUrl(f"https://www.imdb.com/title/{imdb_id}/"))

        table.cellDoubleClicked.connect(open_imdb)

        def filtered_rows() -> list[dict]:
            query = search.text().strip().casefold()
            wanted_rating = rating_filter.currentData()
            wanted_genre = str(genre_filter.currentData() or "")
            selected = []
            for row in rows:
                if wanted_rating is not None and int(row["user_rating"]) != int(wanted_rating):
                    continue
                if wanted_genre and wanted_genre not in row["genres"]:
                    continue
                if query:
                    haystack = " | ".join(
                        [
                            row["title"], row["original_title"], str(row["year"] or ""), row["imdb_id"],
                            " ".join(row["genres"]), " ".join(row["directors"]), row["source"],
                        ]
                    ).casefold()
                    if query not in haystack:
                        continue
                selected.append(row)

            mode = str(sort_combo.currentData() or "date_desc")
            if mode == "date_desc":
                selected.sort(key=_rating_sort_key, reverse=True)
            else:
                selected.sort(key=lambda r: _rating_sort_key(r, mode))
            return selected

        def render():
            selected = filtered_rows()
            table.setSortingEnabled(False)
            table.clearContents()
            table.setRowCount(len(selected))
            for i, row in enumerate(selected):
                values = [
                    SmartItem(row["title"], row["title"].casefold()),
                    SmartItem(str(row["year"] or "—"), row["year"]),
                    SmartItem(f"{row['user_rating']}/10", row["user_rating"]),
                    SmartItem(f"{row['imdb_rating']:.1f}" if row["imdb_rating"] is not None else "—", row["imdb_rating"]),
                    SmartItem(f"{row['delta']:+.1f}" if row["delta"] is not None else "—", row["delta"]),
                    SmartItem(", ".join(row["genres"]) or "—"),
                    SmartItem(", ".join(row["directors"]) or "—"),
                    SmartItem(row["date_rated"][:10] if row["date_rated"] else "—", row["date_rated"]),
                    SmartItem(row["source"] or "—"),
                    SmartItem(row["imdb_id"] or "—"),
                ]
                for j, item in enumerate(values):
                    item.setData(Qt.UserRole, row["imdb_id"] or "")
                    table.setItem(i, j, item)
            table.setSortingEnabled(True)
            count_label.setText(f"Afișate {len(selected):,} din {len(rows):,} ratinguri")

        debounce = QTimer(page); debounce.setSingleShot(True); debounce.setInterval(120); debounce.timeout.connect(render)
        search.textChanged.connect(lambda _text: debounce.start())
        rating_filter.currentIndexChanged.connect(lambda _i: render())
        genre_filter.currentIndexChanged.connect(lambda _i: render())
        sort_combo.currentIndexChanged.connect(lambda _i: render())

        def reset_filters():
            search.clear(); rating_filter.setCurrentIndex(0); genre_filter.setCurrentIndex(0); sort_combo.setCurrentIndex(0); render()

        reset.clicked.connect(reset_filters)
        render()
        return page

    def page_profile(self):
        profile = get_profile(self.db)
        page, content = self.page_shell(
            "Taste Hub",
            "Profilul calculat din ratingurile tale. Acum poți vedea toate semnalele, le poți căuta, filtra și sorta.",
        )

        rated = int(profile.get("rated_count", 0) or 0)
        mean = float(profile.get("global_mean_rating", 0) or 0)
        delta = profile.get("mean_user_minus_imdb")
        version = int(profile.get("version", 0) or 0)

        metrics = QGridLayout(); metrics.setHorizontalSpacing(12); metrics.setVerticalSpacing(12)
        metric_values = [
            (f"{rated:,}", "ratinguri analizate"),
            (f"{mean:.2f}", "media ta ponderată"),
            (f"{float(delta):+.2f}" if delta is not None else "—", "tu vs IMDb"),
            (f"v{version}", "versiune profil gust"),
        ]
        for i, (value, label) in enumerate(metric_values):
            card = QFrame(); card.setObjectName("PremiumCard")
            lay = QVBoxLayout(card); lay.setContentsMargins(18, 16, 18, 16)
            val = QLabel(value); val.setObjectName("MetricValue"); lay.addWidget(val)
            cap = QLabel(label); cap.setObjectName("Muted"); cap.setWordWrap(True); lay.addWidget(cap)
            metrics.addWidget(card, 0, i)
        metric_wrap = QFrame(); metric_wrap.setLayout(metrics); content.addWidget(metric_wrap)

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
