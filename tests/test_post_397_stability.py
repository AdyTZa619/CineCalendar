from __future__ import annotations

from pathlib import Path

from cinecalendar.models import Movie
from cinecalendar.recommendation import romance_policy


ROOT = Path(__file__).resolve().parents[1]


def test_romance_is_never_globally_excluded_or_penalized():
    movie = Movie(id=1, title="Romance Test", genres=["Romance", "Drama"])
    allowed, penalty, reason = romance_policy(movie, True)
    assert allowed is True
    assert penalty == 0.0
    assert reason == ""


def test_service_and_settings_have_no_romance_exclusion_switch():
    service = (ROOT / "cinecalendar" / "service.py").read_text(encoding="utf-8")
    ui = (ROOT / "cinecalendar" / "qt_ui.py").read_text(encoding="utf-8")
    recommendation = (ROOT / "cinecalendar" / "recommendation.py").read_text(encoding="utf-8")

    assert 'set_setting("exclude_romance"' not in service
    assert "Exclude Romance" not in ui
    assert 'exclude_romance = False' in recommendation


def test_today_choice_is_persisted_until_cleared_seen_or_day_changes():
    ui = (ROOT / "cinecalendar" / "qt_ui_v2.py").read_text(encoding="utf-8")
    assert 'decision_chosen_date' in ui
    assert 'decision_chosen_movie_id' in ui
    assert 'ALEGERE FIXATĂ PENTRU AZI' in ui
    assert 'chosen = self._chosen_movie_today()' in ui
    assert 'if kind == "seen"' in ui
    assert 'self._clear_chosen_decision()' in ui


def test_poster_loader_uses_central_queue_not_one_thread_per_card():
    ui = (ROOT / "cinecalendar" / "qt_ui.py").read_text(encoding="utf-8")
    assert "self._poster_queue" in ui
    assert "self._poster_waiters" in ui
    assert "self._poster_limit = 6" in ui
    assert "def _pump_poster_queue" in ui
    assert "if request_id in self._poster_enqueued" in ui
    assert "BoundedSemaphore" not in ui


def test_release_workflow_blocks_on_live_imdb_and_uses_node24_artifact_action():
    workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
    assert "Gate live IMDb public profile sync" in workflow
    gate = workflow.split("Gate live IMDb public profile sync", 1)[1].split("Benchmark 260k catalog engine", 1)[0]
    assert "continue-on-error" not in gate
    assert "attempt -le 3" in gate
    assert "LIVE_IMDB_RATINGS" in gate
    assert "actions/upload-artifact@v7" in workflow


def test_transparency_text_matches_real_updater_state():
    ui = (ROOT / "cinecalendar" / "qt_ui.py").read_text(encoding="utf-8")
    assert "Updaterul Stable este activ" in ui
    assert "Updaterul automat rămâne dezactivat" not in ui
