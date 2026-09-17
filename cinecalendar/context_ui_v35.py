from __future__ import annotations

from datetime import date

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


UI_CONTEXT_VERSION = "context-ui-v3.5.0"


def install_context_ui_v35(window_cls) -> None:
    if getattr(window_cls, "_cinecalendar_context_v35_installed", False):
        return

    original_page_shell = window_cls.page_shell

    def page_shell(self, title, subtitle, *args, **kwargs):
        page, content = original_page_shell(self, title, subtitle, *args, **kwargs)
        if str(title) == "Taste Hub":
            try:
                quality = self.s.quality_manager.status()
                context_status = self.s.recommender.context_status() if hasattr(self.s.recommender, "context_status") else {}
                preferred = str(quality.get("preferred_engine") or type(self.s.recommender).__name__)
                state = str(quality.get("status") or "în așteptare")
                comparison = quality.get("comparison") or {}
                approved = bool((comparison.get("aggregate") or {}).get("approved"))
                verdict = "V17 aprobat de backtest" if approved else "V16 rămâne etalonul sigur"

                box = QFrame()
                box.setObjectName("PremiumCard")
                layout = QVBoxLayout(box)
                layout.setContentsMargins(20, 18, 20, 18)
                layout.setSpacing(7)
                heading = QLabel("Motor recomandări • diagnostic")
                heading.setObjectName("SectionTitle")
                layout.addWidget(heading)
                line = QLabel(
                    f"Verdict local 3.4: {preferred} • stare: {state} • {verdict}. "
                    f"Context 3.5: {context_status.get('calendar_class', type(self.s.calendar).__name__)} "
                    f"+ gardă de gust {context_status.get('predicted_floor', 6.0):.1f}/10 pentru benzile contextuale."
                )
                line.setObjectName("Muted")
                line.setWordWrap(True)
                layout.addWidget(line)
                note = QLabel(
                    "Contextul poate rafina o alegere competitivă, dar nu poate ridica artificial un film "
                    "pe care modelul personal îl estimează clar slab. Ratingurile și backtestul rămân locale."
                )
                note.setObjectName("Muted")
                note.setWordWrap(True)
                layout.addWidget(note)
                content.addWidget(box)
            except Exception:
                # Diagnostics must never prevent Taste Hub from opening.
                pass
        return page, content

    window_cls.page_shell = page_shell

    original_calendar_card = getattr(window_cls, "calendar_movie_card", None)
    if callable(original_calendar_card):
        def calendar_movie_card(self, rec):
            card = original_calendar_card(self, rec)
            try:
                target = getattr(self, "calendar_selected", None) or date.today()
                why = self.s.calendar.why_now(rec.movie, target)
                reason = str(why.get("reason") or "").strip()
                phase = str(why.get("phase") or "").strip()
                if reason:
                    text = "De ce acum: " + reason
                    if phase:
                        text += " • " + phase
                    label = QLabel(text)
                    label.setObjectName("Muted")
                    label.setWordWrap(True)
                    card.layout().addWidget(label)
            except Exception:
                pass
            return card

        window_cls.calendar_movie_card = calendar_movie_card

    window_cls._cinecalendar_context_v35_installed = True
