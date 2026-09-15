from __future__ import annotations
import sys
import threading

from . import __version__
from .service import CineCalendarService
from .updater import parse_special_startup, write_health_marker
from .updater_v3 import cleanup_update_residue


def main():
    exit_code, post_update = parse_special_startup(sys.argv)
    if exit_code is not None:
        return exit_code

    service = CineCalendarService()
    service.log.info("CineCalendar Premium start")

    # Keep one authoritative package version in inherited/base widgets.
    from . import qt_ui as base_ui
    base_ui.APP_VERSION = __version__

    # Patch the inherited update action before loading Premium UI.
    from . import qt_ui_v2 as decision_ui
    from .update_exit_guard import install_update_exit_guard
    install_update_exit_guard(decision_ui.DecisionWindow)

    from .premium_calendar_ui import CalendarPremiumWindow, run_premium_calendar
    from .performance_ui_patch import install_performance_ui_patch
    from .table_theme_patch import install_table_theme_patch
    from .als_ui_patch import install_als_ui_patch
    from .daily_genre_ui_patch import install_daily_genre_ui_patch
    from . import library_ui

    # Remove inherited small-catalog hot spots before the window is instantiated: the IMDb
    # watcher becomes asynchronous/less frequent and catalog counts avoid a 260k LEFT JOIN.
    install_performance_ui_patch(CalendarPremiumWindow)

    # Premium used a light Windows alternateBase under the dark global text palette, making
    # every second table row appear blank. Theme QTableWidget/QHeaderView explicitly.
    install_table_theme_patch(CalendarPremiumWindow)

    # Cards must describe the real ALS collaborative signal when it was actually used, rather
    # than falling back to a generic genre/director sentence.
    install_als_ui_patch(CalendarPremiumWindow)

    # Genre is an explicit one-day intent (Action, Western, Romance etc.), never a permanent
    # exclusion. The old global Romance switch is hidden and disabled.
    install_daily_genre_ui_patch(CalendarPremiumWindow)

    # The renderer calls the date sort key without an explicit mode; keep a safe default
    # while retaining the same implementation for every other explicit sort mode.
    _rating_sort_impl = library_ui._rating_sort_key

    def _rating_sort_key(row, mode="date_desc"):
        return _rating_sort_impl(row, mode)

    library_ui._rating_sort_key = _rating_sort_key
    library_ui.install_library_ui(CalendarPremiumWindow)

    on_ready = None
    if post_update:
        health_path, expected_version = post_update

        def on_ready():
            write_health_marker(health_path, expected_version)
            service.log.info("Post-update health marker written for %s", expected_version)

            def delayed_cleanup():
                try:
                    cleanup_update_residue(service.paths.root / "updates")
                    service.log.info("Updater residue cleanup completed after %s", expected_version)
                except Exception as exc:
                    service.log.exception("Updater residue cleanup failed: %s", exc)

            timer = threading.Timer(5.0, delayed_cleanup)
            timer.daemon = True
            timer.start()

    return run_premium_calendar(service, on_ready=on_ready)


if __name__ == "__main__":
    raise SystemExit(main())
