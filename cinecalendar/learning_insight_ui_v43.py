from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
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

from .recommendation_history_v43 import history_engine_versions, recommendation_history_rows


_STATUS_LABELS = {
    "shown": "afișat",
    "chosen": "ales",
    "started": "pornit",
    "watched": "văzut",
    "rated": "notat",
    "skipped": "sărit",
    "ignored": "expirat",
}


class RecommendationHistoryDetailDialog(QDialog):
    def __init__(self, row: dict, owner):
        super().__init__(owner)
        self.setWindowTitle("Detalii recomandare")
        self.resize(820, 700)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)

        title = QLabel(row["title"])
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        root.addWidget(title)

        summary = QLabel(
            f"{row['context_date']} • poziția {row['rank'] or '—'} • "
            f"{_STATUS_LABELS.get(row['status'], row['status'])} • "
            f"motor {row['engine_version'] or 'legacy'}"
        )
        summary.setObjectName("Muted")
        summary.setWordWrap(True)
        root.addWidget(summary)

        score = QLabel(
            " • ".join([
                f"estimat {row['predicted_rating']:.1f}/10" if row["predicted_rating"] is not None else "estimare nesalvată",
                f"potrivire {row['final_score']*100:.0f}%" if row["final_score"] is not None else "scor nesalvat",
                f"încredere {row['confidence']*100:.0f}%" if row["confidence"] is not None else "încredere nesalvată",
                f"real {row['actual_rating']}/10" if row["actual_rating"] is not None else "fără rating ulterior",
            ])
        )
        score.setObjectName("BodyStrong")
        score.setWordWrap(True)
        root.addWidget(score)

        if not row.get("snapshot_available"):
            old = QLabel(
                "Expunere veche: versiunea care a creat această recomandare nu salva explicația exactă. "
                "Scorurile, acțiunile și outcome-ul existente rămân afișate, dar motivarea nu este reconstruită retroactiv."
            )
            old.setObjectName("Muted")
            old.setWordWrap(True)
            root.addWidget(old)
        else:
            reason_box = QFrame()
            reason_box.setObjectName("PremiumCard")
            rl = QVBoxLayout(reason_box)
            rh = QLabel("Motivul exact salvat")
            rh.setObjectName("SectionTitle")
            rl.addWidget(rh)
            reason = QLabel(row.get("personal_reason") or "—")
            reason.setWordWrap(True)
            rl.addWidget(reason)
            if row.get("why_not"):
                wh = QLabel("Compromisuri")
                wh.setObjectName("BodyStrong")
                rl.addWidget(wh)
                why = QLabel(row["why_not"])
                why.setObjectName("Muted")
                why.setWordWrap(True)
                rl.addWidget(why)
            root.addWidget(reason_box)

            factors = dict(row.get("score_factors") or {})
            if factors:
                fb = QFrame()
                fb.setObjectName("PremiumCard")
                fl = QVBoxLayout(fb)
                fh = QLabel("Factori salvați")
                fh.setObjectName("SectionTitle")
                fl.addWidget(fh)
                for name, value in factors.items():
                    line = QHBoxLayout()
                    line.addWidget(QLabel(str(name)), 1)
                    try:
                        shown = f"{float(value):+.3f}"
                    except Exception:
                        shown = str(value)
                    val = QLabel(shown)
                    val.setObjectName("Muted")
                    line.addWidget(val)
                    fl.addLayout(line)
                root.addWidget(fb)

            contributions = list(row.get("contributions") or [])
            if contributions:
                cb = QFrame()
                cb.setObjectName("PremiumCard")
                cl = QVBoxLayout(cb)
                ch = QLabel("Contribuțiile de la momentul recomandării")
                ch.setObjectName("SectionTitle")
                cl.addWidget(ch)
                for item in contributions[:12]:
                    try:
                        name, points, explanation = item
                    except Exception:
                        continue
                    head = QLabel(f"{name}: {float(points):+.1f}")
                    head.setObjectName("BodyStrong")
                    cl.addWidget(head)
                    if explanation:
                        expl = QLabel(str(explanation))
                        expl.setObjectName("Muted")
                        expl.setWordWrap(True)
                        cl.addWidget(expl)
                root.addWidget(cb)

        actions = QHBoxLayout()
        if row.get("imdb_id"):
            imdb = QPushButton("Deschide IMDb")
            imdb.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl(f"https://www.imdb.com/title/{row['imdb_id']}/")
                )
            )
            actions.addWidget(imdb)
        actions.addStretch(1)
        close = QPushButton("Închide")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        root.addLayout(actions)


def install_learning_insight_ui_v43(window_cls) -> None:
    """Replace the legacy five-column history with an outcome-aware 4.3 explorer."""

    def page_history(self):
        page, content = self.page_shell(
            "Istoric recomandări",
            "Fiecare expunere, poziția de atunci, predicția, outcome-ul și explicația exactă când snapshotul este disponibil.",
        )

        controls = QFrame()
        controls.setObjectName("PremiumCard")
        grid = QGridLayout(controls)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        search = QLineEdit()
        search.setPlaceholderText("Caută titlu, context sau motor…")
        grid.addWidget(search, 0, 0, 1, 3)

        period = QComboBox()
        for label, key in (
            ("Toată perioada", "all"),
            ("Ultimele 30 zile", "30"),
            ("Ultimele 90 zile", "90"),
            ("Anul curent", "year"),
        ):
            period.addItem(label, key)
        grid.addWidget(period, 0, 3)

        status = QComboBox()
        status.addItem("Toate stările", "")
        for key in ("rated", "watched", "started", "chosen", "skipped", "ignored", "shown"):
            status.addItem(_STATUS_LABELS[key].capitalize(), key)
        grid.addWidget(status, 0, 4)

        engine = QComboBox()
        engine.addItem("Toate motoarele", "")
        for version in history_engine_versions(self.db):
            engine.addItem(version, version)
        grid.addWidget(engine, 0, 5)

        count_label = QLabel("")
        count_label.setObjectName("Muted")
        grid.addWidget(count_label, 1, 0, 1, 6)
        content.addWidget(controls)

        table = QTableWidget(0, 11)
        table.setHorizontalHeaderLabels([
            "Data", "Titlu", "#", "Estimat", "Potrivire", "Încredere",
            "Motor", "Status", "Real", "Eroare", "Context",
        ])
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (0, 2, 3, 4, 5, 7, 8, 9):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Interactive)
        header.setSectionResizeMode(10, QHeaderView.Interactive)
        table.setColumnWidth(6, 170)
        table.setColumnWidth(10, 150)
        table.setMinimumHeight(650)
        content.addWidget(table)

        cache: list[dict] = []

        def period_days() -> int | None:
            key = str(period.currentData() or "all")
            if key == "30":
                return 30
            if key == "90":
                return 90
            if key == "year":
                return max(0, (date.today() - date(date.today().year, 1, 1)).days)
            return None

        def render():
            nonlocal cache
            rows = recommendation_history_rows(
                self.db,
                days=period_days(),
                status=str(status.currentData() or ""),
                engine_version=str(engine.currentData() or ""),
                limit=4000,
            )
            needle = search.text().strip().casefold()
            if needle:
                rows = [
                    row for row in rows
                    if needle in (
                        row["title"] + " " + row["slot"] + " " + row["engine_version"]
                    ).casefold()
                ]
            cache = rows
            table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                values = [
                    row["recommended_at"][:16].replace("T", " "),
                    row["title"],
                    str(row["rank"] or "—"),
                    f"{row['predicted_rating']:.1f}" if row["predicted_rating"] is not None else "—",
                    f"{row['final_score']*100:.0f}%" if row["final_score"] is not None else "—",
                    f"{row['confidence']*100:.0f}%" if row["confidence"] is not None else "—",
                    row["engine_version"] or "legacy",
                    _STATUS_LABELS.get(row["status"], row["status"]),
                    f"{row['actual_rating']}/10" if row["actual_rating"] is not None else "—",
                    f"{row['absolute_error']:.2f}" if row["absolute_error"] is not None else "—",
                    row["slot"],
                ]
                for j, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if j == 1:
                        item.setData(Qt.UserRole, i)
                        tip = row.get("personal_reason") or (
                            "Explicația exactă nu a fost salvată de versiunea veche."
                        )
                        item.setToolTip(tip)
                    table.setItem(i, j, item)
            snapshots = sum(bool(row.get("snapshot_available")) for row in rows)
            count_label.setText(
                f"{len(rows):,} expuneri • {snapshots:,} cu explicația exactă salvată • "
                "dublu-click pe un rând pentru detalii"
            )

        def open_row(row_idx: int, _column: int):
            if 0 <= row_idx < len(cache):
                RecommendationHistoryDetailDialog(cache[row_idx], self).exec()

        search.textChanged.connect(lambda _text: render())
        period.currentIndexChanged.connect(lambda _idx: render())
        status.currentIndexChanged.connect(lambda _idx: render())
        engine.currentIndexChanged.connect(lambda _idx: render())
        table.cellDoubleClicked.connect(open_row)
        render()
        return page

    window_cls.page_history = page_history
    window_cls._cinecalendar_v43_history_ui = True
