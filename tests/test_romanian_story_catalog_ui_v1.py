from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_FILE = ROOT / "cinecalendar" / "romanian_list_ui_patch.py"


def test_romanian_catalog_ui_exposes_story_period_and_unknown_state():
    source = UI_FILE.read_text(encoding="utf-8")
    assert "Perioada acțiunii" in source
    assert "Luna / anotimpul" in source
    assert "Descriere" in source
    assert "Neconfirmată" in source
    assert "Vezi tot" in source
    assert "Catalog complet" in source


def test_release_year_is_labeled_separately_from_story_period():
    source = UI_FILE.read_text(encoding="utf-8")
    assert 'meta_bits.append(f"Lansare {item.release_year}")' in source
    assert 'period_text = item.period or "Neconfirmată"' in source
    assert 'season_text = item.season or "Neconfirmat"' in source


def test_story_shelf_has_drag_scroll_and_edge_arrows():
    source = UI_FILE.read_text(encoding="utf-8")
    assert "QScroller.grabGesture" in source
    assert 'QPushButton("‹", self)' in source
    assert 'QPushButton("›", self)' in source
    assert "bar.maximum()" in source
    assert "horizontalScrollBar().setValue(self._drag_origin - delta)" in source
    assert "story_season = QLabel" in source
