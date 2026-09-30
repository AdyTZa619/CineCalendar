from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


UI_CONTEXT_VERSION = "context-ui-v3.5.2"


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
                baseline = str(quality.get("baseline_engine") or preferred)
                state = str(quality.get("status") or "în așteptare")

                box = QFrame()
                box.setObjectName("PremiumCard")
                layout = QVBoxLayout(box)
                layout.setContentsMargins(20, 18, 20, 18)
                layout.setSpacing(7)
                heading = QLabel("Motor recomandări • diagnostic")
                heading.setObjectName("SectionTitle")
                layout.addWidget(heading)
                line = QLabel(
                    f"Motor local selectat: {preferred} • baseline validat: {baseline} • stare: {state}. "
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

    # CalendarPremiumWindow already renders the verified event relation inside each movie card.
    # Do not append a second root-layout "De ce acum" label here: on the horizontal card it
    # becomes a detached right-hand column and duplicates the same evidence.

    window_cls._cinecalendar_context_v35_installed = True
