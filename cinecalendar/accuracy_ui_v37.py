from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


UI_ACCURACY_VERSION = "accuracy-ui-v3.7.0"


def install_accuracy_ui_v37(window_cls) -> None:
    if getattr(window_cls, "_cinecalendar_accuracy_v37_installed", False):
        return

    original_page_shell = window_cls.page_shell

    def page_shell(self, title, subtitle, *args, **kwargs):
        page, content = original_page_shell(self, title, subtitle, *args, **kwargs)
        if str(title) == "Taste Hub":
            try:
                status = self.s.quality_manager.status()
                baseline = str(status.get("baseline_engine") or "V16/V17")
                legacy = str(status.get("legacy_36_engine") or baseline)
                preferred = str(status.get("preferred_engine") or legacy)
                state = str(status.get("status") or "în așteptare")
                selected = status.get("selected_verdict") or {}
                approved = bool(selected.get("approved"))
                share = status.get("selected_share")
                windows = int(status.get("window_count", 0) or 0)

                box = QFrame(); box.setObjectName("PremiumCard")
                layout = QVBoxLayout(box); layout.setContentsMargins(20,18,20,18); layout.setSpacing(7)
                heading = QLabel("Accuracy 3.7 • calibrare personală anti-regresie")
                heading.setObjectName("SectionTitle"); layout.addWidget(heading)

                if approved and share is not None:
                    verdict = (
                        f"A fost aprobat un lane local de {float(share)*100:.0f}% peste {baseline}; "
                        f"motorul selectat este {preferred}."
                    )
                elif state == "completed":
                    verdict = (
                        f"Niciun procent testat nu a bătut sigur {baseline}; programul păstrează baseline-ul validat."
                    )
                else:
                    verdict = (
                        f"Până termină evaluarea 3.7, programul păstrează exact motorul 3.6 curent: {legacy}."
                    )
                line = QLabel(f"Stare: {state} • ferestre independente: {windows} • {verdict}")
                line.setObjectName("Muted"); line.setWordWrap(True); layout.addWidget(line)

                note = QLabel(
                    "3.7 testează separat 8%, 14% și 20% candidați locali pe ferestre temporale care nu se suprapun. "
                    "Fiecare test pornește din motorul tău V16/V17 deja aprobat, fără să introducă automat V17 peste un baseline V16."
                )
                note.setObjectName("Muted"); note.setWordWrap(True); layout.addWidget(note)
                content.addWidget(box)
            except Exception:
                pass
        return page, content

    window_cls.page_shell = page_shell
    window_cls._cinecalendar_accuracy_v37_installed = True
