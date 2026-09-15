from __future__ import annotations


def install_als_ui_patch(window_cls) -> None:
    """Show the real collaborative reason on cards instead of a generic genre sentence."""
    original_human_reason = window_cls.human_reason

    def human_reason(self, rec):
        contributions = getattr(rec.score, "contributions", []) or []
        if any(str(name).startswith("ALS colaborativ MovieLens") for name, _pts, _reason in contributions):
            text = str(getattr(rec.score, "personal_reason", "") or "").strip()
            if text:
                # Cards stay compact; the detail dialog still shows the complete explanation.
                return text if len(text) <= 360 else text[:357].rsplit(" ", 1)[0] + "…"
        return original_human_reason(self, rec)

    window_cls.human_reason = human_reason
