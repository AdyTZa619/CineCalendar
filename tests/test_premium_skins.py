from __future__ import annotations

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QStackedWidget

from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.decision_action_patch import current_today_choice_state
from cinecalendar.premium_calendar_ui import CalendarPremiumWindow
from cinecalendar.service import CineCalendarService
from cinecalendar.ui_composition import compose_premium_window
from cinecalendar.util import AppPaths, json_dumps, utcnow_iso


def test_three_skins_keep_navigation_and_real_decision_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA", "1")
    monkeypatch.setenv("CINECALENDAR_V5_ALPHA_ALLOW_EMPTY", "1")
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "skin-profile"
    paths = AppPaths(root, root / "data", root / "logs", root / "cache", root / "backups")
    for path in (paths.data, paths.logs, paths.cache, paths.backups):
        path.mkdir(parents=True, exist_ok=True)
    service = CineCalendarService(paths)
    stamp = utcnow_iso()
    recs = []
    with service.db.tx() as con:
        for n in range(3):
            cursor = con.execute(
                """INSERT INTO movies(imdb_id,identity_key,title,original_title,year,title_type,
                   genres_json,directors_json,countries_json,keywords_json,semantic_json,
                   source,created_at,updated_at,title_norm,original_title_norm)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"tt{n + 7000000:07d}", f"skin:{n}", f"Film verificabil {n+1}",
                 f"Film verificabil {n+1}", 2023, "movie", json_dumps(["Mystery"]),
                 json_dumps([]), json_dumps([]), json_dumps([]), json_dumps({}),
                 "test", stamp, stamp, f"film verificabil {n+1}",
                 f"film verificabil {n+1}"),
            )
            recs.append(Recommendation(
                Movie(id=cursor.lastrowid, title=f"Film verificabil {n+1}",
                      year=2023, genres=["Mystery"]),
                ScoreBreakdown(predicted_rating=7.7, confidence=.52,
                               personal_reason="Se potrivește cu genurile evaluate."),
            ))

    compose_premium_window(CalendarPremiumWindow)
    window = CalendarPremiumWindow(service)
    try:
        window.resize(1280, 720)
        window.show()
        expected_menus = {key for key, _ in window.NAV}
        overflow_by_skin = {}
        for skin in ("cinematic", "editorial", "workbench"):
            window.set_skin(skin)
            assert service.db.get_setting("ui_skin") == skin
            assert set(window.nav_buttons) == expected_menus
            assert all(button.text() for button in window.nav_buttons.values())
            window.today_result = (recs[0], recs[1:])
            window.today_cache_signature = window._today_signature()
            window.show_page("today")
            page = window.stack.currentWidget()
            app.processEvents()
            assert any("Film verificabil 1" in x.text() for x in page.findChildren(QLabel))
            assert any("Aleg pentru azi" == x.text() for x in page.findChildren(QPushButton))
            assert any("Nu acum / motiv" == x.text() for x in page.findChildren(QPushButton))
            assert any("Aleg" == x.text() for x in page.findChildren(QPushButton))
            overflow_by_skin[skin] = [
                (scroll.horizontalScrollBar().maximum(), scroll.viewport().width(),
                 [(item.widget().objectName(), item.widget().sizeHint().width())
                  for item in (scroll.widget().layout().itemAt(i)
                               for i in range(scroll.widget().layout().count()))
                  if item.widget() is not None])
                for scroll in page.findChildren(QScrollArea)
            ]
            assert window.today_worker is None
            window.show_page("settings")
            assert any("Aspectul aplicației" == x.text() for x in window.stack.currentWidget().findChildren(QLabel))
            # Every real production menu must continue to construct after a skin switch.
            for key in expected_menus:
                if key not in {"today", "settings"}:
                    window.show_page(key)
                    assert window.stack.currentWidget() is not None
            window.show_page("settings")
        assert all(not maximum for data in overflow_by_skin.values()
                   for maximum, _viewport, _children in data), overflow_by_skin
        assert window.skin == "workbench"
        # Switching from the actual Settings control must rebuild and persist the shell.
        choices = [button for button in window.stack.currentWidget().findChildren(QPushButton)
                   if button.text() == "Folosește skinul"]
        assert len(choices) == 2
        choices[0].click()
        assert window.skin == "cinematic" and service.db.get_setting("ui_skin") == "cinematic"
        choices = [button for button in window.stack.currentWidget().findChildren(QPushButton)
                   if button.text() == "Folosește skinul"]
        choices[-1].click()
        assert window.skin == "workbench" and service.db.get_setting("ui_skin") == "workbench"
        window.today_result = (recs[0], recs[1:])
        window.today_cache_signature = window._today_signature()
        window.show_page("today")
        window.resize(1440, 900)
        app.processEvents()
        window.resize(1280, 720)
        app.processEvents()
        page = window.stack.currentWidget()
        assert all(scroll.horizontalScrollBar().maximum() == 0
                   for scroll in page.findChildren(QScrollArea))
        selectors = [b for b in page.findChildren(QPushButton) if b.text() == "Selectează"]
        assert len(selectors) == 3
        selectors[1].click()
        inspector = page.findChild(QStackedWidget)
        assert inspector.currentIndex() == 1
        choose = next(b for b in inspector.currentWidget().findChildren(QPushButton)
                      if b.text() == "Aleg pentru azi")
        choose.click()
        choice = current_today_choice_state(service.db)
        assert choice is not None and choice.movie.id == recs[1].movie.id
    finally:
        window.close()
        app.processEvents()

    # A new window uses the persisted selection.
    reopened = CalendarPremiumWindow(service)
    try:
        assert reopened.skin == "workbench"
        assert reopened.db.get_setting("ui_skin") == "workbench"
    finally:
        reopened.close()
        app.processEvents()
