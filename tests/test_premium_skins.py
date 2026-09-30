from __future__ import annotations

from PySide6.QtCore import QTimer, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QPushButton, QScrollArea, QStackedWidget, QTableWidget
from PySide6.QtGui import QPixmap

from cinecalendar.models import Movie, Recommendation, ScoreBreakdown
from cinecalendar.decision_action_patch import current_today_choice_state
from cinecalendar.premium_calendar_ui import CalendarPremiumWindow
from cinecalendar.premium_skins import HeroCanvas, PosterCanvas, _cached_backdrop_url
from cinecalendar.service import CineCalendarService
from cinecalendar.ui_composition import compose_premium_window
from cinecalendar.util import AppPaths, json_dumps, utcnow_iso


def test_four_skins_keep_navigation_and_real_decision_actions(tmp_path, monkeypatch):
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
        for n in range(6):
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
    # Scheduled catalog/sync maintenance is independent of the skin and can mutate
    # the recommendation signature while this UI test cycles through 70 pages.
    monkeypatch.setattr(CalendarPremiumWindow, "auto_catalog_if_needed", lambda self: None)
    monkeypatch.setattr(CalendarPremiumWindow, "sync_imdb_public", lambda self, **kwargs: None)
    monkeypatch.setattr(CalendarPremiumWindow, "run_metadata_doctor", lambda self, **kwargs: None)
    window = CalendarPremiumWindow(service)
    try:
        window.resize(1280, 720)
        window.show()
        expected_menus = {key for key, _ in window.NAV}
        assert "calendar" in expected_menus
        assert "month" not in expected_menus
        required_actions = (
            "choose_decision", "skip_decision", "contextual_feedback_menu",
            "open_details", "_open_calendar_date", "start_update",
        )
        assert all(callable(getattr(window, name, None)) for name in required_actions)
        overflow_by_skin = {}
        for skin in ("cinematic", "editorial", "poster_wall", "workbench"):
            window.set_skin(skin)
            assert service.db.get_setting("ui_skin") == skin
            assert set(window.nav_buttons) == expected_menus
            assert all(button.text() for button in window.nav_buttons.values())
            window.today_result = (recs[0], recs[1:3])
            window.today_cache_signature = window._today_signature()
            window.today_gallery = recs[3:]
            window.today_gallery_signature = window._today_signature()
            window.show_page("today")
            page = window.stack.currentWidget()
            app.processEvents()
            assert any("Film verificabil 1" in x.text() for x in page.findChildren(QLabel))
            assert any("Aleg pentru azi" in x.text() for x in page.findChildren(QPushButton))
            assert any("Nu acum / motiv" == x.text() for x in page.findChildren(QPushButton))
            assert any("Alt film" == x.text() for x in page.findChildren(QPushButton))
            assert all(not button.icon().isNull() for button in window.nav_buttons.values())

            # Calendar is one shared functional destination in every skin, not a second
            # skin-specific implementation.  Keep it cached here so the parity test does not
            # start recommendation workers while cycling the presentation shells.
            target = window.calendar_selected
            window.calendar_last_result = {
                "date": target, "phase": "test", "events": [], "sections": [],
                "ordinary_day": True, "specific_event_active": False,
            }
            window.calendar_last_signature = (target, window._browse_state_signature())
            window.show_page("calendar")
            calendar_page = window.stack.currentWidget()
            assert calendar_page.findChild(QFrame, "CalendarStage") is not None
            assert calendar_page.findChild(QFrame, "CalendarSpotlight") is not None
            assert any(button.text() == "Repere anuale"
                       for button in calendar_page.findChildren(QPushButton))
            assert "CalendarDateTile" in QApplication.instance().styleSheet()

            # One integrated calendar-relation block only. The removed legacy context patch
            # must not append a detached "De ce acum" column to the root card layout.
            recs[0].score.calendar_kind = "istorică"
            recs[0].score.calendar_reason = "Legătură verificată pentru test."
            calendar_card = window.calendar_movie_card(recs[0])
            assert len(calendar_card.findChildren(QLabel, "CalendarRelationKind")) == 1
            assert any(label.text() == "Legătură verificată pentru test."
                       for label in calendar_card.findChildren(QLabel, "CalendarRelationText"))
            assert not any(label.text().startswith("De ce acum:")
                           for label in calendar_card.findChildren(QLabel))

            window.today_result = (recs[0], recs[1:3])
            window.today_cache_signature = window._today_signature()
            window.today_gallery = recs[3:]
            window.today_gallery_signature = window._today_signature()
            window.show_page("today")
            page = window.stack.currentWidget()
            app.processEvents()
            artwork = (page.findChildren(HeroCanvas) if skin == "cinematic"
                       else page.findChildren(PosterCanvas))
            assert artwork
            assert artwork[0].illustrative and not artwork[0].artwork.isNull()
            if skin == "poster_wall":
                assert sum(x.objectName() == "PosterTile" for x in page.findChildren(QFrame)) == 6
                assert any("Film verificabil 6" == x.text() for x in page.findChildren(QLabel))
            test_image = QPixmap(400, 600)
            test_image.fill("#754b37")
            window._apply_poster_pixmap(artwork[0], test_image)
            assert not artwork[0].illustrative and not artwork[0].artwork.isNull()
            assert not artwork[0].grab().isNull()
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
        assert len(choices) == 4
        choices[1].click()
        assert window.skin == "cinematic" and service.db.get_setting("ui_skin") == "cinematic"
        choices = [button for button in window.stack.currentWidget().findChildren(QPushButton)
                   if button.text() == "Folosește skinul"]
        choices[-1].click()
        assert window.skin == "workbench" and service.db.get_setting("ui_skin") == "workbench"
        window.today_result = (recs[0], recs[1:3])
        window.today_cache_signature = window._today_signature()
        window.today_gallery = recs[3:]
        window.today_gallery_signature = window._today_signature()
        window.show_page("today")
        window.resize(1440, 900)
        app.processEvents()
        window.resize(1280, 720)
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        page = window.stack.currentWidget()
        assert all(scroll.horizontalScrollBar().maximum() == 0
                   for scroll in page.findChildren(QScrollArea))
        selectors = [b for b in page.findChildren(QPushButton) if b.text() == "Selectează"]
        assert len(selectors) == 4
        selectors[1].click()
        inspector = page.findChild(QStackedWidget)
        assert inspector.currentIndex() == 1
        viewed = []
        def close_comparison():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, QDialog)
            table = dialog.findChild(QTableWidget)
            viewed.append(table.rowCount())
            dialog.accept()
        compare = next(b for b in page.findChildren(QPushButton) if "Comparație detaliată" in b.text())
        QTimer.singleShot(0, close_comparison)
        compare.click()
        assert viewed == [4]
        # Gallery failure offers a retry without replacing the already visible decision.
        monkeypatch.setattr(service.recommender, "recommend",
                            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
        monkeypatch.setattr(window, "_ensure_metadata", lambda *args: None)
        window.today_gallery = []
        window.today_gallery_signature = None
        window.today_gallery_failed = False
        window._render_today(*window.today_result)
        assert window.today_result[0].movie.id == recs[0].movie.id
        assert any("Încarc încă un film" in x.text()
                   for x in window.stack.currentWidget().findChildren(QLabel))
        QTest.qWait(300)
        app.processEvents()
        assert window.today_gallery_failed
        retry = next(button for button in window.stack.currentWidget().findChildren(QPushButton)
                     if button.text() == "Reîncearcă doar galeria")
        monkeypatch.setattr(service.recommender, "recommend", lambda *args, **kwargs: recs)
        retry.click()
        assert window.today_result[0].movie.id == recs[0].movie.id
        QTest.qWait(300)
        app.processEvents()
        assert [r.movie.id for r in window.today_gallery] == [r.movie.id for r in recs[3:]]
        assert window.today_gallery_signature == window._today_signature()
        inspector = window.stack.currentWidget().findChild(QStackedWidget)
        assert inspector.count() == 4
        assert inspector.currentIndex() == 1
        decision_before_switch = window.today_result
        window.today_cache_signature = window._today_signature()
        window.set_skin("simple")
        window.show_page("today")
        simple_page = window.stack.currentWidget()
        assert window.today_result is decision_before_switch
        labels = [label.text() for label in simple_page.findChildren(QLabel)]
        assert any("Film verificabil 1" in value for value in labels), labels[:25]
        assert any("DESCHIDE ÎN STREMIO" == button.text()
                   for button in simple_page.findChildren(QPushButton))
        assert not simple_page.findChildren(HeroCanvas)
        window.set_skin("workbench")
        window.show_page("today")
        inspector = window.stack.currentWidget().findChild(QStackedWidget)
        selectors = [b for b in window.stack.currentWidget().findChildren(QPushButton)
                     if b.text() == "Selectează"]
        selectors[1].click()
        choose = next(b for b in inspector.currentWidget().findChildren(QPushButton)
                      if "Aleg pentru azi" in b.text())
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
        reopened.today_result = (recs[0], recs[1:3])
        reopened.today_cache_signature = reopened._today_signature()
        reopened.set_skin("editorial")
        assert reopened.theme == "light"
        reopened.set_skin("simple")
        assert reopened.skin == "simple"
        assert reopened.theme == service.db.get_setting("theme", "dark")
        assert service.db.get_setting("ui_skin") == "simple"
        assert set(reopened.nav_buttons) == expected_menus
        assert all(not button.icon().isNull() for button in reopened.nav_buttons.values())
        # The shared immersive calendar must respect both themes supported by Simple.
        reopened.theme = "light"
        reopened.db.set_setting("theme", "light")
        reopened.apply_theme()
        assert "#F4F2ED" in QApplication.instance().styleSheet()
        assert "CalendarStage" in QApplication.instance().styleSheet()
        reopened.theme = "dark"
        reopened.db.set_setting("theme", "dark")
        reopened.apply_theme()
        assert "#0B0D12" in QApplication.instance().styleSheet()
        assert reopened.centralWidget().findChild(QFrame, "UtilityBar") is None
        reopened.show_page("today")
        simple_page = reopened.stack.currentWidget()
        assert any("Film verificabil 2" in label.text() for label in simple_page.findChildren(QLabel))
        assert any("DESCHIDE ÎN STREMIO" == button.text()
                   for button in simple_page.findChildren(QPushButton))
        assert not simple_page.findChildren(HeroCanvas)
        for key in expected_menus:
            reopened.show_page(key)
            assert reopened.stack.currentWidget() is not None
        reopened.show_page("settings")
        simple_choice = next(button for button in reopened.stack.currentWidget().findChildren(QPushButton)
                             if button.text() == "Activ")
        assert simple_choice.isEnabled()
        with service.db.tx() as con:
            con.execute("UPDATE movies SET tmdb_id=98765 WHERE id=?", (recs[0].movie.id,))
            con.execute("""INSERT INTO metadata_cache(provider,cache_key,payload_json,fetched_at,expires_at)
                           VALUES('tmdb',?,?,?,?)""",
                        ('/movie/98765?{}', '{"backdrop_path":"/scene.jpg"}',
                         stamp, '2099-01-01T00:00:00+00:00'))
        assert _cached_backdrop_url(reopened, recs[0]) == 'https://image.tmdb.org/t/p/w1280/scene.jpg'
    finally:
        reopened.close()
        app.processEvents()

    persisted = CalendarPremiumWindow(service)
    try:
        assert persisted.skin == "simple"
        assert persisted.db.get_setting("ui_skin") == "simple"
    finally:
        persisted.close()
        app.processEvents()


def test_stable_runtime_keeps_v16_and_full_skin_navigation(tmp_path, monkeypatch):
    """Promoting the shared UI must not silently promote the Alpha V5 engine."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("CINECALENDAR_V5_ALPHA", raising=False)
    monkeypatch.delenv("CINECALENDAR_V5_ALPHA_ALLOW_EMPTY", raising=False)

    from cinecalendar.collaborative_als import CollaborativeALSProvider
    from cinecalendar.quality_manager_v47 import RecommendationQualityManagerV47
    from cinecalendar.v5_visible_trial import AlphaTrialRecommender
    from cinecalendar.premium_skins import SKINS

    monkeypatch.setattr(CollaborativeALSProvider, "start_background", lambda self: None)
    monkeypatch.setattr(RecommendationQualityManagerV47, "start_background", lambda self: None)
    monkeypatch.setattr(CalendarPremiumWindow, "auto_catalog_if_needed", lambda self: None)
    monkeypatch.setattr(CalendarPremiumWindow, "sync_imdb_public", lambda self, **kwargs: None)
    monkeypatch.setattr(CalendarPremiumWindow, "run_metadata_doctor", lambda self, **kwargs: None)

    app = QApplication.instance() or QApplication([])
    root = tmp_path / "stable-profile"
    paths = AppPaths(root, root / "data", root / "logs", root / "cache", root / "backups")
    for path in (paths.data, paths.logs, paths.cache, paths.backups):
        path.mkdir(parents=True, exist_ok=True)

    service = CineCalendarService(paths)
    assert service.v5_alpha is False
    assert not isinstance(service.recommender, AlphaTrialRecommender)
    assert not hasattr(service, "alpha_v5_recommender")
    assert not hasattr(service, "alpha_v16_recommender")

    compose_premium_window(CalendarPremiumWindow)
    window = CalendarPremiumWindow(service)
    try:
        stable_menus = {key for key, _label in window.NAV}
        assert "v5_lab" not in stable_menus
        assert "calendar" in stable_menus
        assert "month" not in stable_menus

        for name in (
            "choose_decision", "skip_decision", "contextual_feedback_menu",
            "open_details", "_open_calendar_date", "start_update",
        ):
            assert callable(getattr(window, name, None)), name

        target = window.calendar_selected
        for skin in SKINS:
            window.set_skin(skin)
            assert set(window.nav_buttons) == stable_menus
            assert all(not button.icon().isNull() for button in window.nav_buttons.values())

            window.calendar_last_result = {
                "date": target,
                "phase": "test",
                "events": [],
                "sections": [],
                "ordinary_day": True,
                "specific_event_active": False,
            }
            window.calendar_last_signature = (target, window._browse_state_signature())
            window.show_page("calendar")
            page = window.stack.currentWidget()
            assert page.findChild(QFrame, "CalendarStage") is not None
            assert page.findChild(QFrame, "CalendarSpotlight") is not None
            assert any(button.text() == "Repere anuale"
                       for button in page.findChildren(QPushButton))
            assert all(callable(getattr(window, f"page_{key}", None))
                       for key in stable_menus)
            # Stable must be able to construct every destination under every skin, not merely
            # expose a button for it.
            for key in stable_menus:
                if key == "calendar":
                    continue
                window.show_page(key)
                assert window.stack.currentWidget() is not None
    finally:
        window.close()
        app.processEvents()
