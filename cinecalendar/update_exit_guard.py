from __future__ import annotations

import os

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from .qt_ui import WorkerThread
from .updater_v4 import stage_and_start_update, update_supported


def install_update_exit_guard(decision_cls) -> None:
    """Guarantee that the old frozen process really exits after updater handoff.

    The real updater helper is created through the Windows CIM/WMI service, so it is no
    longer part of the CineCalendar process tree. Once download, SHA verification and
    staging succeed, the GUI process can hard-exit without killing the helper.
    """

    def start_update(self, info, confirm: bool = True):
        if not update_supported():
            QMessageBox.information(
                self,
                "Actualizări",
                "Updaterul automat funcționează numai din CineCalendar.exe pe Windows.",
            )
            return
        if confirm:
            text = (
                f"Instalez CineCalendar {info.version}?\n\n"
                "Versiunea curentă este păstrată ca backup numai până când noua versiune "
                "pornește și confirmă health-check-ul. După succes, fișierele temporare sunt șterse."
            )
            if QMessageBox.question(
                self,
                "Confirmă actualizarea",
                text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            ) != QMessageBox.Yes:
                return
        if self.update_worker and self.update_worker.isRunning():
            return

        self.set_status(f"Descarc CineCalendar {info.version}…", True)
        worker = WorkerThread(
            lambda progress: stage_and_start_update(info, self.s.paths.root, progress),
            self,
        )
        self.update_worker = worker
        worker.message.connect(lambda m: self.set_status(m, True))

        def success(_req):
            self.set_status("Update verificat. Predau instalarea updaterului independent…", True)
            QTimer.singleShot(500, lambda: os._exit(0))

        def failure(message):
            self.update_worker = None
            self.set_status("Actualizarea a eșuat; versiunea curentă nu a fost înlocuită.", False)
            QMessageBox.critical(self, "Actualizare eșuată", message)

        worker.success.connect(success)
        worker.failure.connect(failure)
        worker.start()

    start_update._cinecalendar_force_exit_guard = True
    decision_cls.start_update = start_update
