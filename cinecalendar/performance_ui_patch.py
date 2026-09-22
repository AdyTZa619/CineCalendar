from __future__ import annotations

from pathlib import Path

from .qt_ui import WorkerThread
from .watcher import RatingsFolderWatcher


def install_performance_ui_patch(window_cls) -> None:
    """Patch inherited legacy UI hot spots without duplicating the premium window.

    The original base window was written for a tiny catalog. It performs a ratings-folder
    scan on the GUI thread every 15 seconds and calculates candidate count with a full
    260k-row join. On a portable install stored on a mechanical HDD those operations can
    compete with recommendation work. This patch keeps monitoring best-effort and invisible.
    """
    original_init = window_cls.__init__

    def __init__(self, service):
        self.ratings_watch_worker = None
        self.ratings_watch_failures = 0
        original_init(self, service)
        # A ratings export is a manual, infrequent action. Polling once per minute is enough.
        try:
            self.watch_timer.setInterval(60_000)
        except Exception:
            pass

    def catalog_count(self):
        # ratings.movie_id is UNIQUE + FK to movies, therefore unseen = movies - ratings.
        # Count the two indexed tables independently instead of scanning the full catalog join.
        with self.db.connect() as con:
            row = con.execute(
                "SELECT (SELECT COUNT(*) FROM movies) AS total, "
                "(SELECT COUNT(*) FROM ratings) AS rated"
            ).fetchone()
        total = int(row["total"] or 0)
        rated = int(row["rated"] or 0)
        return total, rated, max(0, total - rated)

    def _foreground_busy(self) -> bool:
        # Do not add HDD work while the user is waiting for a recommendation, calendar program,
        # metadata hydration or a catalog bootstrap. The next timer tick will retry automatically.
        for name in (
            "today_worker",
            "browse_worker",
            "calendar_worker",
            "metadata_worker",
            "worker",
        ):
            obj = getattr(self, name, None)
            try:
                if obj is not None and obj.isRunning():
                    return True
            except RuntimeError:
                continue
        try:
            return bool(self.db.get_setting("catalog_bootstrap_running", False))
        except Exception:
            return False

    def scan_ratings_folder(self, manual: bool = False):
        # QPushButton.clicked(bool) supplies False for a normal button, so detect an explicit
        # user click by sender as well. QTimer.timeout has the watch timer as sender.
        sender = self.sender()
        if sender is not None and sender is not getattr(self, "watch_timer", None):
            manual = True
        manual = bool(manual)

        if not manual and not self.db.get_setting("auto_watch_enabled", False):
            return
        if not manual and _foreground_busy(self):
            return

        worker = getattr(self, "ratings_watch_worker", None)
        if worker is not None and worker.isRunning():
            if manual:
                self.set_status("Scanarea IMDb este deja în curs.", True)
            return

        folder = self.db.get_setting("ratings_folder", str(Path.home() / "Downloads"))
        if manual:
            self.set_status("Scanez folderul pentru un export IMDb nou…", True)

        def fn(_progress):
            # RatingsFolderWatcher rebuilds the profile exactly once when a changed export is
            # imported. Invalid/unrelated CSV files are ignored inside the watcher.
            return RatingsFolderWatcher(self.db, folder).scan()

        worker = WorkerThread(fn, self)
        self.ratings_watch_worker = worker

        def success(results):
            self.ratings_watch_worker = None
            self.ratings_watch_failures = 0
            if not results:
                if manual:
                    self.set_status("Nu am găsit un export IMDb nou și valid în folderul urmărit.", False)
                return
            result = results[0]
            self.refresh_live_recommendation_guard()
            self.set_status(
                f"Export IMDb nou importat: {len(result.new_ratings)} ratinguri noi, "
                f"{len(result.changed_ratings)} modificate.",
                False,
            )
            if self.current_page in {"ratings", "romanian_list"}:
                self.show_page(self.current_page)

        def failure(message):
            self.ratings_watch_worker = None
            self.ratings_watch_failures = int(getattr(self, "ratings_watch_failures", 0)) + 1
            self.s.log.error("ratings watcher failed: %s", message)
            if manual:
                detail = str(message or "eroare necunoscută").strip().replace("\n", " ")[:180]
                self.set_status(f"Scanarea IMDb a eșuat: {detail}", False)
            elif self.ratings_watch_failures >= 3:
                # One transient file/lock error must never leave a scary persistent message in
                # the sidebar. Escalate only after repeated real failures.
                self.set_status(
                    "Monitorul IMDb este temporar indisponibil; importul manual rămâne disponibil.",
                    False,
                )

        worker.success.connect(success)
        worker.failure.connect(failure)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    window_cls.__init__ = __init__
    window_cls.catalog_count = catalog_count
    window_cls.scan_ratings_folder = scan_ratings_folder
