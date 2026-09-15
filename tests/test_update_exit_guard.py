from cinecalendar.qt_ui_v2 import DecisionWindow
from cinecalendar.update_exit_guard import install_update_exit_guard


def test_update_exit_guard_replaces_decision_start_update():
    original = DecisionWindow.start_update
    try:
        install_update_exit_guard(DecisionWindow)
        assert DecisionWindow.start_update is not original
        assert getattr(DecisionWindow.start_update, "_cinecalendar_force_exit_guard", False) is True
    finally:
        DecisionWindow.start_update = original
