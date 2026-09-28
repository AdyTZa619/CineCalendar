"""Three selectable visual shells for the production Qt interface.

The skin is presentation state. Recommendation rounds, exposures and user actions
stay in the existing service and decision methods.
"""
from __future__ import annotations

from datetime import date
import json

from PySide6.QtCore import Qt, QTimer, QEasingCurve, QPropertyAnimation, QSize, QPointF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QLinearGradient, QPolygonF
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget, QGraphicsOpacityEffect,
)


SKINS = {
    "cinematic": ("Cinematic", "Film în prim-plan, alternative într-un rând."),
    "editorial": ("Editorial", "Afiș și explicație, cu alternative alături."),
    "workbench": ("Workbench", "Rezultate comparabile și detalii la selecție."),
}

PALETTES = {
    "cinematic": dict(bg="#0B0B0D", surface="#151416", card="#1B1A1D", card2="#282529",
                      text="#F7F1E7", muted="#B8AFA5", border="#3B3535",
                      accent="#D9AF69", on="#21170B", good="#D7BB80"),
    "editorial": dict(bg="#F6F1E8", surface="#FBF8F1", card="#FFFCF6", card2="#EEE6DA",
                     text="#241F1D", muted="#6D625D", border="#D5C8BB",
                     accent="#792B30", on="#FFFFFF", good="#815C35"),
    "workbench": dict(bg="#10191D", surface="#152126", card="#1B292F", card2="#26373D",
                     text="#EDF3F1", muted="#ADBBB9", border="#375057",
                     accent="#E3A16D", on="#201912", good="#88CCBC"),
}


def normalized_skin(value: object) -> str:
    value = str(value or "").lower().strip()
    return value if value in SKINS else "cinematic"


def _label(text: str, name: str = "Muted", wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


ICON_NAMES = {
    "today": "play", "recommendations": "star", "romanian": "film",
    "romanian_list": "film", "calendar": "calendar", "month": "calendar",
    "profile": "person", "ratings": "list", "watchlist": "bookmark",
    "history": "history", "settings": "settings", "metadata_doctor": "database",
    "v5_lab": "flask", "updates": "download",
}


def _icon(key: str, color: str, size: int = 20) -> QIcon:
    """Small native vector icons, rendered at device resolution with no font dependency."""
    pix = QPixmap(size * 2, size * 2)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size * 2 / 24, size * 2 / 24)
    pen = QPen(QColor(color), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    name = ICON_NAMES.get(key, "grid")
    if name == "play":
        p.setBrush(QColor(color)); p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF([QPointF(7, 4), QPointF(20, 12), QPointF(7, 20)]))
    elif name == "star":
        import math
        points = [QPointF(12 + (9 if i % 2 == 0 else 4) * math.sin(i * math.pi / 5),
                          12 - (9 if i % 2 == 0 else 4) * math.cos(i * math.pi / 5)) for i in range(10)]
        p.drawPolygon(QPolygonF(points))
    elif name == "calendar":
        p.drawRoundedRect(3, 5, 18, 16, 2, 2); p.drawLine(3, 10, 21, 10)
        p.drawLine(8, 3, 8, 7); p.drawLine(16, 3, 16, 7)
        p.drawPoint(8, 14); p.drawPoint(13, 14); p.drawPoint(8, 18)
    elif name == "bookmark":
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in ((6,3),(18,3),(18,21),(12,17),(6,21),(6,3))]))
    elif name == "film":
        p.drawRoundedRect(3, 5, 18, 15, 2, 2); p.drawLine(3, 10, 21, 10)
        p.drawLine(8, 5, 11, 10); p.drawLine(15, 5, 18, 10)
    elif name == "person":
        p.drawEllipse(9, 3, 6, 6); p.drawArc(4, 11, 16, 11, 0, 180 * 16)
    elif name == "list":
        for y in (6, 12, 18):
            p.drawEllipse(3, y - 1, 2, 2); p.drawLine(9, y, 21, y)
    elif name == "history":
        p.drawArc(3, 3, 18, 18, 30 * 16, 295 * 16)
        p.drawLine(12, 6, 12, 12); p.drawLine(12, 12, 16, 14)
        p.drawLine(3, 5, 3, 10); p.drawLine(3, 10, 8, 10)
    elif name == "settings":
        p.drawEllipse(7, 7, 10, 10); p.drawEllipse(10, 10, 4, 4)
        for x1, y1, x2, y2 in ((12,2,12,6),(12,18,12,22),(2,12,6,12),(18,12,22,12),
                                (5,5,8,8),(16,16,19,19),(19,5,16,8),(8,16,5,19)):
            p.drawLine(x1, y1, x2, y2)
    elif name == "database":
        p.drawEllipse(3, 3, 18, 6); p.drawArc(3, 9, 18, 6, 180 * 16, 180 * 16)
        p.drawArc(3, 15, 18, 6, 180 * 16, 180 * 16)
        p.drawLine(3, 6, 3, 18); p.drawLine(21, 6, 21, 18)
    elif name == "flask":
        p.drawLine(9, 3, 15, 3); p.drawLine(10, 3, 10, 10); p.drawLine(14, 3, 14, 10)
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in ((10,10),(5,20),(6,21),(18,21),(19,20),(14,10))]))
        p.drawLine(7, 17, 17, 17)
    elif name == "download":
        p.drawLine(12, 3, 12, 16); p.drawPolyline(QPolygonF([QPointF(x,y) for x,y in ((7,11),(12,16),(17,11))]))
        p.drawPolyline(QPolygonF([QPointF(x,y) for x,y in ((4,17),(4,21),(20,21),(20,17))]))
    else:
        for x in (4, 13):
            for y in (4, 13): p.drawRoundedRect(x, y, 7, 7, 1, 1)
    p.end()
    return QIcon(pix)


def _nav_button(key: str, name: str, skin: str, handler, *, compact=False) -> QPushButton:
    button = QPushButton("" if compact else name)
    button.setProperty("nav", True)
    button.setIcon(_icon(key, PALETTES[skin]["accent"], 18 if compact else 17))
    button.setIconSize(QSize(19, 19))
    button.setToolTip(name)
    button.clicked.connect(handler)
    return button


class HeroCanvas(QFrame):
    """Paint a real poster behind cinematic content; resize never distorts the image."""

    def __init__(self, skin: str):
        super().__init__()
        self.skin = skin
        self.artwork = QPixmap()
        self.landscape = False
        self.setObjectName("ArtworkHero")
        self.setMinimumHeight(370 if skin == "cinematic" else 310)

    def set_artwork(self, pixmap: QPixmap):
        self.artwork = pixmap
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#111013" if self.skin == "cinematic" else "#141b20"))
        if not self.artwork.isNull():
            # Use a cached film backdrop when present; poster is a portrait fallback.
            image_width = (self.width() if self.landscape else
                           min(int(self.width() * .62), int(self.height() * .72)))
            image_area = self.rect().adjusted(self.width() - image_width, 0, 0, 0)
            scaled = self.artwork.scaled(image_area.size(), Qt.KeepAspectRatioByExpanding,
                                         Qt.SmoothTransformation)
            source_x = max(0, (scaled.width() - image_area.width()) // 2)
            source_y = max(0, (scaled.height() - image_area.height()) // 2)
            p.drawPixmap(image_area, scaled, scaled.rect().adjusted(source_x, source_y,
                         -(scaled.width() - image_area.width() - source_x),
                         -(scaled.height() - image_area.height() - source_y)))
        else:
            p.setPen(QPen(QColor(199, 148, 89, 55), 3))
            center_x, center_y = int(self.width() * .79), int(self.height() * .53)
            for radius in (55, 115, 174, 245):
                p.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)
            for x in range(max(0, center_x - 220), self.width(), 48):
                p.drawRoundedRect(x, self.height() - 34, 25, 16, 2, 2)
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0, QColor(8, 9, 11, 252))
        gradient.setColorAt(.48, QColor(8, 9, 11, 220))
        gradient.setColorAt(.82, QColor(8, 9, 11, 45))
        gradient.setColorAt(1, QColor(8, 9, 11, 18))
        p.fillRect(self.rect(), gradient)
        p.setPen(QPen(QColor("#5B4D3C"), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()


class PosterCanvas(QLabel):
    """Crop loaded artwork to the card at every window size, with a useful empty state."""

    def __init__(self, width: int, height: int, skin: str, available: bool):
        super().__init__()
        self.artwork = QPixmap()
        self.skin = skin
        self.available = available
        self.setObjectName("FilmPoster")
        self.setFixedHeight(height)
        self.setMinimumWidth(width)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_artwork(self, pixmap: QPixmap):
        self.artwork = pixmap
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setClipRect(self.rect())
        c = PALETTES[self.skin]
        p.fillRect(self.rect(), QColor(c["card2"]))
        if not self.artwork.isNull():
            scaled = self.artwork.scaled(self.size(), Qt.KeepAspectRatioByExpanding,
                                         Qt.SmoothTransformation)
            p.drawPixmap((self.width() - scaled.width()) // 2,
                         (self.height() - scaled.height()) // 2, scaled)
        else:
            pen = QPen(QColor(c["muted"]), 1.4)
            p.setPen(pen)
            mid = self.rect().center()
            p.drawRoundedRect(mid.x() - 20, mid.y() - 27, 40, 54, 3, 3)
            p.drawEllipse(mid.x() - 8, mid.y() - 15, 16, 16)
            p.drawLine(mid.x() - 11, mid.y() + 12, mid.x() + 11, mid.y() + 12)
            if self.width() > 115:
                p.drawText(self.rect().adjusted(4, 64, -4, -12), Qt.AlignBottom | Qt.AlignHCenter,
                           "Se încarcă" if self.available else "Fără afiș")
        p.end()


def _skin_preview(skin: str) -> QFrame:
    """Tiny layout preview made from Qt widgets, with no fictitious movie data."""
    c = PALETTES[skin]
    frame = QFrame()
    frame.setFixedHeight(84)
    frame.setStyleSheet(f"background:{c['bg']};border:1px solid {c['border']};border-radius:4px;")

    def block(width=0, stretch=0, fill=None):
        part = QFrame()
        part.setStyleSheet(f"background:{fill or c['card2']};border:0;border-radius:2px;")
        if width:
            part.setFixedWidth(width)
        return part, stretch

    if skin == "cinematic":
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        top, _ = block(fill=c['accent']); top.setFixedHeight(8); layout.addWidget(top)
        hero, _ = block(fill=c['card2']); layout.addWidget(hero, 1)
        rail = QHBoxLayout(); rail.setSpacing(4)
        for _ in range(3):
            tile, _ = block(); tile.setFixedHeight(13); rail.addWidget(tile, 1)
        layout.addLayout(rail)
    else:
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        if skin == "workbench":
            rail, _ = block(9, fill=c['accent']); layout.addWidget(rail)
        nav, _ = block(27 if skin == "workbench" else 36, fill=c['surface']); layout.addWidget(nav)
        main, _ = block(fill=c['card2']); layout.addWidget(main, 3)
        info, _ = block(fill=c['card']); layout.addWidget(info, 1 if skin == "editorial" else 2)
    return frame


def _status_widgets(window):
    window.undo_feedback_button = QPushButton("Anulează feedback")
    window.undo_feedback_button.setEnabled(False)
    window.undo_feedback_button.setToolTip("Anulează ultima acțiune de feedback din sesiune (Ctrl+Z).")
    window.undo_feedback_button.clicked.connect(window.undo_last_feedback)
    window.status = _label("Pregătit")
    window.status.setMaximumHeight(44)
    window.status.setWordWrap(True)
    window.progress = QProgressBar()
    window.progress.setVisible(False)
    window.progress.setRange(0, 0)


def build_skin_shell(window):
    window.skin = normalized_skin(window.db.get_setting("ui_skin", "cinematic"))
    root = QWidget()
    window.setCentralWidget(root)
    _status_widgets(window)
    window.nav_buttons = {}
    window.stack = QStackedWidget()

    if window.skin == "cinematic":
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top = QFrame()
        top.setObjectName("Topbar")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(30, 12, 30, 9)
        tl.setSpacing(9)
        first = QHBoxLayout()
        first.addWidget(_label("◉  CineCalendar", "Brand"))
        first.addSpacing(40)
        first.addWidget(_label("FILME PE GUSTUL TĂU", "NavGroup"))
        first.addStretch(1)
        first.addWidget(window.status)
        first.addWidget(window.progress)
        first.addWidget(window.undo_feedback_button)
        tl.addLayout(first)
        for chunk in (window.NAV[:7], window.NAV[7:]):
            row = QHBoxLayout()
            row.setSpacing(9)
            for key, name in chunk:
                button = _nav_button(key, name, window.skin,
                                     lambda _=False, k=key: window.show_page(k))
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                window.nav_buttons[key] = button
                row.addWidget(button, 1)
            tl.addLayout(row)
        outer.addWidget(top)
        outer.addWidget(window.stack, 1)
        return

    outer = QHBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    side = QFrame()
    side.setObjectName("Sidebar")
    side.setFixedWidth(260 if window.skin == "editorial" else 285)
    sl = QVBoxLayout(side)
    sl.setContentsMargins(15 if window.skin == "editorial" else 22, 25, 15, 15)
    sl.setSpacing(4)
    sl.addWidget(_label("◉  CineCalendar", "Brand"))
    sl.addWidget(_label("cinema pe gustul tău" if window.skin == "workbench"
                        else "Filme bune. La momentul potrivit.", "Muted"))
    sl.addSpacing(26)
    nav_scroll = QScrollArea()
    nav_scroll.setObjectName("SidebarNav")
    nav_scroll.setWidgetResizable(True)
    nav_scroll.setFrameShape(QFrame.NoFrame)
    nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    nav_body = QWidget()
    nav_body.setObjectName("SidebarNavContent")
    nl = QVBoxLayout(nav_body)
    nl.setContentsMargins(0, 0, 3, 0)
    nl.setSpacing(2)
    for index, (key, name) in enumerate(window.NAV):
        if window.skin == "editorial" and index in (0, 4, 7, 10):
            nl.addWidget(_label({0: "DESCOPERĂ", 4: "CONTUL MEU", 7: "PLANIFICĂ",
                                  10: "INSTRUMENTE"}[index], "NavGroup"))
        button = _nav_button(key, name, window.skin,
                             lambda _=False, k=key: window.show_page(k))
        nl.addWidget(button)
        window.nav_buttons[key] = button
    nl.addStretch(1)
    nav_scroll.setWidget(nav_body)
    sl.addWidget(nav_scroll, 1)
    sl.addWidget(window.undo_feedback_button)
    sl.addWidget(window.status)
    sl.addWidget(window.progress)
    sl.addWidget(_label("v" + str(window.windowTitle().split(" ")[1])
                        if len(window.windowTitle().split(" ")) > 1 else "CineCalendar"))
    outer.addWidget(side)
    outer.addWidget(window.stack, 1)


def skin_qss(skin: str) -> str:
    c = PALETTES[skin]
    editorial = skin == "editorial"
    sharp = skin == "workbench"
    radius = "5px" if sharp else ("3px" if editorial else "13px")
    title_font = "'Georgia'" if editorial else "'Segoe UI'"
    title_weight = "600" if editorial else "750"
    padding = "7px 9px" if sharp else "9px 13px"
    return f"""
        QWidget {{ background:{c['bg']}; color:{c['text']}; font-family:'Segoe UI'; font-size:14px; }}
        QMainWindow, QScrollArea, QScrollArea>QWidget>QWidget {{ background:{c['bg']}; }}
        QLabel {{ background:transparent; }}
        QFrame#Sidebar, QFrame#Topbar, QFrame#IconRail {{ background:{c['surface']}; border-color:{c['border']}; }}
        QFrame#Sidebar {{ border-right:1px solid {c['border']}; }}
        QFrame#Topbar {{ border-bottom:1px solid {c['border']}; }}
        QFrame#IconRail {{ border-right:1px solid {c['border']}; }}
        QScrollArea#SidebarNav, QWidget#SidebarNavContent {{ background:transparent; border:0; }}
        QFrame#PremiumCard, QFrame#Card, QFrame#HeroCard, QFrame#DetailHero {{
            background:{c['card']}; border:1px solid {c['border']}; border-radius:{radius};
        }}
        QFrame#HeroCard {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,
            stop:0 {c['card2']}, stop:.62 {c['card']}, stop:1 {c['bg']}); }}
        QLabel#Brand {{ font-family:{title_font}; font-size:{'21' if editorial else '25'}px; font-weight:750; color:{c['accent']}; }}
        QLabel#PageTitle {{ font-family:{title_font}; font-size:{'37' if editorial else '32'}px;
            font-weight:{title_weight}; color:{c['text']}; }}
        QLabel#HeroTitle {{ font-family:{title_font}; font-size:{'36' if editorial else '30'}px;
            font-weight:{title_weight}; }}
        QFrame#ArtworkHero {{ background:{c['card']}; border:1px solid {c['border']}; border-radius:8px; }}
        QFrame#ArtworkHero QLabel {{ background:transparent; }}
        QLabel#FilmPoster {{ background:{c['card2']}; color:{c['muted']}; border:1px solid {c['border']};
                             border-radius:6px; font-size:13px; }}
        QLabel#Eyebrow {{ color:{c['accent']}; font-size:11px; font-weight:750; letter-spacing:2px; }}
        QLabel#HeroDescription {{ font-family:{title_font}; font-size:17px; line-height:1.3; }}
        QFrame#AlternativeCard {{ background:{c['card']}; border:1px solid {c['border']}; border-radius:8px; }}
        QFrame#AlternativeCard:hover {{ border-color:{c['accent']}; }}
        QFrame#SkeletonBlock {{ background:{c['card2']}; border:0; border-radius:6px; }}
        QLabel#SectionTitle {{ font-family:{title_font}; font-size:21px; font-weight:700; }}
        QLabel#CardTitle {{ font-size:17px; font-weight:700; }}
        QLabel#Muted, QLabel#NavGroup {{ color:{c['muted']}; }}
        QLabel#NavGroup {{ font-size:10px; font-weight:700; margin-top:8px; }}
        QLabel#RailGlyph {{ font-size:21px; color:{c['accent']}; }}
        QPushButton#RailAction {{ border:0; background:transparent; color:{c['accent']};
            font-size:19px; padding:7px 2px; }}
        QPushButton#RailAction:hover {{ background:{c['card2']}; }}
        QLabel#Kicker, QLabel#Score, QLabel#MetricValue {{ color:{c['accent']}; }}
        QLabel#ScoreLarge, QLabel#SignalPositive {{ color:{c['good']}; }}
        QLabel#SignalNegative {{ color:#D97978; }}
        QLabel#Pill {{ background:{c['card2']}; color:{c['muted']}; border:1px solid {c['border']};
                        border-radius:{radius}; padding:5px 9px; }}
        QLabel#ScoreBadge {{ background:{c['accent']}; color:{c['on']}; border-radius:36px;
                             font-size:20px; font-weight:800; }}
        QFrame#ReliabilityGood, QFrame#ReliabilityCaution, QFrame#ReliabilityBad {{
            background:{c['card2']}; border:1px solid {c['border']}; border-radius:{radius}; }}
        QPushButton {{ background:{c['card2']}; color:{c['text']}; border:1px solid {c['border']};
                      border-radius:{radius}; padding:{padding}; font-weight:600; }}
        QPushButton:hover {{ border-color:{c['accent']}; }}
        QPushButton:focus {{ border-color:{c['accent']}; }}
        QPushButton:disabled {{ color:{c['muted']}; background:{c['surface']}; }}
        QPushButton[accent='true'] {{ background:{c['accent']}; color:{c['on']}; border-color:{c['accent']}; font-weight:750; }}
        QPushButton[nav='true'] {{ text-align:left; background:transparent; border:0; color:{c['muted']}; padding:10px 9px; font-size:{'13' if editorial else '14'}px; }}
        QPushButton[nav='true']:hover {{ background:{c['card2']}; color:{c['text']}; }}
        QPushButton[navActive='true'] {{ text-align:left; background:{c['card2']};
            color:{c['text']}; border-left:3px solid {c['accent']}; font-weight:750; padding:10px 12px; }}
        QFrame#Topbar QPushButton[nav='true'], QFrame#Topbar QPushButton[navActive='true'] {{
            text-align:center; font-size:12px; padding:8px 3px; }}
        QLineEdit, QSpinBox, QComboBox {{ background:{c['card2']}; color:{c['text']};
            border:1px solid {c['border']}; border-radius:{radius}; padding:7px; }}
        QComboBox QAbstractItemView {{ background:{c['surface']}; color:{c['text']};
            selection-background-color:{c['accent']}; selection-color:{c['on']}; }}
        QProgressBar {{ border:1px solid {c['border']}; border-radius:5px; background:{c['card2']}; }}
        QProgressBar::chunk {{ background:{c['accent']}; }}
        QTableView, QTableWidget {{ background:{c['card']}; alternate-background-color:{c['card2']};
            color:{c['text']}; gridline-color:{c['border']}; border:1px solid {c['border']};
            selection-background-color:{c['accent']}; selection-color:{c['on']}; }}
        QTableView::item, QTableWidget::item {{ color:{c['text']}; padding:5px; }}
        QTableView::item:selected, QTableWidget::item:selected {{ background:{c['accent']}; color:{c['on']}; }}
        QHeaderView::section {{ background:{c['surface']}; color:{c['text']}; border:0;
            border-bottom:1px solid {c['border']}; padding:8px; }}
        QScrollBar:vertical {{ background:transparent; width:11px; }}
        QScrollBar::handle:vertical {{ background:{c['border']}; min-height:30px; border-radius:5px; }}
    """


def _action(window, text, handler, primary=False):
    button = QPushButton(text)
    button.setProperty("accent", bool(primary))
    button.clicked.connect(handler)
    return button


def _film_title(rec) -> str:
    return rec.movie.title + (f"  ({rec.movie.year})" if rec.movie.year else "")


def _poster(window, rec, width, height, artwork_url=None):
    source = artwork_url or rec.movie.poster_url
    poster = PosterCanvas(width, height, normalized_skin(window.skin), bool(source))
    if source:
        window.load_poster_async(poster, source, rec.movie.imdb_id or str(rec.movie.id))
    return poster


def _cached_backdrop_url(window, rec) -> str | None:
    """Reuse TMDb details already fetched for metadata; no UI-thread network request."""
    if not rec.movie.id:
        return None
    try:
        with window.db.connect() as con:
            movie = con.execute("SELECT tmdb_id FROM movies WHERE id=?", (rec.movie.id,)).fetchone()
            if not movie or not movie["tmdb_id"]:
                return None
            rows = con.execute(
                "SELECT payload_json FROM metadata_cache WHERE provider='tmdb' "
                "AND cache_key LIKE ? ORDER BY fetched_at DESC LIMIT 3",
                (f"/movie/{movie['tmdb_id']}?%",),
            ).fetchall()
        for row in rows:
            path = json.loads(row["payload_json"]).get("backdrop_path")
            if isinstance(path, str) and path.startswith("/"):
                return "https://image.tmdb.org/t/p/w1280" + path
    except Exception:
        return None
    return None


def _film_panel(window, rec, *, cinematic=False):
    skin = normalized_skin(window.skin)
    if cinematic:
        box = HeroCanvas(skin)
        box.setMinimumHeight(405)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(40, 34, 28, 30)
        layout.setSpacing(22)
        info = QVBoxLayout()
        info.setSpacing(13)
        info.addStretch(1)
        info.addWidget(_label("RECOMANDAREA TA PENTRU ASTĂZI", "Eyebrow"))
        title = _label(_film_title(rec), "HeroTitle", True)
        title.setStyleSheet("font-family:Georgia;font-size:45px;color:#F8EAD1;")
        info.addWidget(title)
        info.addWidget(_label("   ·   ".join(window.movie_chips(rec.movie, 4)), "Muted", True))
        reason = _label(window.human_reason(rec), "HeroDescription", True)
        info.addWidget(reason)
        info.addSpacing(8)
        layout.addLayout(info, 3)
        layout.addStretch(2)
    else:
        box = QFrame()
        box.setObjectName("PremiumCard" if skin == "editorial" else "HeroCard")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(17 if skin == "editorial" else 18, 18, 20, 18)
        layout.setSpacing(30)
        backdrop = _cached_backdrop_url(window, rec) if skin == "workbench" else None
        poster = _poster(window, rec,
                         260 if skin == "editorial" else (390 if window.width() >= 1500 else 300),
                         450 if skin == "editorial" else 360, backdrop)
        layout.addWidget(poster, 0, Qt.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(13)
        info.addWidget(_label("RECOMANDAREA ZILEI" if skin == "editorial" else "ALEGEREA SERII", "Eyebrow"))
        title = _label(_film_title(rec), "HeroTitle", True)
        title.setStyleSheet(f"font-family:Georgia;font-size:{38 if skin == 'editorial' else 37}px;")
        info.addWidget(title)
        info.addWidget(_label("   ·   ".join(window.movie_chips(rec.movie, 5)), "Muted", True))
        if rec.movie.overview:
            synopsis = _label(rec.movie.overview[:530], "HeroDescription", True)
            info.addWidget(synopsis)
        info.addSpacing(8)
        info.addWidget(_label("DE CE ȚI-L RECOMANDĂM ASTĂZI?", "Eyebrow"))
        info.addWidget(_label(window.human_reason(rec), "HeroDescription", True))
        if rec.score.calendar_reason:
            info.addWidget(_label(rec.score.calendar_reason, "Muted", True))
        info.addStretch(1)
        layout.addLayout(info, 1)

    choose = _action(window, "Aleg pentru azi", lambda _=False, mid=rec.movie.id: window.choose_decision(mid), True)
    choose.setIcon(_icon("today", PALETTES[skin]["on"]))
    details = _action(window, "De ce acesta?", lambda _=False, r=rec: window.open_details(r))
    feedback = QPushButton("Nu acum / motiv")
    feedback.setToolTip("Alege un motiv temporar sau spune ce nu ți se potrivește.")
    feedback.clicked.connect(lambda _=False, mid=rec.movie.id, button=feedback:
                             window.contextual_feedback_menu(mid, button))
    skip = _action(window, "Alt film", lambda _=False, mid=rec.movie.id: window.skip_decision(mid))
    actions = QHBoxLayout() if cinematic else QGridLayout()
    actions.setSpacing(9)
    for index, button in enumerate((choose, details, feedback, skip)):
        if cinematic:
            actions.addWidget(button)
        else:
            actions.addWidget(button, index // 2, index % 2)
    info.addLayout(actions)
    if cinematic:
        info.addSpacing(13)
        reliability = window.reliability_gate.evaluate(rec)
        info.addWidget(_label(f"{reliability.label}  ·  Estimare personală {rec.score.predicted_rating:.1f}/10", "Muted", True))
        info.addStretch(1)
        backdrop = _cached_backdrop_url(window, rec)
        box.landscape = bool(backdrop)
        artwork_url = backdrop or rec.movie.poster_url
        if artwork_url:
            window.load_poster_async(box, artwork_url,
                                     (rec.movie.imdb_id or str(rec.movie.id)) + (":backdrop" if backdrop else ""))
    else:
        info.addWidget(window.reliability_widget(rec, compact=True))
    return box


def _alternative(window, rec, index, *, variant=None):
    variant = variant or normalized_skin(window.skin)
    box = QFrame()
    box.setObjectName("AlternativeCard")
    visual = variant in ("cinematic", "workbench")
    layout = QVBoxLayout(box) if visual else QHBoxLayout(box)
    layout.setContentsMargins(0 if visual else 11, 0 if visual else 10,
                              0 if visual else 11, 10 if visual else 10)
    layout.setSpacing(7 if visual else 12)
    poster = _poster(window, rec, 210 if visual else 72, 158 if visual else 115)
    if visual:
        poster.setMinimumWidth(1)
    if visual:
        layout.addWidget(poster)
    else:
        layout.addWidget(poster, 0, Qt.AlignTop)
    detail = QVBoxLayout()
    detail.setContentsMargins(12 if visual else 0, 2, 12 if visual else 0, 0)
    detail.addWidget(_label(f"{index}.  {_film_title(rec)}", "CardTitle", True))
    detail.addWidget(_label(" · ".join(window.movie_chips(rec.movie, 3)), "Muted", True))
    detail.addWidget(_label(f"Estimare personală {rec.score.predicted_rating:.1f}/10", "Score", True))
    actions = QHBoxLayout()
    actions.addWidget(_action(window, "Detalii", lambda _=False, r=rec: window.open_details(r)))
    actions.addWidget(_action(window, "Aleg", lambda _=False, mid=rec.movie.id: window.choose_decision(mid)))
    detail.addLayout(actions)
    layout.addLayout(detail, 1)
    return box


def _quick_controls(window):
    bar = QFrame()
    bar.setObjectName("PremiumCard")
    layout = QVBoxLayout(bar)
    layout.setContentsMargins(14, 9, 14, 9)
    layout.setSpacing(6)
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(_label("Cum alegem:", "Muted"))
    for title, key in (("Echilibrat", "decide"), ("Mai sigur", "safe"),
                       ("Surprinde-mă", "surprise"), ("Mai scurt", "short")):
        row.addWidget(_action(window, title,
                    lambda _=False, value=key: window.set_decision_mode(value),
                    key == window.decision_mode))
    row.addStretch(1)
    layout.addLayout(row)
    filters = QHBoxLayout()
    filters.setSpacing(8)
    filters.addWidget(_label("Filtre rapide:", "Muted"))
    runtime = QComboBox()
    for name, value in (("Orice durată", "all"), ("≤60 min", "60"),
                        ("≤90 min", "90"), ("≤120 min", "120"), ("180+ min", "180plus")):
        runtime.addItem(name, value)
    runtime.setCurrentIndex(max(0, runtime.findData(str(window.db.get_setting("chooser_runtime_bucket", "all")))))
    runtime.currentIndexChanged.connect(lambda _=0: _set_filter(window, "chooser_runtime_bucket", runtime.currentData()))
    filters.addWidget(runtime)
    mood = QComboBox()
    for name, value in (("Orice ton", "neutral"), ("Lejer", "light"),
                        ("Intens", "intense"), ("Contemplativ", "contemplative"),
                        ("Ușor de urmărit", "easy")):
        mood.addItem(name, value)
    mood.setCurrentIndex(max(0, mood.findData(str(window.db.get_setting("chooser_mood", "neutral")))))
    mood.currentIndexChanged.connect(lambda _=0: _set_filter(window, "chooser_mood", mood.currentData()))
    filters.addWidget(mood)
    filters.addStretch(1)
    layout.addLayout(filters)
    return bar


def _set_filter(window, name, value):
    window.db.set_setting(name, str(value or ""))
    window.show_page("today")


def render_skin_today(window, primary, backups):
    content = window.today_content
    if content is None:
        return
    window._clear_layout(content)
    if primary is None:
        content.addWidget(_label("Nu am găsit momentan un titlu suficient de bun după filtrele tale.", "Muted", True))
        return
    visible = [primary, *list(backups or [])[:2]]
    window.reliability_gate.refresh()
    window.record_once(visible, date.today(), "decision")
    window._schedule_retrieval_shadow(visible, "decision")
    skin = normalized_skin(getattr(window, "skin", None))
    if skin == "cinematic":
        content.addWidget(_film_panel(window, primary, cinematic=True))
        if backups:
            heading = QHBoxLayout()
            heading.addWidget(_label("Alte recomandări pentru tine", "SectionTitle"))
            heading.addStretch(1)
            more = _action(window, "Vezi toate recomandările  →", lambda: window.show_page("recommendations"))
            heading.addWidget(more)
            header = QWidget(); header.setLayout(heading); content.addWidget(header)
            row = QHBoxLayout()
            row.setSpacing(14)
            for i, rec in enumerate(backups[:2], 2):
                row.addWidget(_alternative(window, rec, i), 1)
            wrap = QWidget()
            wrap.setLayout(row)
            content.addWidget(wrap)
    elif skin == "editorial":
        narrow = window.width() < 1400
        row = QVBoxLayout() if narrow else QHBoxLayout()
        row.setSpacing(18)
        row.addWidget(_film_panel(window, primary), 0 if narrow else 7)
        side = QFrame()
        side.setObjectName("PremiumCard")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(15, 18, 15, 16)
        sl.setSpacing(14)
        sl.addWidget(_label("ALTERNATIVE PENTRU DISEARĂ", "Eyebrow", True))
        for i, rec in enumerate(backups[:2], 2):
            sl.addWidget(_alternative(window, rec, i, variant="editorial"))
        see_all = _action(window, "Toate recomandările  →", lambda: window.show_page("recommendations"))
        sl.addWidget(see_all)
        sl.addStretch(1)
        row.addWidget(side, 0 if narrow else 3)
        wrap = QWidget()
        wrap.setLayout(row)
        content.addWidget(wrap)
    else:
        inspector = QStackedWidget()
        buttons = []
        for i, rec in enumerate(visible, 1):
            inspector.addWidget(_film_panel(window, rec))
        inspector.setMinimumHeight(405)
        content.addWidget(inspector)
        heading = QHBoxLayout()
        heading.addWidget(_label("Alternative pentru azi", "SectionTitle"))
        heading.addStretch(1)
        heading.addWidget(_action(window, "Vezi toate  →", lambda: window.show_page("recommendations")))
        head = QWidget(); head.setLayout(heading); content.addWidget(head)
        row = QHBoxLayout()
        row.setSpacing(12)
        for i, rec in enumerate(visible, 1):
            selector = _alternative(window, rec, i, variant="workbench")
            pick = _action(window, "Selectează", lambda _=False, index=i-1: _select(inspector, buttons, index))
            selector.layout().itemAt(1).layout().addWidget(pick)
            buttons.append(pick)
            row.addWidget(selector, 1)
        wrap = QWidget(); wrap.setLayout(row); content.addWidget(wrap)
        inspector.setProperty("revealOnSelect", False)
        _select(inspector, buttons, 0)
    content.addWidget(_quick_controls(window))
    content.addStretch(1)


def _select(stack, buttons, index):
    stack.setCurrentIndex(index)
    for i, button in enumerate(buttons):
        button.setProperty("accent", i == index)
        button.style().unpolish(button)
        button.style().polish(button)
    if stack.property("revealOnSelect"):
        page = stack.window().stack.currentWidget()
        scroll = next(iter(page.findChildren(QScrollArea)), None)
        if scroll is not None:
            def reveal():
                try:
                    if stack.window().stack.currentWidget() is page:
                        scroll.ensureWidgetVisible(stack)
                except RuntimeError:
                    pass  # The user may have changed pages before the queued scroll.
            QTimer.singleShot(0, reveal)


def install_premium_skins(window_cls) -> None:
    if getattr(window_cls, "_cinecalendar_skin_installed", False):
        return
    original_settings = window_cls.page_settings
    original_page_shell = window_cls.page_shell
    original_resize_event = window_cls.resizeEvent
    original_show_page = window_cls.show_page

    def loading_panel(self, title, subtitle):
        box = QFrame()
        box.setObjectName("HeroCard")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(24, 23, 24, 23)
        layout.setSpacing(25)
        portrait = normalized_skin(getattr(self, "skin", None)) == "editorial"
        art = QFrame()
        art.setObjectName("SkeletonBlock")
        art.setFixedSize(230 if portrait else 340, 300 if portrait else 280)
        if portrait:
            layout.addWidget(art)
        content = QVBoxLayout()
        content.setSpacing(13)
        content.addWidget(_label("PREGĂTIM SELECȚIA", "Eyebrow"))
        content.addWidget(_label(title, "HeroTitle", True))
        content.addWidget(_label(subtitle, "Muted", True))
        for width in (340, 270, 310):
            line = QFrame(); line.setObjectName("SkeletonBlock")
            line.setFixedSize(width, 15)
            content.addWidget(line)
        content.addStretch(1)
        progress = QProgressBar()
        progress.setRange(0, 0)
        progress.setTextVisible(False)
        progress.setFixedHeight(8)
        content.addWidget(progress)
        layout.addLayout(content, 1)
        if not portrait:
            layout.addWidget(art)
        return box

    def _build_shell(self):
        build_skin_shell(self)

    def apply_theme(self):
        self.skin = normalized_skin(self.db.get_setting("ui_skin", "cinematic"))
        self.theme = "light" if self.skin == "editorial" else "dark"
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(skin_qss(self.skin))

    def set_skin(self, value):
        value = normalized_skin(value)
        if value == getattr(self, "skin", None):
            if self.db.get_setting("ui_skin") != value:
                self.db.set_setting("ui_skin", value)
            return
        self.db.set_setting("ui_skin", value)
        self.skin = value
        self._build_shell()
        self.apply_theme()
        self.show_page("settings")
        self.set_status(f"Skin activ: {SKINS[value][0]}. Alegerea este salvată.", False)

    def page_settings(self):
        page = original_settings(self)
        scroll = next((item for item in page.findChildren(QScrollArea)
                       if item.widget() is not None and item.widget().layout() is not None), None)
        if scroll is None:
            return page
        content = scroll.widget().layout()
        panel = QFrame()
        panel.setObjectName("PremiumCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(_label("Aspectul aplicației", "SectionTitle"))
        layout.addWidget(_label("Alege cum sunt așezate meniurile și recomandările. Schimbarea se aplică imediat și se păstrează după repornire.", "Muted", True))
        cards = QHBoxLayout()
        for key, (name, description) in SKINS.items():
            item = QFrame()
            item.setObjectName("PremiumCard")
            column = QVBoxLayout(item)
            column.addWidget(_label(name, "CardTitle"))
            column.addWidget(_skin_preview(key))
            column.addWidget(_label(description, "Muted", True))
            button = _action(self, "Activ" if key == self.skin else "Folosește skinul",
                             lambda _=False, selected=key: self.set_skin(selected),
                             key == self.skin)
            column.addStretch(1)
            column.addWidget(button)
            cards.addWidget(item, 1)
        layout.addLayout(cards)
        content.insertWidget(0, panel)
        return page

    def page_shell(self, title, subtitle="", actions=None):
        page, content = original_page_shell(self, title, subtitle, actions)
        if getattr(self, "skin", None) == "cinematic" and title == "Ce văd acum?" and actions:
            outer = page.layout()
            outer.setContentsMargins(24, 12, 24, 12)
            top = outer.itemAt(0).layout()
            if top is not None and top.count() >= 3:
                title_item = top.takeAt(0)
                actions_item = top.takeAt(top.count() - 1)
                title_widget, actions_layout = title_item.widget(), actions_item.layout()
                if title_widget is not None and actions_layout is not None:
                    row = QHBoxLayout()
                    row.addWidget(title_widget, 1)
                    row.addLayout(actions_layout)
                    top.insertLayout(0, row)
        return page, content

    def resizeEvent(self, event):
        old_width = event.oldSize().width()
        original_resize_event(self, event)
        if (old_width > 0 and (old_width < 1400) != (event.size().width() < 1400)
                and getattr(self, "current_page", None) == "today"
                and getattr(self, "today_result", None) is not None):
            QTimer.singleShot(0, lambda: self.show_page("today")
                              if not getattr(self, "_ui_closing", False)
                              and self.current_page == "today" else None)

    def show_page(self, key):
        original_show_page(self, key)
        page = self.stack.currentWidget()
        if page is None or not self.isVisible():
            return
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", page)
        animation.setDuration(190)
        animation.setStartValue(.35)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        animation.finished.connect(lambda target=page: target.setGraphicsEffect(None))
        page._skin_animation = animation
        animation.start()

    window_cls._build_shell = _build_shell
    window_cls.apply_theme = apply_theme
    window_cls.set_skin = set_skin
    window_cls.page_settings = page_settings
    window_cls.page_shell = page_shell
    window_cls.resizeEvent = resizeEvent
    window_cls.show_page = show_page
    window_cls.loading_panel = loading_panel
    window_cls._render_today = render_skin_today
    window_cls._cinecalendar_skin_installed = True
