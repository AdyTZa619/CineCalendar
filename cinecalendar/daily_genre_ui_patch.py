from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


GENRES = (
    "Orice gen",
    "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "History", "Horror", "Mystery", "Romance", "Sci-Fi",
    "Sport", "Thriller", "War", "Western",
)


def install_daily_genre_ui_patch(window_cls) -> None:
    """Keep genre as an optional one-day intent, never a required step.

    Home always starts by choosing the best automatic recommendation from the personal ALS
    profile. The genre control is secondary and only matters when the user explicitly wants
    something specific in that moment.
    """
    original_settings = window_cls.page_settings

    def _today_genre(self) -> str:
        payload = self.db.get_setting("daily_genre_filter", {})
        if not isinstance(payload, dict) or str(payload.get("date") or "") != date.today().isoformat():
            return ""
        return str(payload.get("genre") or "").strip()

    def _set_today_genre(self, label: str) -> None:
        genre = "" if label == "Orice gen" else str(label).strip()
        self.db.set_setting("daily_genre_filter", {"date": date.today().isoformat(), "genre": genre})
        # Recommendation caches include this setting through the engine state token.
        self.show_page("today")

    def page_today(self):
        page, content = self.page_shell(
            "Ce văd acum?",
            "Îți aleg automat filmul cu cea mai bună potrivire pentru tine. Nu trebuie să setezi nimic.",
        )
        self.today_content = content
        _total, rated, cand = self.catalog_count()
        if cand <= 0:
            box = QFrame(); box.setObjectName("HeroCard")
            l = QVBoxLayout(box); l.setContentsMargins(26,26,26,26); l.setSpacing(12)
            h = QLabel("Catalogul personal nu este încă pregătit"); h.setObjectName("SectionTitle"); l.addWidget(h)
            d = QLabel(f"Am {rated:,} ratinguri de învățat. Catalogul IMDb și regizorii se descarcă și se leagă automat — nu introduci filme manual.")
            d.setObjectName("Muted"); d.setWordWrap(True); l.addWidget(d)
            b = QPushButton("Pregătește automat catalogul"); b.setProperty("accent",True); b.clicked.connect(lambda:self.bootstrap_catalog(False)); l.addWidget(b, alignment=Qt.AlignLeft)
            content.addWidget(box); content.addStretch(1); return page

        active = self._today_genre()
        text = "Analizez profilul colaborativ MovieLens, ratingurile tale, istoricul și contextul zilei."
        if active:
            text += f" Pentru azi ai cerut explicit genul {active}."
        content.addWidget(self.loading_panel("Îți aleg filmul…", text))

        # Secondary control: useful only when the user explicitly feels like watching a genre.
        chooser = QFrame(); chooser.setObjectName("PremiumCard")
        row = QHBoxLayout(chooser); row.setContentsMargins(16,10,16,10); row.setSpacing(12)
        label = QLabel("Opțional, doar dacă ai chef de ceva anume:")
        label.setObjectName("Muted"); row.addWidget(label)
        combo = QComboBox(); combo.addItems(list(GENRES)); combo.setMinimumWidth(180)
        current = active
        combo.setCurrentText(current if current in GENRES else "Orice gen")
        combo.currentTextChanged.connect(lambda value: self._set_today_genre(value))
        row.addWidget(combo)
        note = QLabel("Nu schimbă profilul; este valabil numai azi.")
        note.setObjectName("Muted"); row.addWidget(note, 1)
        content.addWidget(chooser)

        content.addStretch(1)
        QTimer.singleShot(0, self._load_today_async)
        return page

    def page_settings(self):
        page = original_settings(self)
        # Keep compatibility with old settings pages but remove the obsolete global Romance switch.
        for checkbox in page.findChildren(QCheckBox):
            if "romance" in checkbox.text().lower():
                checkbox.setChecked(False)
                checkbox.hide()
        return page

    window_cls._today_genre = _today_genre
    window_cls._set_today_genre = _set_today_genre
    window_cls.page_today = page_today
    window_cls.page_settings = page_settings
