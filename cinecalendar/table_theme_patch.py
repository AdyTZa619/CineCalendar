from __future__ import annotations

from PySide6.QtWidgets import QApplication


def table_qss(dark: bool) -> str:
    """Explicitly theme Qt item views instead of inheriting the native white palette.

    Premium's global QWidget rule sets text to a light color, while QTableWidget's native
    alternateBase can remain white on Windows. With alternating rows enabled that makes
    every second row look blank (white text on white background). Headers can suffer the
    same palette mismatch. Keep all table colors explicit in both themes.
    """
    if dark:
        bg = "#151820"
        alt = "#1B1F29"
        header = "#0F1116"
        text = "#F7F7F4"
        border = "#282E3A"
        selection = "#315FD6"
        selection_text = "#FFFFFF"
    else:
        bg = "#FFFFFF"
        alt = "#F0EEE9"
        header = "#FAF9F6"
        text = "#17181C"
        border = "#DFDCD4"
        selection = "#315FD6"
        selection_text = "#FFFFFF"

    return f"""
        QTableView, QTableWidget {{
            background-color:{bg};
            alternate-background-color:{alt};
            color:{text};
            gridline-color:{border};
            border:1px solid {border};
            border-radius:10px;
            selection-background-color:{selection};
            selection-color:{selection_text};
        }}
        QTableView::item, QTableWidget::item {{
            color:{text};
            padding:5px;
        }}
        QTableView::item:selected, QTableWidget::item:selected {{
            background:{selection};
            color:{selection_text};
        }}
        QHeaderView::section {{
            background:{header};
            color:{text};
            border:0;
            border-right:1px solid {border};
            border-bottom:1px solid {border};
            padding:8px;
            font-weight:700;
        }}
        QTableCornerButton::section {{
            background:{header};
            border:0;
            border-right:1px solid {border};
            border-bottom:1px solid {border};
        }}
    """


def install_table_theme_patch(window_cls) -> None:
    """Append explicit table palette rules every time the Premium theme is applied."""
    if getattr(window_cls, "_cinecalendar_table_theme_patch", False):
        return

    original = window_cls.apply_theme

    def apply_theme(self):
        original(self)
        app = QApplication.instance()
        if app is not None:
            base = app.styleSheet() or ""
            app.setStyleSheet(base + "\n" + table_qss(self.theme != "light"))

    window_cls.apply_theme = apply_theme
    window_cls._cinecalendar_table_theme_patch = True
