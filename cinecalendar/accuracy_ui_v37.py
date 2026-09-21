from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


UI_ACCURACY_VERSION = "accuracy-ui-v4.7.0-live-protected"


def install_accuracy_ui_v37(window_cls) -> None:
    if getattr(window_cls, "_cinecalendar_accuracy_v37_installed", False):
        return

    original_page_shell = window_cls.page_shell

    def page_shell(self, title, subtitle, *args, **kwargs):
        page, content = original_page_shell(self, title, subtitle, *args, **kwargs)
        if str(title) == "Taste Hub":
            try:
                status = self.s.quality_manager.status()
                v37 = status.get("base_v37") or {}
                baseline = str(status.get("baseline_engine") or "V16/V17")
                legacy = str(v37.get("legacy_36_engine") or baseline)
                preferred = str(status.get("preferred_engine") or baseline)
                state = str(status.get("status") or "în așteptare")
                selected = status.get("selected_verdict") or {}
                approved = bool(selected.get("approved"))
                weight = status.get("selected_als_weight")
                windows = int(status.get("window_count", 0) or 0)

                box = QFrame(); box.setObjectName("PremiumCard")
                layout = QVBoxLayout(box); layout.setContentsMargins(20,18,20,18); layout.setSpacing(7)
                heading = QLabel("Accuracy 4.7 • raport personal și protecție pe rezultate reale")
                heading.setObjectName("SectionTitle"); layout.addWidget(heading)

                if approved and weight is not None:
                    verdict = (
                        f"Istoricul tău a aprobat {float(weight)*100:.0f}% ALS / "
                        f"{(1.0-float(weight))*100:.0f}% conținut peste baseline-ul {baseline}; "
                        f"motorul selectat este {preferred}."
                    )
                elif state == "completed":
                    verdict = (
                        f"Nicio pondere testată nu a bătut sigur 70/30 pe istoricul tău; "
                        f"programul păstrează baseline-ul {baseline}."
                    )
                else:
                    verdict = (
                        f"Până termină evaluarea, programul păstrează motorul deja validat: {preferred}."
                    )
                progress = ""
                if state == "running":
                    progress = (
                        f" • progres: {int(status.get('progress_step',0) or 0)}/"
                        f"{int(status.get('progress_total',0) or 0)} • "
                        f"{str(status.get('progress_label') or 'calibrare în curs')}"
                    )
                line = QLabel(f"Stare: {state} • ferestre independente: {windows}{progress} • {verdict}")
                line.setObjectName("Muted"); line.setWordWrap(True); layout.addWidget(line)

                note = QLabel(
                    "4.6 compară 50/50, 60/40 și 80/20 cu 70/30 pe ferestre temporale care nu se suprapun. "
                    "Motorul de bază păstrează calibrarea 3.7 a candidaților locali și baseline-ul anterior "
                    f"({legacy}); o pondere nouă intră în producție numai după ce trece toate gardurile anti-regresie."
                )
                note.setObjectName("Muted"); note.setWordWrap(True); layout.addWidget(note)

                policy = status.get("recalibration_policy") or {}
                changed = int(policy.get("changed_ratings", 0) or 0)
                required = int(policy.get("required_ratings", 0) or 0)
                policy_line = QLabel(
                    f"Recalibrare economică: {changed}/{required} ratinguri noi sau modificate • "
                    "feedbackul temporar nu invalidează backtestul."
                )
                policy_line.setObjectName("Muted"); policy_line.setWordWrap(True); layout.addWidget(policy_line)

                live = status.get("live_guard") or {}
                live_state = str(live.get("status") or "baseline")
                active = live.get("active") or {}
                if live_state == "rolled_back":
                    live_text = "ROLLBACK ACTIV: două semnale reale independente au regresat; motorul sigur este folosit din nou."
                elif live_state == "protected":
                    live_text = "VALIDAT LIVE: formula personală trece verificarea pe alegeri, vizionări și ratinguri reale."
                elif live_state == "collecting":
                    live_text = f"VALIDARE LIVE: se strâng rezultate reale ({int(active.get('rated',0) or 0)} cu rating)."
                else:
                    live_text = "PROTEJAT: motorul sigur rămâne activ până când există o formulă personală aprobată."
                live_line = QLabel(live_text)
                live_line.setObjectName("BodyStrong"); live_line.setWordWrap(True); layout.addWidget(live_line)
                content.addWidget(box)
            except Exception:
                pass
        return page, content

    window_cls.page_shell = page_shell
    window_cls._cinecalendar_accuracy_v37_installed = True
