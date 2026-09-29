"""Four selectable visual shells for the production Qt interface.

The skin is presentation state. Recommendation rounds, exposures and user actions
stay in the existing service and decision methods.
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QEasingCurve, QPropertyAnimation, QSize, QPointF, QRectF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QLinearGradient, QPolygonF, QFont, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QGraphicsOpacityEffect,
)


SKINS = {
    "cinematic": ("Cinema Immersive", "Cadru panoramic, navigare sus și selecția săptămânii."),
    "editorial": ("Cinematheque Editorial", "Afiș mare, poveste tipografică și coloană de sugestii."),
    "poster_wall": ("Poster Wall", "Perete de afișe, filtre și calendar lateral."),
    "workbench": ("Decision Studio", "Compară filmele și inspectează motivele dintr-un panou lateral."),
}

PALETTES = {
    "cinematic": dict(bg="#0B0B0D", surface="#151416", card="#1B1A1D", card2="#282529",
                      text="#F7F1E7", muted="#B8AFA5", border="#3B3535",
                      accent="#D9AF69", on="#21170B", good="#D7BB80"),
    "editorial": dict(bg="#F6F1E8", surface="#FBF8F1", card="#FFFCF6", card2="#EEE6DA",
                     text="#241F1D", muted="#6D625D", border="#D5C8BB",
                     accent="#792B30", on="#FFFFFF", good="#815C35"),
    "poster_wall": dict(bg="#101418", surface="#15191D", card="#1A2228", card2="#263039",
                         text="#F4F1EC", muted="#B5BEC2", border="#344047",
                         accent="#F2A263", on="#171719", good="#F6C577"),
    "workbench": dict(bg="#10191D", surface="#152126", card="#1B292F", card2="#26373D",
                     text="#EDF3F1", muted="#ADBBB9", border="#375057",
                     accent="#E3A16D", on="#201912", good="#88CCBC"),
}


@lru_cache(maxsize=4)
def _fallback_art(key: str) -> QPixmap:
    filename = {"cinematic": "immersive_scene.png", "editorial": "editorial_scene.png",
                "poster_wall": "poster_wall_scene.png", "workbench": "studio_scene.png"}[key]
    return QPixmap(str(Path(__file__).resolve().parent / "assets" / filename))


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


def _display_nav(key: str, name: str) -> str:
    return {"today": "Ce văd acum?", "recommendations": "Recomandări",
            "romanian": "Recomandări românești", "romanian_list": "Filme RO · Cronologie",
            "month": "Program calendar", "history": "Istoric",
            "updates": "Actualizări"}.get(key, name)


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
    button = QPushButton("" if compact else _display_nav(key, name))
    button.setProperty("nav", True)
    button.setIcon(_icon(key, "#F9EEE8" if skin == "editorial" else PALETTES[skin]["accent"],
                         18 if compact else 17))
    button.setIconSize(QSize(19, 19))
    button.setToolTip(name)
    button.clicked.connect(handler)
    return button


class HeroCanvas(QFrame):
    """Paint a real poster behind cinematic content; resize never distorts the image."""

    def __init__(self, skin: str, title: str = "", year: int | None = None):
        super().__init__()
        self.skin = skin
        self.artwork = _fallback_art(skin)
        self.illustrative = not self.artwork.isNull()
        self.landscape = self.illustrative
        self.title = title
        self.year = year
        self.setObjectName("ArtworkHero")
        self.setMinimumHeight(370 if skin == "cinematic" else 310)

    def set_artwork(self, pixmap: QPixmap):
        self.artwork = pixmap
        self.illustrative = False
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
            # A typographic cover for missing artwork. It deliberately uses only
            # the actual title and year instead of inventing a film image.
            w, h = self.width(), self.height()
            glow = QRadialGradient(w * .82, h * .42, w * .48)
            glow.setColorAt(0, QColor(167, 91, 54, 135))
            glow.setColorAt(.62, QColor(95, 49, 41, 85))
            glow.setColorAt(1, QColor(15, 13, 17, 0))
            p.fillRect(self.rect(), glow)
            p.setPen(QPen(QColor(230, 184, 116, 72), 1.5))
            center_x, center_y = int(w * .82), int(h * .52)
            for radius in (75, 150, 225, 300):
                p.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)
            p.setPen(QColor(242, 210, 165, 95))
            font = QFont("Georgia", max(22, min(57, w // 26)))
            font.setWeight(QFont.DemiBold)
            title_area = QRectF(w * .62, h * .3, w * .33, h * .42)
            for size in range(font.pointSize(), 16, -2):
                font.setPointSize(size)
                p.setFont(font)
                if p.boundingRect(title_area, Qt.TextWordWrap, self.title).height() <= title_area.height():
                    break
            p.drawText(title_area, Qt.TextWordWrap | Qt.AlignCenter, self.title)
            p.setFont(QFont("Segoe UI", 11, QFont.DemiBold))
            p.drawText(QRectF(w * .68, h * .78, w * .27, 30), Qt.AlignRight,
                       f"{self.year or ''}  ·  CINECALENDAR")
        gradient = QLinearGradient(0, 0, self.width(), 0)
        gradient.setColorAt(0, QColor(8, 9, 11, 252))
        gradient.setColorAt(.40, QColor(8, 9, 11, 125))
        gradient.setColorAt(.75, QColor(8, 9, 11, 12))
        gradient.setColorAt(1, QColor(8, 9, 11, 5))
        p.fillRect(self.rect(), gradient)
        if self.illustrative:
            p.setPen(QColor(255, 245, 225, 160))
            p.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
            p.drawText(self.rect().adjusted(0, 12, -17, 0), Qt.AlignTop | Qt.AlignRight,
                       "ILUSTRAȚIE")
        p.setPen(QPen(QColor("#5B4D3C"), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()


class PosterCanvas(QLabel):
    """Crop loaded artwork to the card at every window size, with a useful empty state."""

    def __init__(self, width: int, height: int, skin: str, available: bool,
                 title: str = "", year: int | None = None, fallback_kind: str | None = None):
        super().__init__()
        kind = fallback_kind or ("editorial", "poster_wall", "workbench", "cinematic")[
            sum(ord(char) for char in title) % 4]
        self.artwork = _fallback_art(kind)
        self.illustrative = not self.artwork.isNull()
        self.skin = skin
        self.available = available
        self.title = title
        self.year = year
        self.setObjectName("FilmPoster")
        self.setFixedHeight(height)
        self.setMinimumWidth(width)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_artwork(self, pixmap: QPixmap):
        self.artwork = pixmap
        self.illustrative = False
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
            if self.illustrative:
                shade = QLinearGradient(0, self.height() * .42, 0, self.height())
                shade.setColorAt(0, QColor(0, 0, 0, 0))
                shade.setColorAt(1, QColor(0, 0, 0, 225))
                p.fillRect(self.rect(), shade)
                p.setPen(QColor("#FFF7EC"))
                p.setFont(QFont("Georgia", max(14, min(26, self.width() // 10)), QFont.DemiBold))
                p.drawText(QRectF(self.width() * .08, self.height() * .46,
                                  self.width() * .84, self.height() * .42),
                           Qt.TextWordWrap | Qt.AlignBottom, self.title)
                p.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
                p.setPen(QColor("#F4D9B4"))
                p.drawText(QRectF(self.width() * .08, self.height() * .91,
                                  self.width() * .84, self.height() * .07),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           "ILUSTRAȚIE" if self.width() < 210 else "ILUSTRAȚIE · AFIȘ INDISPONIBIL")
        else:
            w, h = self.width(), self.height()
            light = self.skin == "editorial"
            gradient = QLinearGradient(0, 0, w, h)
            covers = (
                (("#D9C4AA", "#A58778", "#503C41"),
                 ("#B9C2B1", "#8B9A92", "#485A5B"),
                 ("#DAC7BD", "#A08895", "#50495F")) if light else
                (("#33484C", "#654743", "#181B22"),
                 ("#3A465A", "#654C53", "#1C272F"),
                 ("#414C40", "#735A42", "#1C2729")) if self.skin == "workbench" else
                (("#7D5C4B", "#443E45", "#17191F"),
                 ("#455D62", "#444D57", "#161C25"),
                 ("#756475", "#4D4555", "#1B1A25"))
            )
            stops = covers[sum(ord(char) for char in self.title) % len(covers)]
            for at, color in zip((0, .53, 1), stops):
                gradient.setColorAt(at, QColor(color))
            p.fillRect(self.rect(), gradient)
            p.setPen(QPen(QColor(255, 241, 213, 53), 1))
            for i in range(4):
                radius = int(min(w, h) * (.38 + i * .2))
                p.drawEllipse(int(w * .78) - radius, int(h * .38) - radius,
                              radius * 2, radius * 2)
            p.setPen(QPen(QColor(255, 240, 218, 130), 1))
            p.drawLine(int(w * .09), int(h * .12), int(w * .91), int(h * .12))
            p.drawLine(int(w * .09), int(h * .86), int(w * .91), int(h * .86))
            ink = QColor("#FFF7EC" if not light else "#2F2225")
            p.setPen(ink)
            p.setFont(QFont("Segoe UI", max(8, min(11, w // 20)), QFont.DemiBold))
            p.drawText(QRectF(w * .09, h * .045, w * .82, h * .07),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       "C  /  FILM" if w < 150 else "CINECALENDAR  /  SELECȚIE")
            font = QFont("Georgia", max(13, min(38, w // 8)))
            font.setWeight(QFont.DemiBold)
            title_area = QRectF(w * .09, h * .21, w * .82, h * .55)
            for size in range(font.pointSize(), 10, -2):
                font.setPointSize(size)
                p.setFont(font)
                if p.boundingRect(title_area, Qt.TextWordWrap, self.title).height() <= title_area.height():
                    break
            p.drawText(title_area, Qt.TextWordWrap | Qt.AlignVCenter, self.title)
            p.setFont(QFont("Segoe UI", max(9, min(12, w // 20)), QFont.DemiBold))
            p.drawText(QRectF(w * .09, h * .88, w * .82, h * .08),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       str(self.year or "FILM") if w < 150 else
                       f"{self.year or 'FILM'}  ·  {'SE ÎNCARCĂ' if self.available else 'AFIȘ INDISPONIBIL'}")
        p.end()


class ScoreRing(QWidget):
    """Show the actual estimated rating as an arc, without presenting it as a probability."""

    def __init__(self, rating: float, skin: str, diameter: int = 74):
        super().__init__()
        self.rating = max(0.0, min(10.0, float(rating)))
        self.color = PALETTES[skin]["accent"]
        self.ink = PALETTES[skin]["text"]
        self.setFixedSize(diameter, diameter)
        self.setToolTip(f"Rating personal estimat: {self.rating:.1f} din 10")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        d = min(self.width(), self.height())
        radius = QRectF(6, 6, d - 12, d - 12)
        p.setPen(QPen(QColor(self.ink).darker(250), 6, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(radius, 0, 360 * 16)
        p.setPen(QPen(QColor(self.color), 6, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(radius, 90 * 16, -int(360 * 16 * self.rating / 10))
        font = QFont("Segoe UI", 12 if d < 60 else 16, QFont.Bold)
        p.setFont(font); p.setPen(QColor(self.ink))
        p.drawText(self.rect(), Qt.AlignCenter, f"{self.rating:.1f}")
        p.end()


def _skin_preview(skin: str) -> QFrame:
    """Miniature of each layout, using neutral labels rather than fictitious films."""
    c = PALETTES[skin]
    frame = QFrame()
    frame.setFixedHeight(146)
    frame.setStyleSheet(f"background:{c['bg']};border:1px solid {c['border']};border-radius:4px;")

    def block(width=0, fill=None):
        part = QFrame()
        part.setStyleSheet(f"background:{fill or c['card2']};border:0;border-radius:2px;")
        if width:
            part.setFixedWidth(width)
        return part

    def mini(text, size=9, accent=False):
        label = QLabel(text)
        label.setStyleSheet(f"background:transparent;border:0;color:{c['accent'] if accent else c['text']};"
                            f"font-size:{size}px;font-weight:700;")
        return label

    if skin == "cinematic":
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(5)
        nav = QHBoxLayout()
        nav.addWidget(mini("CINECALENDAR", 8, True))
        nav.addStretch(1)
        nav.addWidget(mini("ACASĂ    FILME    CALENDAR", 7))
        layout.addLayout(nav)
        hero = block(fill="#3F302D")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(10, 5, 8, 6)
        hl.addWidget(mini("ALEGEREA SERII", 7, True))
        hl.addWidget(mini("Filmul tău", 15))
        hl.addStretch(1)
        hl.addWidget(mini("▶  Alege     Detalii", 8))
        layout.addWidget(hero, 1)
        rail = QHBoxLayout(); rail.setSpacing(5)
        for _ in range(3):
            tile = block(fill=c['card2']); tile.setFixedHeight(24); rail.addWidget(tile, 1)
        layout.addLayout(rail)
    elif skin == "editorial":
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)
        nav = block(44, c['surface']); nl = QVBoxLayout(nav); nl.setContentsMargins(4, 6, 2, 3)
        nl.addWidget(mini("CINE", 7, True)); nl.addWidget(mini("Acasă", 7)); nl.addWidget(mini("Filme", 7)); nl.addStretch(1)
        layout.addWidget(nav)
        poster = block(65, "#B49A88"); pl = QVBoxLayout(poster); pl.setContentsMargins(5, 7, 5, 5)
        pl.addStretch(1); pl.addWidget(mini("FILM", 13)); pl.addWidget(mini("SELECȚIE", 6))
        layout.addWidget(poster)
        copy = QVBoxLayout(); copy.setSpacing(4)
        copy.addWidget(mini("RECOMANDAREA ZILEI", 7, True))
        copy.addWidget(mini("O poveste pentru azi", 12))
        copy.addWidget(mini("De ce ți se potrivește", 8))
        copy.addStretch(1)
        copy.addWidget(mini("CITEȘTE    →", 8, True))
        layout.addLayout(copy, 1)
        side = block(47, c['card2']); layout.addWidget(side)
    elif skin == "poster_wall":
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(7, 7, 7, 7); layout.setSpacing(5)
        nav = block(40, c['surface']); layout.addWidget(nav)
        center = QVBoxLayout(); center.setSpacing(4)
        center.addWidget(block(fill="#51402F"), 1)
        tiles = QGridLayout(); tiles.setSpacing(3)
        for i in range(6): tiles.addWidget(block(fill=("#384F53", "#5A4946", "#53605A")[i % 3]), i // 3, i % 3)
        center.addLayout(tiles, 2); layout.addLayout(center, 1)
        layout.addWidget(block(35, c['surface']))
    else:
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)
        nav = block(42, c['surface']); nl = QVBoxLayout(nav); nl.setContentsMargins(4, 6, 2, 3)
        nl.addWidget(mini("◉  HUB", 7, True)); nl.addWidget(mini("Filme", 7)); nl.addWidget(mini("Listă", 7)); nl.addStretch(1)
        layout.addWidget(nav)
        main = QVBoxLayout(); main.setSpacing(5)
        main.addWidget(mini("ALEGEREA SERII   /   DETALII", 8, True))
        hero = block(fill=c['card2']); hl = QHBoxLayout(hero); hl.setContentsMargins(5, 4, 5, 4)
        art = block(46, "#67514A"); hl.addWidget(art)
        hl.addWidget(mini("Film selectat\nScor personal\n▶  Alege", 9), 1)
        main.addWidget(hero, 1)
        tiles = QHBoxLayout(); tiles.setSpacing(4)
        for _ in range(3):
            tiles.addWidget(block(fill=c['card2']), 1)
        main.addLayout(tiles, 1)
        layout.addLayout(main, 1)
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


def _search_visible_recommendations(window, search):
    query = search.text().strip().casefold()
    if not query:
        window.show_page("recommendations")
        return
    today = getattr(window, "today_result", None)
    current = ([today[0], *today[1]] if today and today[0] else [])
    seen = set()
    for rec in [*current, *list(getattr(window, "browse_result", []) or [])]:
        if rec.movie.id in seen:
            continue
        seen.add(rec.movie.id)
        haystack = " ".join((rec.movie.title, str(rec.movie.year or ""),
                             " ".join(rec.movie.genres or []), rec.movie.overview or "")).casefold()
        if query in haystack:
            window.open_details(rec)
            return
    window.set_status("Niciun film din recomandările încărcate nu corespunde căutării.", False)


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
        tl.setContentsMargins(28, 5, 28, 3)
        tl.setSpacing(1)
        first = QHBoxLayout()
        first.addWidget(_label("◉ CineCalendar", "Brand"), 0)
        first.addSpacing(30)
        for key, name in window.NAV[:7]:
            button = _nav_button(key, name, window.skin,
                                 lambda _=False, k=key: window.show_page(k))
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            window.nav_buttons[key] = button
            first.addWidget(button, 1)
        tl.addLayout(first)
        row = QHBoxLayout()
        row.addSpacing(282)
        for key, name in window.NAV[7:]:
            button = _nav_button(key, name, window.skin,
                                 lambda _=False, k=key: window.show_page(k))
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            window.nav_buttons[key] = button
            row.addWidget(button, 1)
        tl.addLayout(row)
        for indicator in (window.status, window.progress, window.undo_feedback_button):
            indicator.setParent(top)
            indicator.setFixedHeight(0)
        window.undo_feedback_button.setFixedWidth(360)
        window.status.hide()
        window.progress.hide()
        window.undo_feedback_button.hide()
        outer.addWidget(top)
        outer.addWidget(window.stack, 1)
        return

    outer = QHBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    side = QFrame()
    side.setObjectName("Sidebar")
    side.setFixedWidth(272 if window.skin == "editorial" else 294 if window.skin == "poster_wall" else 258)
    sl = QVBoxLayout(side)
    sl.setContentsMargins(16 if window.skin == "editorial" else 20, 14, 16, 12)
    sl.setSpacing(4)
    if window.skin == "editorial":
        sl.addSpacing(38)
    else:
        sl.addWidget(_label("◉  CineCalendar", "Brand"))
        if window.skin == "poster_wall":
            sl.addWidget(_label("Filmele potrivite, la momentul potrivit.", "Muted"))
        sl.addSpacing(20)
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
    for key, name in window.NAV:
        if key in ("today", "profile", "calendar", "metadata_doctor", "updates"):
            group = {"today":"DESCOPERĂ", "profile":"CONTUL MEU",
                     "calendar":"PLANIFICĂ", "metadata_doctor":"INSTRUMENTE" if window.skin != "workbench" else "ÎNTREȚINERE",
                     "updates":"APLICAȚIE"}[key]
            nl.addWidget(_label(group, "NavGroup"))
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
    version = window.windowTitle().removeprefix("CineCalendar ").split(" —", 1)[0]
    sl.addWidget(_label("v" + version if version else "CineCalendar"))
    outer.addWidget(side)
    body = QWidget(); body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(0, 0, 0, 0); body_layout.setSpacing(0)
    utility = QFrame(); utility.setObjectName("UtilityBar")
    ul = QHBoxLayout(utility); ul.setContentsMargins(16, 8, 22, 8); ul.setSpacing(13)
    if window.skin == "editorial":
        ul.addWidget(_label("CineCalendar", "UtilityBrand"))
        ul.addSpacing(12)
    search = QLineEdit(); search.setPlaceholderText("Caută în recomandările încărcate…")
    search.setClearButtonEnabled(True)
    search.returnPressed.connect(lambda: _search_visible_recommendations(window, search))
    ul.addWidget(search, 4)
    ul.addStretch(1)
    sync = _label("●  Profil local", "SyncLabel")
    ul.addWidget(sync)
    ul.addWidget(window.status)
    ul.addWidget(window.progress)
    ul.addWidget(window.undo_feedback_button)
    body_layout.addWidget(utility)
    body_layout.addWidget(window.stack, 1)
    outer.addWidget(body, 1)


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
        QFrame#Sidebar {{ background:{'#4C171C' if editorial else c['surface']}; border-right:1px solid {c['border']}; }}
        QFrame#Topbar {{ border-bottom:1px solid {c['border']}; }}
        QFrame#UtilityBar {{ background:{c['surface']}; border-bottom:1px solid {c['border']}; }}
        QLabel#UtilityBrand {{ font-family:Georgia; font-size:29px; font-weight:750; color:{c['accent']}; }}
        QLabel#SyncLabel {{ color:{c['good']}; font-size:12px; }}
        QFrame#Sidebar QFrame#PremiumCard {{ background:{c['card2']}; }}
        QFrame#IconRail {{ border-right:1px solid {c['border']}; }}
        QFrame#PageMasthead {{ border:{'0' if editorial else '1px solid ' + c['border']};
            border-bottom:{'2px solid ' + c['accent'] if editorial else '1px solid ' + c['border']};
            border-radius:{'0' if editorial else radius};
            background:{'transparent' if editorial else 'qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #261C1C,stop:.55 #181619,stop:1 #121316)' if skin == 'cinematic' else '#1B2C32'}; }}
        QFrame#PageMasthead[compact='true'] {{ border:0; background:transparent; }}
        QFrame#PageMasthead QLabel#PageTitle {{ font-size:{'46' if editorial else '38'}px; }}
        QFrame#PageMasthead QLabel#Eyebrow {{ font-size:11px; }}
        QFrame#PageMasthead QFrame#PageEmblem {{ background:{c['card2']};
            border:1px solid {c['border']}; border-radius:8px; }}
        QFrame#PageMasthead QFrame#PageEmblem QLabel {{ color:{c['accent']}; }}
        QWidget#MastheadCopy {{ background:transparent; }}
        QFrame#TodayGenreChooser {{ background:transparent; border:0; }}
        QFrame#TodayGenreChooser QLabel {{ color:{c['muted']}; }}
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
        QLabel#EditorialTitle {{ font-family:Georgia; font-size:67px; font-weight:600; color:{c['text']}; }}
        QLabel#EditorialBody {{ font-family:Georgia; font-size:17px; color:{c['text']}; }}
        QLabel#EditorialMeta {{ font-family:Georgia; font-size:20px; color:{c['muted']}; }}
        QFrame#EditorialSuggestion {{ border-bottom:1px solid {c['border']}; }}
        QFrame#PosterTileFoot {{ background:{c['surface']}; }}
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
        QPushButton#TextLink {{ background:transparent; border:0; color:{c['muted']}; padding:3px 0; font-size:11px; }}
        QPushButton[nav='true'] {{ text-align:left; background:transparent; border:0; color:{c['muted']}; padding:10px 9px; font-size:{'13' if editorial else '14'}px; }}
        QPushButton[nav='true']:hover {{ background:{c['card2']}; color:{c['text']}; }}
        QPushButton[navActive='true'] {{ text-align:left; background:{c['card2']};
            color:{c['text']}; border-left:3px solid {c['accent']}; font-weight:750; padding:10px 12px; }}
        QFrame#Topbar QPushButton[nav='true'], QFrame#Topbar QPushButton[navActive='true'] {{
            text-align:center; font-size:13px; padding:9px 3px; border-radius:0; }}
        QFrame#Topbar QPushButton[navActive='true'] {{ border:0; border-bottom:3px solid {c['accent']}; background:transparent; color:{c['accent']}; }}
        QFrame#Sidebar QPushButton[nav='true'], QFrame#Sidebar QPushButton[navActive='true'] {{ padding:10px 12px; font-size:14px; color:{'#F9EEE8' if editorial else c['muted']}; }}
        QFrame#Sidebar QPushButton[navActive='true'] {{ background:{'#783037' if editorial else c['card2']}; color:{'#FFFFFF' if editorial else c['text']}; }}
        QFrame#Sidebar QLabel#NavGroup {{ letter-spacing:2px; margin-top:21px; margin-bottom:5px; color:{'#E7C9C5' if editorial else c['muted']}; }}
        QFrame#Sidebar QLabel#Muted {{ color:{'#E7C9C5' if editorial else c['muted']}; }}
        QFrame#Sidebar QLabel#Brand {{ color:{c['accent']}; }}
        QFrame#UtilityBar QLineEdit {{ background:{c['card2']}; border-radius:6px; padding:9px 14px; }}
        QFrame#PosterFeature, QFrame#StudioInspector, QFrame#StudioChoice, QFrame#WeekDock {{ background:{c['card']}; border:1px solid {c['border']}; border-radius:{radius}; }}
        QFrame#StudioChoice[selected='true'] {{ border:2px solid {c['accent']}; background:{c['card2']}; }}
        QFrame#StudioChoice QLabel#ScoreLarge {{ color:{c['accent']}; }}
        QFrame#PosterTile {{ background:{c['surface']}; border:1px solid {c['border']}; border-radius:9px; }}
        QFrame#PosterTile:hover {{ border-color:{c['accent']}; }}
        QFrame#PosterWallRail {{ background:{c['surface']}; border-left:1px solid {c['border']}; }}
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


def _poster(window, rec, width, height, artwork_url=None, fallback_kind=None):
    source = artwork_url or rec.movie.poster_url
    poster = PosterCanvas(width, height, normalized_skin(window.skin), bool(source),
                          rec.movie.title, rec.movie.year, fallback_kind)
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
        box = HeroCanvas(skin, rec.movie.title, rec.movie.year)
        box.setMinimumHeight(500)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(46, 38, 28, 38)
        layout.setSpacing(22)
        info = QVBoxLayout()
        info.setSpacing(13)
        info.addStretch(1)
        info.addWidget(_label("━  ALEGEREA ZILEI", "Eyebrow"))
        title = _label(rec.movie.title, "HeroTitle", True)
        title.setStyleSheet(f"font-family:Georgia;font-size:{49 if window.width() < 1500 else 68}px;color:#FFF9EE;")
        title.setMinimumWidth(0)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        info.addWidget(title)
        info.addWidget(_label("   ·   ".join(window.movie_chips(rec.movie, 4)), "Muted", True))
        reason = _label(rec.movie.overview[:170] if rec.movie.overview else window.human_reason(rec), "HeroDescription", True)
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
                         315 if skin == "editorial" and window.width() >= 1400 else 245 if skin == "editorial" else (390 if window.width() >= 1500 else 300),
                         570 if skin == "editorial" else 390, backdrop)
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
    visible_buttons = (choose, details, skip) if cinematic else (choose, details, feedback, skip)
    for index, button in enumerate(visible_buttons):
        if cinematic:
            actions.addWidget(button)
        else:
            actions.addWidget(button, index // 2, index % 2)
    info.addLayout(actions)
    if cinematic:
        feedback.setObjectName("TextLink")
        info.addWidget(feedback, 0, Qt.AlignLeft)
        info.addSpacing(13)
        reliability = window.reliability_gate.evaluate(rec)
        info.addWidget(_label(f"{reliability.label}  ·  Estimare personală {rec.score.predicted_rating:.1f}/10", "Muted", True))
        info.addStretch(1)
        backdrop = _cached_backdrop_url(window, rec)
        box.landscape = bool(backdrop) or not (backdrop or rec.movie.poster_url)
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
    poster = _poster(window, rec, 210 if visual else 96,
                     218 if variant == "cinematic" else 178 if visual else 142)
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


def _calendar_context(window, rec):
    box = QFrame()
    box.setObjectName("AlternativeCard")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(17, 15, 17, 15)
    layout.setSpacing(12)
    today = date.today()
    layout.addWidget(_label("CONTEXT ÎN CALENDAR", "Eyebrow"))
    layout.addWidget(_label(today.strftime("%d.%m.%Y"), "CardTitle"))
    week = QHBoxLayout()
    week.setSpacing(2)
    monday = today - timedelta(days=today.weekday())
    for offset, name in enumerate(("Lu", "Ma", "Mi", "Jo", "Vi", "Sâ", "Du")):
        item = QVBoxLayout()
        day = _label(name, "Muted")
        day.setAlignment(Qt.AlignCenter)
        number = _label(str((monday + timedelta(days=offset)).day),
                        "Kicker" if offset == today.weekday() else "Muted")
        number.setAlignment(Qt.AlignCenter)
        item.addWidget(day); item.addWidget(number)
        week.addLayout(item, 1)
    layout.addLayout(week)
    layout.addWidget(_label(rec.score.calendar_reason or
                            "Consultă programul și contextul zilei pentru alegerea ta.",
                            "Muted", True))
    layout.addStretch(1)
    layout.addWidget(_action(window, "Deschide calendarul  →",
                             lambda: window.show_page("calendar")))
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
    selected = getattr(window, "today_result", None)
    if selected and selected[0]:
        feedback = QPushButton("Nu acum / motiv")
        feedback.clicked.connect(lambda _=False, mid=selected[0].movie.id, button=feedback:
                                 window.contextual_feedback_menu(mid, button))
        filters.addWidget(feedback)
    layout.addLayout(filters)
    return bar


def _set_filter(window, name, value):
    window.db.set_setting(name, str(value or ""))
    window.show_page("today")


def _ro_short_date(day):
    months = ("ian.", "feb.", "mar.", "apr.", "mai", "iun.", "iul.",
              "aug.", "sept.", "oct.", "nov.", "dec.")
    return f"{day.day} {months[day.month - 1]}"


def _week_strip(window, *, dock=False):
    box = QFrame(); box.setObjectName("WeekDock" if dock else "PremiumCard")
    layout = QHBoxLayout(box) if dock and window.width() >= 1500 else QVBoxLayout(box)
    layout.setContentsMargins(16, 12, 16, 12); layout.setSpacing(8)
    today = date.today(); monday = today - timedelta(days=today.weekday())
    if dock:
        lead = QVBoxLayout()
        lead.addWidget(_label("Săptămâna aceasta", "CardTitle"))
        lead.addWidget(_label(f"{_ro_short_date(monday)} – "
                              f"{_ro_short_date(monday + timedelta(days=6))} {today.year}", "Muted"))
        layout.addLayout(lead)
    else:
        layout.addWidget(_label("Săptămâna la film", "CardTitle"))
    days = QHBoxLayout(); days.setSpacing(5)
    for i, name in enumerate(("Lu", "Ma", "Mi", "Jo", "Vi", "Sâ", "Du")):
        current = monday + timedelta(days=i)
        b = _action(window, f"{name}\n{current.day}", lambda _=False: window.show_page("calendar"),
                    current == today)
        b.setMinimumHeight(52 if dock else 48)
        days.addWidget(b, 1)
    layout.addLayout(days, 1)
    layout.addWidget(_action(window, "Deschide calendarul  →", lambda: window.show_page("calendar")))
    return box


def _poster_tile(window, rec, *, height=260, featured=False):
    box = QFrame(); box.setObjectName("PosterTile")
    layout = QVBoxLayout(box); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
    image = _poster(window, rec, 155, height)
    image.setMinimumWidth(1)
    layout.addWidget(image)
    foot = QFrame(); foot.setObjectName("PosterTileFoot")
    fl = QVBoxLayout(foot); fl.setContentsMargins(12, 8, 12, 8); fl.setSpacing(3)
    fl.addWidget(_label(rec.movie.title, "CardTitle", True))
    fl.addWidget(_label(" · ".join(window.movie_chips(rec.movie, 3)), "Muted", True))
    row = QHBoxLayout(); row.addWidget(ScoreRing(rec.score.predicted_rating, window.skin, 42))
    row.addStretch(1)
    row.addWidget(_action(window, "Detalii", lambda _=False, r=rec: window.open_details(r)))
    row.addWidget(_action(window, "Aleg", lambda _=False, mid=rec.movie.id: window.choose_decision(mid)))
    fl.addLayout(row)
    layout.addWidget(foot)
    if featured:
        box.setProperty("featured", True)
    return box


def _discovery_tile(window, title, subtitle, destination, art):
    box = QFrame(); box.setObjectName("PosterTile")
    layout = QVBoxLayout(box); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
    cover = PosterCanvas(155, 195, window.skin, False, title, fallback_kind=art)
    cover.setMinimumWidth(1); layout.addWidget(cover)
    foot = QFrame(); foot.setObjectName("PosterTileFoot")
    details = QVBoxLayout(foot); details.setContentsMargins(12, 8, 12, 8)
    details.addWidget(_label(subtitle, "Muted", True))
    details.addWidget(_action(window, "Explorează  →", lambda: window.show_page(destination)))
    layout.addWidget(foot)
    return box


def _poster_feature(window, rec):
    box = HeroCanvas("poster_wall", rec.movie.title, rec.movie.year)
    box.setObjectName("PosterFeature")
    box.setMinimumHeight(214); box.setMaximumHeight(245)
    row = QHBoxLayout(box); row.setContentsMargins(30, 18, 28, 18)
    copy = QVBoxLayout(); copy.setSpacing(4)
    copy.addWidget(_label("★  ALEGEREA SERII", "Eyebrow"))
    copy.addWidget(_label(rec.movie.title, "HeroTitle", True))
    copy.addWidget(_label(" · ".join(window.movie_chips(rec.movie, 3)), "Muted", True))
    copy.addWidget(_label(window.human_reason(rec), "HeroDescription", True))
    actions = QHBoxLayout()
    actions.addWidget(_action(window, "▶  Aleg pentru azi", lambda _=False, mid=rec.movie.id: window.choose_decision(mid), True))
    actions.addWidget(_action(window, "De ce acesta?", lambda _=False, r=rec: window.open_details(r)))
    actions.addWidget(_action(window, "Alt film", lambda _=False, mid=rec.movie.id: window.skip_decision(mid)))
    feedback = QPushButton("Nu acum / motiv")
    feedback.clicked.connect(lambda _=False, mid=rec.movie.id, button=feedback:
                             window.contextual_feedback_menu(mid, button))
    actions.addWidget(feedback)
    copy.addLayout(actions); row.addLayout(copy, 3); row.addStretch(2)
    backdrop = _cached_backdrop_url(window, rec)
    box.landscape = bool(backdrop) or not (backdrop or rec.movie.poster_url)
    source = backdrop or rec.movie.poster_url
    if source:
        window.load_poster_async(box, source, (rec.movie.imdb_id or str(rec.movie.id)) + (":backdrop" if backdrop else ""))
    return box


def _poster_wall(window, primary, backups):
    content = window.today_content
    content.addWidget(_poster_feature(window, primary))
    narrow = window.width() < 1450
    body = QVBoxLayout() if narrow else QHBoxLayout()
    body.setSpacing(16)
    center = QVBoxLayout(); center.setSpacing(10)
    title = QVBoxLayout() if narrow else QHBoxLayout()
    title.addWidget(_label("Descoperă pentru tine", "PageTitle"))
    if not narrow:
        title.addStretch(1)
    title.addWidget(_action(window, "Toate recomandările  →", lambda: window.show_page("recommendations")))
    center.addLayout(title)
    filters = QVBoxLayout() if narrow else QHBoxLayout()
    mode_row = QHBoxLayout() if narrow else filters
    for caption, value in (("Toate", "decide"), ("Mai sigur", "safe"),
                           ("Surprinde-mă", "surprise"), ("Sub 2 ore", "short")):
        mode_row.addWidget(_action(window, caption,
                lambda _=False, mode=value: window.set_decision_mode(mode),
                value == window.decision_mode))
    mode_row.addStretch(1)
    if narrow:
        filters.addLayout(mode_row)
    center.addLayout(filters)
    window._poster_filter_row = filters
    extras = [r for r in list(getattr(window, "browse_result", []) or [])
              if r.movie.id not in {primary.movie.id, *(r.movie.id for r in backups)}]
    items = [primary, *backups, *extras][:6]
    grid = QGridLayout(); grid.setSpacing(12)
    dense_fonts = window.fontMetrics().horizontalAdvance("Surprinde-mă") > 120
    columns = 1 if narrow and dense_fonts else 2 if narrow else 3
    for i, rec in enumerate(items):
        grid.addWidget(_poster_tile(window, rec, height=195, featured=i == 0), i // columns, i % columns)
    destinations = (("Mai multe filme", "Toate recomandările din catalog", "recommendations", "cinematic"),
                    ("Cinema românesc", "Selecția de filme românești", "romanian", "editorial"),
                    ("Lista mea", "Filmele salvate de tine", "watchlist", "workbench"))
    for i in range(len(items), 6):
        title, subtitle, destination, art = destinations[(i - len(items)) % len(destinations)]
        grid.addWidget(_discovery_tile(window, title, subtitle, destination, art), i // columns, i % columns)
    center.addLayout(grid); center.addStretch(1)
    center_wrap = QWidget(); center_wrap.setLayout(center)
    body.addWidget(center_wrap, 7 if not narrow else 0)
    rail = QFrame(); rail.setObjectName("PosterWallRail")
    rl = QVBoxLayout(rail); rl.setContentsMargins(16, 8, 16, 16); rl.setSpacing(16)
    rl.addWidget(_week_strip(window))
    rl.addWidget(_calendar_context(window, primary))
    rl.addWidget(_label("Recomandări românești", "CardTitle"))
    rl.addWidget(_label("Explorează filmele românești verificate din catalog.", "Muted", True))
    rl.addWidget(_action(window, "Vezi recomandările românești  →", lambda: window.show_page("romanian")))
    rl.addStretch(1)
    body.addWidget(rail, 3 if not narrow else 0)
    wrap = QWidget(); wrap.setLayout(body); content.addWidget(wrap)


def _studio_choice(window, rec, index, stack, buttons):
    box = QFrame(); box.setObjectName("StudioChoice")
    compact = window.width() < 1500
    outer = QVBoxLayout(box) if compact else QHBoxLayout(box)
    outer.setContentsMargins(12, 10, 14, 10); outer.setSpacing(8 if compact else 12)
    row = QHBoxLayout() if compact else outer
    if compact:
        row.setSpacing(10)
    radio = _action(window, "◉" if index == 0 else "○",
                    lambda _=False, i=index: _select_studio(stack, buttons, i))
    radio.setFixedWidth(28 if compact else 34); row.addWidget(radio)
    art = _poster(window, rec, 120 if compact else 165,
                  100 if compact else 120, _cached_backdrop_url(window, rec))
    art.setFixedWidth(120 if compact else 165); row.addWidget(art)
    details = QVBoxLayout(); details.setSpacing(4)
    name = _label(rec.movie.title, "CardTitle", True)
    name.setMinimumWidth(0)
    name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    details.addWidget(name)
    details.addWidget(_label(" · ".join(window.movie_chips(rec.movie, 3)), "Muted", True))
    if not compact:
        details.addWidget(_label(window.human_reason(rec)[:110], "Muted", True))
    details.addStretch(1); row.addLayout(details, 4)
    row.addWidget(ScoreRing(rec.score.predicted_rating, window.skin, 58 if compact else 72))
    select = _action(window, "Selectează", lambda _=False, i=index: _select_studio(stack, buttons, i))
    if compact:
        outer.addLayout(row)
        bottom = QHBoxLayout()
        reason = _label(window.human_reason(rec)[:130], "Muted", True)
        reason.setMinimumWidth(0)
        reason.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bottom.addWidget(reason, 1)
        bottom.addWidget(select)
        outer.addLayout(bottom)
    else:
        row.addWidget(select)
    buttons.append((box, radio, select))
    return box


def _select_studio(stack, buttons, index):
    stack.setCurrentIndex(index)
    for i, (box, radio, select) in enumerate(buttons):
        box.setProperty("selected", i == index)
        radio.setText("◉" if i == index else "○")
        select.setProperty("accent", i == index)
        for widget in (box, select):
            widget.style().unpolish(widget); widget.style().polish(widget)


def _studio_inspector(window, rec):
    box = QFrame(); box.setObjectName("StudioInspector")
    layout = QVBoxLayout(box); layout.setContentsMargins(16, 16, 16, 16); layout.setSpacing(8)
    art = _poster(window, rec, 260, 205, _cached_backdrop_url(window, rec))
    art.setMinimumWidth(1); layout.addWidget(art)
    headline = QHBoxLayout()
    headline.addWidget(_label(rec.movie.title, "HeroTitle", True), 1)
    headline.addWidget(ScoreRing(rec.score.predicted_rating, window.skin, 82))
    layout.addLayout(headline)
    layout.addWidget(_label(" · ".join(window.movie_chips(rec.movie, 4)), "Muted", True))
    layout.addWidget(_label("De ce ți-l recomandăm?", "CardTitle"))
    layout.addWidget(_label(window.human_reason(rec), "Muted", True))
    evidence = QHBoxLayout(); evidence.setSpacing(7)
    for title, value in (("ESTIMARE", f"{rec.score.predicted_rating:.1f}/10"),
                         ("ÎNCREDERE", f"{rec.score.confidence:.0%}"),
                         ("CONTEXT", rec.score.calendar_reason or "Fără ajustare")):
        signal = QFrame(); signal.setObjectName("PremiumCard")
        column = QVBoxLayout(signal); column.setContentsMargins(9, 7, 9, 7)
        column.addWidget(_label(title, "Eyebrow"))
        column.addWidget(_label(value, "Muted", True))
        evidence.addWidget(signal, 1)
    layout.addLayout(evidence)
    if rec.movie.overview:
        layout.addWidget(_label("Sinopsis", "CardTitle"))
        layout.addWidget(_label(rec.movie.overview[:360], "Muted", True))
    else:
        layout.addWidget(_label("Sinopsisul va apărea după completarea metadatelor.", "Muted", True))
    layout.addStretch(1)
    row = QHBoxLayout()
    row.addWidget(_action(window, "▶  Aleg pentru azi", lambda _=False, mid=rec.movie.id: window.choose_decision(mid), True))
    row.addWidget(_action(window, "Detalii", lambda _=False, r=rec: window.open_details(r)))
    layout.addLayout(row)
    row2 = QHBoxLayout()
    row2.addWidget(_action(window, "Alt film", lambda _=False, mid=rec.movie.id: window.skip_decision(mid)))
    feedback = QPushButton("Nu acum / motiv")
    feedback.clicked.connect(lambda _=False, mid=rec.movie.id, button=feedback: window.contextual_feedback_menu(mid, button))
    row2.addWidget(feedback); layout.addLayout(row2)
    return box


def _studio_comparison(window, recs):
    dialog = QDialog(window)
    dialog.setWindowTitle("Comparație detaliată")
    dialog.resize(1050, 480)
    layout = QVBoxLayout(dialog); layout.setContentsMargins(22, 18, 22, 18)
    layout.addWidget(_label("Compară recomandările de azi", "PageTitle"))
    table = QTableWidget(len(recs), 6)
    table.setHorizontalHeaderLabels(("Film", "An", "Genuri", "Rating estimat",
                                      "Încredere", "Motiv"))
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    for row, rec in enumerate(recs):
        values = (rec.movie.title, str(rec.movie.year or "—"),
                  ", ".join(rec.movie.genres or []) or "—",
                  f"{rec.score.predicted_rating:.1f}/10", f"{rec.score.confidence:.0%}",
                  window.human_reason(rec))
        for col, value in enumerate(values):
            table.setItem(row, col, QTableWidgetItem(value))
    table.horizontalHeader().setStretchLastSection(True)
    table.resizeColumnsToContents()
    table.setWordWrap(True)
    if recs:
        table.selectRow(0)
    layout.addWidget(table, 1)
    action = _action(window, "Vezi detaliile filmului selectat",
                     lambda: window.open_details(recs[max(0, table.currentRow())]))
    action.setEnabled(bool(recs))
    layout.addWidget(action)
    dialog.exec()


def _studio_quick_choice(window, rec):
    dialog = QDialog(window)
    dialog.setWindowTitle("Decizie rapidă")
    layout = QVBoxLayout(dialog); layout.setContentsMargins(24, 22, 24, 22); layout.setSpacing(12)
    layout.addWidget(_label("Alegerea recomandată pentru azi", "Eyebrow"))
    layout.addWidget(_label(rec.movie.title, "HeroTitle", True))
    layout.addWidget(_label(f"Rating personal estimat {rec.score.predicted_rating:.1f}/10", "Muted"))
    layout.addWidget(_label(window.human_reason(rec), "Muted", True))
    row = QHBoxLayout()
    choose = _action(window, "Aleg pentru azi", lambda: (dialog.accept(), window.choose_decision(rec.movie.id)), True)
    row.addWidget(choose)
    row.addWidget(_action(window, "Alt film", lambda: (dialog.accept(), window.skip_decision(rec.movie.id))))
    layout.addLayout(row)
    dialog.exec()


def _decision_studio(window, primary, backups):
    content = window.today_content
    compact = window.width() < 1500
    heading = QVBoxLayout() if compact else QHBoxLayout()
    heading.addWidget(_label("Alegerea serii", "PageTitle"))
    if not compact:
        heading.addStretch(1)
    tabs = QHBoxLayout() if compact else heading
    tabs.addWidget(_action(window, "✦  Recomandări pentru azi", lambda: window.show_page("today"), True))
    tabs.addWidget(_action(window, "☷  Comparație detaliată",
                           lambda: _studio_comparison(window, [primary, *backups])))
    tabs.addWidget(_action(window, "ϟ  Decizie rapidă", lambda: _studio_quick_choice(window, primary)))
    if compact:
        heading.addLayout(tabs)
    head = QWidget(); head.setLayout(heading); content.addWidget(head)
    row = QVBoxLayout() if window.width() < 1500 else QHBoxLayout()
    row.setSpacing(14)
    choices = QVBoxLayout(); choices.setSpacing(8)
    inspector = QStackedWidget(); buttons = []
    for i, rec in enumerate([primary, *backups]):
        inspector.addWidget(_studio_inspector(window, rec))
        choices.addWidget(_studio_choice(window, rec, i, inspector, buttons))
    more = QFrame(); more.setObjectName("StudioChoice")
    mr = QVBoxLayout(more) if window.width() < 1500 else QHBoxLayout(more)
    mr.setContentsMargins(16, 10, 16, 10)
    illustration = PosterCanvas(165, 115, window.skin, False, "Descoperă", fallback_kind="workbench")
    illustration.setFixedWidth(165)
    if window.width() >= 1500:
        mr.addWidget(illustration)
    summary = QVBoxLayout()
    summary.addWidget(_label("Vrei să compari mai multe?", "CardTitle", True))
    summary.addWidget(_label("Deschide selecția completă de filme potrivite.", "Muted", True))
    summary.addStretch(1)
    mr.addLayout(summary, 1)
    mr.addWidget(_action(window, "Vezi toate  →", lambda: window.show_page("recommendations")))
    choices.addWidget(more)
    choices.addStretch(1)
    left = QWidget(); left.setLayout(choices)
    row.addWidget(left, 6 if window.width() >= 1500 else 0)
    row.addWidget(inspector, 4 if window.width() >= 1500 else 0)
    wrap = QWidget(); wrap.setLayout(row); content.addWidget(wrap)
    _select_studio(inspector, buttons, 0)
    content.addWidget(_week_strip(window, dock=True))


def _editorial_spread(window, primary, backups):
    content = window.today_content
    narrow = window.width() < 1400
    columns = QVBoxLayout() if narrow else QHBoxLayout()
    columns.setSpacing(24)
    top = QVBoxLayout() if narrow else columns
    top.setSpacing(24)
    poster = _poster(window, primary, 300 if narrow else 390, 550 if narrow else 870,
                     fallback_kind="editorial")
    poster.setMinimumWidth(1 if narrow else 300)
    top.addWidget(poster, 39)
    story = QVBoxLayout(); story.setSpacing(13)
    story.addWidget(_label("CÂND TRECUTUL REVINE,\nNICI DRUMUL NU MAI E ACELAȘI.", "Eyebrow", True))
    story.addSpacing(17)
    story_title = _label(primary.movie.title, "EditorialTitle", True)
    if narrow:
        story_title.setStyleSheet("font-family:Georgia;font-size:48px;")
    story_title.setMinimumWidth(0)
    story_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    story.addWidget(story_title)
    story.addWidget(_label(" · ".join(window.movie_chips(primary.movie, 4)), "EditorialMeta", True))
    if primary.movie.overview:
        story.addWidget(_label(primary.movie.overview[:560], "EditorialBody", True))
    story.addSpacing(8)
    story.addWidget(_label("De ce ți-l recomandăm?", "SectionTitle"))
    story.addWidget(_label(window.human_reason(primary), "EditorialBody", True))
    if primary.score.calendar_reason:
        story.addWidget(_label(primary.score.calendar_reason, "Muted", True))
    story.addStretch(1)
    story.addWidget(_action(window, "▶  Aleg pentru azi", lambda _=False, mid=primary.movie.id: window.choose_decision(mid), True))
    controls = QHBoxLayout()
    controls.addWidget(_action(window, "Alt film", lambda _=False, mid=primary.movie.id: window.skip_decision(mid)))
    controls.addWidget(_action(window, "Detalii", lambda _=False, r=primary: window.open_details(r)))
    feedback = QPushButton("Nu acum / motiv")
    feedback.clicked.connect(lambda _=False, mid=primary.movie.id, b=feedback: window.contextual_feedback_menu(mid, b))
    controls.addWidget(feedback)
    story.addLayout(controls)
    middle = QWidget(); middle.setLayout(story); top.addWidget(middle, 31)
    side = QVBoxLayout(); side.setSpacing(14)
    side.addWidget(_label("ALTE SUGESTII PENTRU AZI", "Eyebrow"))
    for rec in backups[:2]:
        card = QFrame(); card.setObjectName("EditorialSuggestion")
        row = QHBoxLayout(card); row.setContentsMargins(0, 0, 0, 10); row.setSpacing(12)
        art = _poster(window, rec, 120, 212)
        art.setFixedWidth(120); row.addWidget(art)
        description = QVBoxLayout()
        description.addWidget(_label(rec.movie.title, "CardTitle", True))
        description.addWidget(_label(" · ".join(window.movie_chips(rec.movie, 3)), "Muted", True))
        description.addWidget(_label(rec.movie.overview[:145] if rec.movie.overview else window.human_reason(rec)[:145], "EditorialBody", True))
        description.addStretch(1)
        description.addWidget(_action(window, "Vezi detalii  ›", lambda _=False, r=rec: window.open_details(r)))
        row.addLayout(description, 1)
        side.addWidget(card)
    context = _calendar_context(window, primary)
    side.addWidget(context)
    side.addStretch(1)
    right = QWidget(); right.setLayout(side)
    if narrow:
        columns.addLayout(top)
        columns.addWidget(right)
    else:
        columns.addWidget(right, 30)
    wrap = QWidget(); wrap.setLayout(columns); content.addWidget(wrap)


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
        heading = QVBoxLayout() if window.width() < 1500 else QHBoxLayout()
        heading.addWidget(_label("Și alte recomandări pentru această săptămână", "SectionTitle"))
        if window.width() >= 1500:
            heading.addStretch(1)
        monday = date.today() - timedelta(days=date.today().weekday())
        date_controls = QHBoxLayout() if window.width() < 1500 else heading
        date_controls.addWidget(_label(f"{_ro_short_date(monday)} – "
                                       f"{_ro_short_date(monday + timedelta(days=6))} {date.today().year}", "Muted"))
        date_controls.addWidget(_action(window, "‹", lambda: window.show_page("calendar")))
        date_controls.addWidget(_action(window, "›", lambda: window.show_page("calendar")))
        if window.width() < 1500:
            heading.addLayout(date_controls)
        header = QWidget(); header.setLayout(heading); content.addWidget(header)
        row = QGridLayout() if window.width() < 1500 else QHBoxLayout()
        row.setSpacing(12)
        extras = [r for r in list(getattr(window, "browse_result", []) or [])
                  if r.movie.id not in {primary.movie.id, *(r.movie.id for r in backups)}]
        for i, rec in enumerate([primary, *backups, *extras][:4]):
            tile = _poster_tile(window, rec, height=255, featured=i == 0)
            if window.width() < 1500:
                row.addWidget(tile, i // 2, i % 2)
            else:
                row.addWidget(tile, 1)
        if not extras:
            more = QFrame(); more.setObjectName("PosterTile")
            ml = QVBoxLayout(more); ml.setContentsMargins(20, 20, 20, 20)
            ml.addWidget(_label("Continuă descoperirea", "CardTitle", True)); ml.addStretch(1)
            ml.addWidget(_label("Selecția completă îți arată mai multe filme din catalog.", "Muted", True))
            ml.addWidget(_action(window, "Vezi toate  →", lambda: window.show_page("recommendations")))
            if window.width() < 1500:
                row.addWidget(more, 1, 1)
            else:
                row.addWidget(more, 1)
        wrap = QWidget(); wrap.setLayout(row); content.addWidget(wrap)
    elif skin == "editorial":
        _editorial_spread(window, primary, backups)
    elif skin == "poster_wall":
        _poster_wall(window, primary, backups)
    else:
        _decision_studio(window, primary, backups)
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
        cards = QGridLayout()
        cards.setSpacing(12)
        for index, (key, (name, description)) in enumerate(SKINS.items()):
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
            cards.addWidget(item, index // 2, index % 2)
        layout.addLayout(cards)
        content.insertWidget(0, panel)
        return page

    def page_shell(self, title, subtitle="", actions=None):
        skin = normalized_skin(getattr(self, "skin", None))
        key = getattr(self, "current_page", "today")
        compact = key == "today" and skin in ("cinematic", "editorial", "poster_wall", "workbench")
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0 if compact else 24 if skin == "cinematic" else 26,
                                 0 if compact else 24,
                                 0 if compact else 24 if skin == "cinematic" else 26,
                                 0 if compact else 18)
        outer.setSpacing(0 if compact else 18)
        masthead = QFrame()
        masthead.setObjectName("PageMasthead")
        masthead.setProperty("compact", compact)
        row = QHBoxLayout(masthead)
        row.setContentsMargins(0 if compact or skin == "editorial" else 26,
                               5 if compact else 12 if skin == "editorial" else 22,
                               0 if compact or skin == "editorial" else 26,
                               5 if compact else 18 if skin == "editorial" else 24)
        row.setSpacing(20)
        if not compact and skin != "editorial":
            emblem = QFrame()
            emblem.setObjectName("PageEmblem")
            emblem.setFixedSize(62, 62)
            el = QVBoxLayout(emblem)
            el.setContentsMargins(0, 0, 0, 0)
            glyph = _label("", "RailGlyph")
            glyph.setPixmap(_icon(key, PALETTES[skin]["accent"], 31).pixmap(31, 31))
            glyph.setAlignment(Qt.AlignCenter)
            el.addWidget(glyph)
            row.addWidget(emblem, 0, Qt.AlignLeft | Qt.AlignTop)
        copy_holder = QWidget()
        copy_holder.setObjectName("MastheadCopy")
        copy = QVBoxLayout(copy_holder)
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(6)
        section = ("SELECȚIA TA" if key in ("today", "recommendations", "romanian") else
                   "CINEMATECĂ" if key == "romanian_list" else
                   "CALENDAR & CONTEXT" if key in ("calendar", "month") else
                   "BIBLIOTECA TA" if key in ("ratings", "watchlist", "history", "profile") else
                   "INSTRUMENTE")
        copy.addWidget(_label(section, "Eyebrow"))
        heading = _label(title, "PageTitle", True)
        if compact:
            heading.setStyleSheet("font-size:19px;font-weight:700;")
        copy.addWidget(heading)
        if subtitle and not compact:
            description = _label(subtitle, "Muted", True)
            description.setMaximumWidth(660)
            copy.addWidget(description)
        copy_holder.setFixedWidth(235 if compact else
                                  min(660, max(350, (self.width() -
                                      (285 if skin != "cinematic" else 0)) // 2)))
        row.addWidget(copy_holder, 0, Qt.AlignLeft | Qt.AlignVCenter)
        row.addStretch(1)
        if actions:
            commands = QGridLayout()
            commands.setHorizontalSpacing(8)
            commands.setVerticalSpacing(8)
            for i, (label, handler, accent) in enumerate(actions):
                button = _action(self, label, handler, accent)
                commands.addWidget(button, i // 2, i % 2)
            row.addLayout(commands)
        masthead._utility_row = row
        outer.addWidget(masthead)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        content = QVBoxLayout(inner)
        content.setContentsMargins(0 if skin == "cinematic" and compact else 14 if compact else 0,
                                   0 if compact else 2,
                                   0 if skin == "cinematic" and compact else 14 if compact else 6,
                                   10)
        content.setSpacing(12 if compact else 15)
        content.setAlignment(Qt.AlignTop)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return page, content

    def resizeEvent(self, event):
        old_width = event.oldSize().width()
        original_resize_event(self, event)
        breakpoint = (1400 if getattr(self, "skin", None) == "editorial" else
                      1450 if getattr(self, "skin", None) == "poster_wall" else
                      1500 if getattr(self, "skin", None) == "workbench" else None)
        if (breakpoint is not None and old_width > 0
                and (old_width < breakpoint) != (event.size().width() < breakpoint)
                and getattr(self, "current_page", None) == "today"
                and getattr(self, "today_result", None) is not None):
            QTimer.singleShot(0, lambda: self.show_page("today")
                              if not getattr(self, "_ui_closing", False)
                              and self.current_page == "today" else None)

    def show_page(self, key):
        utility = self.centralWidget().findChild(QFrame, "UtilityBar")
        if utility is not None:
            previous = utility.findChild(QFrame, "TodayGenreChooser")
            if previous is not None:
                utility.layout().removeWidget(previous)
                previous.hide()
                previous.deleteLater()
        self._poster_filter_row = None
        original_show_page(self, key)
        page = self.stack.currentWidget()
        if page is None or not self.isVisible():
            return
        if key == "today" and normalized_skin(self.skin) == "cinematic":
            outer = page.layout()
            if outer.count() >= 3:
                chooser = outer.itemAt(1).widget()
                masthead = outer.itemAt(0).widget()
                if (chooser is not None and chooser.objectName() == "PremiumCard"
                        and masthead is not None and masthead.objectName() == "PageMasthead"):
                    outer.removeWidget(chooser)
                    chooser.setObjectName("TodayGenreChooser")
                    chooser.setMaximumWidth(285)
                    for label in chooser.findChildren(QLabel):
                        label.hide()
                    hero = next(iter(page.findChildren(HeroCanvas)), None)
                    if hero is not None:
                        hero.layout().addWidget(chooser, 0, Qt.AlignRight | Qt.AlignTop)
                        masthead.hide()
                    else:
                        masthead._utility_row.insertWidget(1, chooser, 1)
                    chooser.style().unpolish(chooser)
                    chooser.style().polish(chooser)
        elif key == "today":
            outer = page.layout()
            if outer.count() >= 3:
                chooser = outer.itemAt(1).widget()
                masthead = outer.itemAt(0).widget()
                if chooser is not None and chooser.objectName() == "PremiumCard":
                    outer.removeWidget(chooser)
                    chooser.setObjectName("TodayGenreChooser")
                    chooser.setMaximumWidth(275)
                    for label in chooser.findChildren(QLabel):
                        label.hide()
                    if normalized_skin(self.skin) == "poster_wall" and self._poster_filter_row is not None:
                        self._poster_filter_row.addWidget(chooser)
                    else:
                        utility = self.centralWidget().findChild(QFrame, "UtilityBar")
                        if utility is not None:
                            utility.layout().insertWidget(2, chooser)
                    if masthead is not None:
                        masthead.hide()
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
