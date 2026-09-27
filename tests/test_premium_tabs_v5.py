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
