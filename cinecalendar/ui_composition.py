from __future__ import annotations


def compose_premium_window(window_cls) -> None:
    """Apply the production UI composition exactly once in one canonical order.

    Older releases spread monkey-patch installation across app.py, so moving one import could
    silently change production behaviour. v3.3 centralized the composition; later versions only
    add diagnostics/information layers before the action and exposure-integrity patches.
    """
    if getattr(window_cls, "_cinecalendar_v33_composed", False):
        return

    from .performance_ui_patch import install_performance_ui_patch
    from .table_theme_patch import install_table_theme_patch
    from .als_ui_patch import install_als_ui_patch
    from .daily_genre_ui_patch import install_daily_genre_ui_patch
    from .romanian_cinema_ui_patch import install_romanian_cinema_ui_patch
    from .romanian_list_ui_patch import install_romanian_list_ui_patch
    from .context_ui_v35 import install_context_ui_v35
    from .accuracy_ui_v37 import install_accuracy_ui_v37
    from .decision_action_patch import install_decision_action_patch
    from .watch_success_ui_patch import install_watch_success_ui_patch
    from .foundation_v33 import install_foundation_v33
    from . import library_ui

    # Information/theme layers first, then action semantics, then immutable exposure handling.
    install_performance_ui_patch(window_cls)
    install_table_theme_patch(window_cls)
    install_als_ui_patch(window_cls)
    install_daily_genre_ui_patch(window_cls)
    install_romanian_cinema_ui_patch(window_cls)
    install_romanian_list_ui_patch(window_cls)
    install_context_ui_v35(window_cls)
    install_accuracy_ui_v37(window_cls)
    install_decision_action_patch(window_cls)
    install_watch_success_ui_patch(window_cls)
    install_foundation_v33(window_cls)

    rating_sort_impl = library_ui._rating_sort_key
    if not getattr(rating_sort_impl, "_cinecalendar_default_mode", False):
        def _rating_sort_key(row, mode="date_desc"):
            return rating_sort_impl(row, mode)
        _rating_sort_key._cinecalendar_default_mode = True
        library_ui._rating_sort_key = _rating_sort_key
    library_ui.install_library_ui(window_cls)

    required = (
        "record_once",
        "choose_decision",
        "skip_decision",
        "watch_now",
        "confirm_playback",
        "mark_watched_from_choice",
    )
    missing = [name for name in required if not callable(getattr(window_cls, name, None))]
    if missing:
        raise RuntimeError("Compoziția UI CineCalendar este incompletă: " + ", ".join(missing))

    window_cls._cinecalendar_v33_composed = True
