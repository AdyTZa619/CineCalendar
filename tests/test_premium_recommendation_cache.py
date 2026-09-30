from pathlib import Path
from datetime import date
from types import SimpleNamespace

from cinecalendar.db import Database
from cinecalendar.premium_ui import PremiumDecisionWindow


def _premium_source() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "cinecalendar"
        / "premium_ui.py"
    ).read_text(encoding="utf-8")


def test_recommendations_navigation_reuses_the_visible_list():
    source = _premium_source()
    start = source.index("def page_recommendations")
    end = source.index("def _load_browse_async", start)
    block = source[start:end]

    assert '("Recalculează", self.recalculate_browse, True)' in block
    assert "if self._browse_cache_valid():" in block
    assert "self._render_browse(list(self.browse_result))" in block
    assert "QTimer.singleShot(0,self._load_browse_async)" in block
    assert 'lambda:self.show_page("recommendations")' not in block


def test_recalculate_is_the_explicit_cache_invalidation_path():
    source = _premium_source()
    start = source.index("def recalculate_browse")
    end = source.index("def page_recommendations", start)
    block = source[start:end]

    assert "self.browse_result = []" in block
    assert "self.browse_cache_signature = None" in block
    assert 'self.show_page("recommendations")' in block


def test_browse_cache_tracks_user_state_and_trial_mode_not_background_engine_state():
    source = _premium_source()
    start = source.index("def _browse_state_signature")
    end = source.index("def _browse_cache_valid", start)
    block = source[start:end]

    assert "date.today().isoformat()" in block
    assert "FROM ratings" in block
    assert "FROM feedback" in block
    assert "FROM watchlist" in block
    assert "FROM user_profile" in block
    assert "trial_mode" in block
    assert "self.session_skips" in block
    assert "_state_token" not in block


def test_explicit_recalculate_invalidates_frozen_trial_pair():
    source = _premium_source()
    start = source.index("def recalculate_browse")
    end = source.index("def page_recommendations", start)
    block = source[start:end]

    assert 'invalidate("browse")' in block


def test_browse_cache_changes_with_explicit_daily_genre(tmp_path):
    db = Database(tmp_path / "browse-genre.db")
    fake = SimpleNamespace(
        db=db,
        s=SimpleNamespace(recommender=SimpleNamespace(mode="v16")),
        session_skips=set(),
        _today_genre=lambda: str(
            (db.get_setting("daily_genre_filter", {}) or {}).get("genre") or ""
        ),
    )
    baseline = PremiumDecisionWindow._browse_state_signature(fake)
    db.set_setting("daily_genre_filter", {"date": date.today().isoformat(), "genre": "Western"})
    assert PremiumDecisionWindow._browse_state_signature(fake) != baseline
