from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


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
            [("Recalculează alegerea", self.recalculate_today, True)],
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
        # Secondary control: useful only when the user explicitly feels like watching a genre.
        chooser = QFrame(); chooser.setObjectName("PremiumCard")
        compact = bool(getattr(self, "skin", None))
        row = QHBoxLayout(chooser) if compact else QGridLayout(chooser)
        row.setContentsMargins(16, 7 if compact else 10, 16, 7 if compact else 10); row.setSpacing(8)
        label = QLabel("Genul de azi (opțional):" if compact else "Opțional, doar dacă ai chef de ceva anume:")
        label.setObjectName("Muted"); label.setWordWrap(not compact)
        if compact: row.addWidget(label)
        else: row.addWidget(label,0,0)
        combo = QComboBox(); combo.addItems(list(GENRES)); combo.setMinimumWidth(180)
        combo.setToolTip("Nu schimbă profilul; filtrul este valabil numai azi.")
        current = active
        combo.setCurrentText(current if current in GENRES else "Orice gen")
        combo.currentTextChanged.connect(lambda value: self._set_today_genre(value))
        if compact:
            row.addStretch(1); row.addWidget(combo)
        else:
            row.addWidget(combo,0,1)
            note = QLabel("Nu schimbă profilul; este valabil numai azi.")
            note.setObjectName("Muted"); note.setWordWrap(True); row.addWidget(note,1,0,1,2)
            row.setColumnStretch(0,1)
        # Ranking clears the result layout; keep this control outside it.
        page.layout().insertWidget(1, chooser)

        if self.today_result is not None and self.today_cache_signature == self._today_signature():
            self._render_today(*self.today_result)
            return page

        content.addWidget(self.loading_panel("Îți aleg filmul…", text))
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
