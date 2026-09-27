from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_premium_tabs_layer_is_installed_last():
    source = _read("cinecalendar/ui_composition.py")
    assert "install_premium_tabs_v5(window_cls)" in source
    assert source.index("install_learning_insight_ui_v43(window_cls)") < source.index(
        "install_premium_tabs_v5(window_cls)"
    )


def test_sidebar_groups_all_main_surfaces():
    source = _read("cinecalendar/premium_tabs_v5.py")
    for label in ("ACUM", "BIBLIOTECĂ", "CALENDAR", "ISTORIC ȘI DATE", "TEST ȘI SISTEM"):
        assert label in source
    for key in (
        "today", "recommendations", "romanian", "romanian_list", "profile", "ratings",
        "watchlist", "calendar", "month", "history", "metadata_doctor", "v5_lab",
        "updates", "settings",
    ):
        assert f'"{key}"' in source


def test_watchlist_reuses_cached_result_and_has_explicit_recalculate():
    source = _read("cinecalendar/smart_watchlist_ui.py")
    assert '("Recalculează coada", self._recalculate_watchlist, True)' in source
    assert "cached_signature == self._watchlist_signature()" in source
    assert "self.smart_watchlist_signature = self._watchlist_signature()" in source


def test_romanian_recommendations_reuse_cached_result():
    source = _read("cinecalendar/romanian_cinema_ui_patch.py")
    assert '("Recalculează", self._recalculate_romanian, True)' in source
    assert "cached_signature == self._romanian_signature()" in source
    assert "self.romanian_result_signature = self._romanian_signature()" in source


def test_calendar_reuses_last_selected_day_result():
    source = _read("cinecalendar/premium_calendar_ui.py")
    assert 'self.calendar_last_result.get("date") == self.calendar_selected' in source
    assert 'self.calendar_last_result.get("date") == target' in source
    assert "if not cached_day:" in source


def test_v5_lab_has_top_level_model_switch():
    source = _read("cinecalendar/premium_tabs_v5.py")
    assert "MODEL FOLOSIT ACUM" in source
    assert 'QPushButton("Folosește V16")' in source
    assert 'QPushButton("Folosește V5 20%")' in source


def test_grouped_sidebar_shell_is_actually_attached_to_window_class():
    source = _read("cinecalendar/premium_tabs_v5.py")
    assert "def _build_shell(self):" in source
    assert "window_cls._build_shell = _build_shell" in source
    assert source.index("def _build_shell(self):") < source.index("window_cls._build_shell = _build_shell")


def test_runtime_calendar_window_really_uses_the_new_shell_and_wrapped_tabs():
    from cinecalendar.premium_calendar_ui import CalendarPremiumWindow
    from cinecalendar.ui_composition import compose_premium_window

    # Ensure the assertion describes the class that app.py instantiates in production.
    compose_premium_window(CalendarPremiumWindow)

    assert CalendarPremiumWindow._build_shell.__module__ == "cinecalendar.premium_tabs_v5"
    assert CalendarPremiumWindow.page_shell.__module__ == "cinecalendar.premium_tabs_v5"

    for method_name in (
        "page_ratings",
        "page_profile",
        "page_watchlist",
        "page_history",
        "page_metadata_doctor",
        "page_settings",
    ):
        method = getattr(CalendarPremiumWindow, method_name)
        assert method.__module__ == "cinecalendar.premium_tabs_v5", method_name
