from __future__ import annotations

from datetime import date
import time

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QComboBox, QLabel

from cinecalendar.premium_calendar_ui import CalendarPremiumWindow
from cinecalendar.service import CineCalendarService
from cinecalendar.smart_watchlist import SmartWatchlistResult
from cinecalendar.ui_composition import compose_premium_window
from cinecalendar.util import AppPaths, json_dumps, utcnow_iso


def test_existing_results_open_immediately_without_new_page_workers(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA", "1")
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA_ALLOW_EMPTY", "1")
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "alpha"
    paths = AppPaths(root, root / "data", root / "logs", root / "cache", root / "backups")
    for path in (paths.data, paths.logs, paths.cache, paths.backups):
        path.mkdir(parents=True, exist_ok=True)
    service = CineCalendarService(paths)
    now = utcnow_iso()
    with service.db.tx() as con:
        con.execute(
            """INSERT INTO movies(imdb_id,identity_key,title,original_title,year,title_type,
               genres_json,directors_json,countries_json,keywords_json,semantic_json,
               source,created_at,updated_at,title_norm,original_title_norm)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("tt0000901", "test:movie", "Test", "Test", 2020, "movie",
             json_dumps([]), json_dumps([]), json_dumps([]), json_dumps([]),
             json_dumps({}), "test", now, now, "test", "test"),
        )

    compose_premium_window(CalendarPremiumWindow)
    window = CalendarPremiumWindow(service)
    worker = None
    try:
        window.resize(1100, 700)
        window.show_page("settings")
        app.processEvents()
        romanian_button = window.nav_buttons["romanian"]
        assert romanian_button.width() >= romanian_button.fontMetrics().horizontalAdvance(romanian_button.text()) + 28
        assert window.undo_feedback_button.width() >= window.undo_feedback_button.fontMetrics().horizontalAdvance(window.undo_feedback_button.text()) + 20

        window.today_result = (None, [])
        window.today_cache_signature = window._today_signature()
        window.show_page("today")
        app.processEvents()
        assert window.today_worker is None
        assert window.today_content.count() == 1
        assert "Nu am găsit momentan" in window.today_content.itemAt(0).widget().text()
        assert window.stack.currentWidget().findChildren(QComboBox)
        window.recalculate_today()
        assert window.today_result is None
        assert window.today_cache_signature is None
        assert any("Îți aleg filmul" in x.text() for x in window.stack.currentWidget().findChildren(QLabel))
        window.db.set_setting("daily_genre_filter", {"date": date.today().isoformat(), "genre": "Horror"})
        window.show_page("today")
        assert any(combo.currentText() == "Horror" for combo in window.stack.currentWidget().findChildren(QComboBox))

        signature = window._browse_state_signature()
        window.romanian_result = []
        window.romanian_cache_signature = signature
        window.show_page("romanian")
        assert getattr(window, "romanian_worker", None) is None
        assert not any("Caut filme românești" in x.text() for x in window.findChildren(QLabel))

        window.smart_watchlist_result = SmartWatchlistResult((), 0, 0, 0, 0, 0, 0)
        window.smart_watchlist_cache_signature = window._watchlist_signature()
        window.show_page("watchlist")
        assert getattr(window, "smart_watchlist_worker", None) is None
        assert not any("Ordonez Watchlist" in x.text() for x in window.findChildren(QLabel))

        target = date.today()
        window.calendar_selected = target
        window.calendar_month_anchor = target.replace(day=1)
        window.calendar_last_result = {"date": target, "phase": "test", "events": [], "sections": []}
        window.calendar_last_signature = (target, window._browse_state_signature())
        window.show_page("month")
        assert window.calendar_worker is None
        assert not any("Calculez o singură dată" in x.text() for x in window.findChildren(QLabel))

        # The actual application class opens every sidebar page without a missing builder.
        assert all(callable(getattr(window, f"page_{key}", None)) for key, _ in window.NAV)
        for key, _label in window.NAV:
            window.show_page(key)
            assert window.stack.currentWidget() is not None

        class PageWorker(QThread):
            def run(self):
                while not self.isInterruptionRequested():
                    self.msleep(5)

        worker = PageWorker(window)
        window.romanian_worker = worker
        worker.start()
        for _ in range(100):
            if worker.isRunning():
                break
            time.sleep(.01)
        assert worker.isRunning()
    finally:
        window.close()
        app.processEvents()
    assert worker is not None and worker.wait(1000)
