from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


UI_ACCURACY_VERSION = "accuracy-ui-v3.6.0"


def install_accuracy_ui_v36(window_cls) -> None:
    if getattr(window_cls, "_cinecalendar_accuracy_v36_installed", False):
        return

    original_page_shell = window_cls.page_shell

    def page_shell(self, title, subtitle, *args, **kwargs):
        page, content = original_page_shell(self, title, subtitle, *args, **kwargs)
        if str(title) == "Taste Hub":
            try:
                status = self.s.quality_manager.status()
                baseline = str(status.get("baseline_engine") or "V16/V17")
                preferred = str(status.get("preferred_engine") or baseline)
                state = str(status.get("status") or "în așteptare")
                strict = status.get("strict_verdict") or {}
                approved = bool(strict.get("approved"))

                box = QFrame(); box.setObjectName("PremiumCard")
                layout = QVBoxLayout(box); layout.setContentsMargins(20,18,20,18); layout.setSpacing(7)
                heading = QLabel("Accuracy 3.6 • protecție anti-regresie")
                heading.setObjectName("SectionTitle"); layout.addWidget(heading)
                if approved:
                    verdict = f"V18 a câștigat strict două ferestre temporale și este preferat la următoarea pornire: {preferred}."
                else:
                    verdict = f"Motorul validat rămâne {baseline}. V18 nu intră în producție fără două victorii temporale și toate gardurile stricte."
                line = QLabel(f"Stare: {state} • {verdict}")
                line.setObjectName("Muted"); line.setWordWrap(True); layout.addWidget(line)
                note = QLabel(
                    "3.6 adaugă doar candidați locali susținuți repetat de regizori și combinații țară+gen din ratingurile tale. "
                    "Scorarea, contextul 3.5 și trust gate-urile V16/V17 nu sunt ocolite."
                )
                note.setObjectName("Muted"); note.setWordWrap(True); layout.addWidget(note)
                content.addWidget(box)
            except Exception:
                pass
        return page, content

    window_cls.page_shell = page_shell
    window_cls._cinecalendar_accuracy_v36_installed = True
