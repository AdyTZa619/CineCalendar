from pathlib import Path


def _source() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "cinecalendar"
        / "premium_ui.py"
    ).read_text(encoding="utf-8")


def test_premium_cards_render_v16_v5_trial_audit_text():
    source = _source()

    assert "owner._trial_audit_text(rec)" in source

    hero = source[source.index("def decision_hero"):source.index("def backup_card")]
    assert "self._trial_audit_text(rec)" in hero

    backup = source[source.index("def backup_card"):source.index("# ---------- premium browse ----------")]
    assert "self._trial_audit_text(rec)" in backup

    compact = source[source.index("def compact_recommendation_card"):source.index("# ---------- taste hub ----------")]
    assert "self._trial_audit_text(rec)" in compact
