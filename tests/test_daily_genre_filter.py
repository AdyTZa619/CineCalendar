from __future__ import annotations

from datetime import date, timedelta
import inspect

from cinecalendar.db import Database
from cinecalendar.daily_genre_ui_patch import GENRES, install_daily_genre_ui_patch
from cinecalendar.recommendation import romance_policy
from cinecalendar.recommender_v12 import FastRecommendationEngineV12
from cinecalendar.models import Movie
from cinecalendar import app as app_module
from cinecalendar import service as service_module
from cinecalendar import ui_composition as ui_composition_module


def _movie(genres):
    return Movie(
        id=1, imdb_id="tt0000001", title="Test", original_title="Test", year=2020,
        title_type="movie", runtime_min=100, genres=list(genres), directors=[], countries=[],
        overview="", keywords=[], imdb_rating=7.0, num_votes=1000, release_date=None,
        poster_url=None, source="test", semantic={},
    )


def test_romance_is_not_globally_excluded_when_legacy_switch_is_off():
    allowed, penalty, _reason = romance_policy(_movie(["Romance", "Drama"]), exclude_romance=False)
    assert allowed is True
    assert penalty == 0.0


def test_service_forces_legacy_romance_setting_off():
    source = inspect.getsource(service_module.CineCalendarService._defaults)
    assert 'set_setting("exclude_romance", False)' in source


def test_premium_startup_installs_daily_genre_ui_patch():
    app_source = inspect.getsource(app_module.main)
    assert "compose_premium_window(CalendarPremiumWindow)" in app_source
    composition_source = inspect.getsource(ui_composition_module.compose_premium_window)
    assert "install_daily_genre_ui_patch(window_cls)" in composition_source
    service_source = inspect.getsource(service_module.CineCalendarService.__init__)
    # V16 inherits V12/V13 daily-genre/adaptive behavior, V14 intent, V15 Watch Success/
    # Startability, then adds the final trust gate. The daily genre patch must remain installed.
    assert "FastRecommendationEngineV16" in service_source


def test_genre_ui_is_secondary_not_required_for_default_recommendation():
    source = inspect.getsource(install_daily_genre_ui_patch)
    assert "Nu trebuie să setezi nimic" in source
    assert "Opțional, doar dacă ai chef de ceva anume" in source
    # Automatic loading must be scheduled independently of any genre interaction.
    assert "QTimer.singleShot(0, self._load_today_async)" in source


def test_daily_genre_is_active_only_for_selected_date(tmp_path):
    db = Database(tmp_path / "cinecalendar.db")
    engine = FastRecommendationEngineV12(db)
    today = date(2026, 9, 15)
    db.set_setting("daily_genre_filter", {"date": today.isoformat(), "genre": "Western"})

    assert engine._active_daily_genre(today) == "Western"
    assert engine._active_daily_genre(today + timedelta(days=1)) == ""


def test_genre_match_is_exact_and_romance_is_a_normal_choice():
    assert "Romance" in GENRES
    row = {"genres_json": '["Drama","Romance"]'}
    assert FastRecommendationEngineV12._row_matches_genre(row, "Romance") is True
    assert FastRecommendationEngineV12._row_matches_genre(row, "Drama") is True
    assert FastRecommendationEngineV12._row_matches_genre(row, "War") is False
