from __future__ import annotations

from PySide6.QtWidgets import QApplication, QComboBox, QFrame, QPushButton, QTableWidget

from cinecalendar.premium_calendar_ui import CalendarPremiumWindow
from cinecalendar.service import CineCalendarService
from cinecalendar.ui_composition import compose_premium_window
from cinecalendar.util import AppPaths, json_dumps, utcnow_iso


def test_large_rating_result_pages_and_romanian_shelf_load_incrementally(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA", "1")
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA_ALLOW_EMPTY", "1")
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "profile"
    paths = AppPaths(root, root / "data", root / "logs", root / "cache", root / "backups")
    for path in (paths.data, paths.logs, paths.cache, paths.backups):
        path.mkdir(parents=True, exist_ok=True)
    service = CineCalendarService(paths)
    now = utcnow_iso()
    with service.db.tx() as con:
        for index in range(225):
            con.execute(
                """INSERT INTO movies(imdb_id,identity_key,title,original_title,year,title_type,
                   genres_json,directors_json,countries_json,keywords_json,semantic_json,
                   source,created_at,updated_at,title_norm,original_title_norm)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"tt{index + 8000000:07d}", f"test:{index}", f"Film {index}",
                 f"Film {index}", 2020, "movie", json_dumps(["Drama"]),
                 json_dumps([]), json_dumps([]), json_dumps([]), json_dumps({}),
                 "test", now, now, f"film {index}", f"film {index}"),
            )
            movie_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            con.execute(
                "INSERT INTO ratings(movie_id,rating,date_rated,source,imported_at,updated_at) VALUES(?,?,?,?,?,?)",
                (movie_id, 10 if index == 224 else 8, "2026-09-01", "test", now, now),
            )

    compose_premium_window(CalendarPremiumWindow)
    window = CalendarPremiumWindow(service)
    try:
        window.resize(1100, 700)
        window.show_page("ratings")
        page = window.stack.currentWidget()
        table = page.findChild(QTableWidget)
        assert table.rowCount() == 100
        next_button = next(b for b in page.findChildren(QPushButton) if b.text() == "Următoare →")
        previous = next(b for b in page.findChildren(QPushButton) if b.text() == "← Anterioare")
        assert next_button.isEnabled() and not previous.isEnabled()
        next_button.click()
        assert table.rowCount() == 100
        next_button.click()
        assert table.rowCount() == 25
        assert table.item(0, 0).text() == "201"
        assert not next_button.isEnabled() and previous.isEnabled()

        rating_filter = next(c for c in page.findChildren(QComboBox)
                             if c.itemText(0) == "Toate notele")
        rating_filter.setCurrentIndex(rating_filter.findData(10))
        assert table.rowCount() == 1
        assert table.item(0, 0).text() == "1"
        assert not next_button.isEnabled() and not previous.isEnabled()
        window.show_page("romanian_list")
        page = window.stack.currentWidget()
        more = next(b for b in page.findChildren(QPushButton) if b.text().startswith("Arată încă"))
        before = len([c for c in page.findChildren(QFrame) if c.objectName() == "PremiumCard"])
        more.click()
        after = len([c for c in page.findChildren(QFrame) if c.objectName() == "PremiumCard"])
        assert after > before
        while buttons := [b for b in page.findChildren(QPushButton)
                         if b.text().startswith("Arată încă") and not b.isHidden()]:
            for button in buttons:
                button.click()
        assert len([c for c in page.findChildren(QFrame) if c.objectName() == "PremiumCard"]) == 233
    finally:
        window.close()
        app.processEvents()
