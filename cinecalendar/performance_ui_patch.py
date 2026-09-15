from __future__ import annotations

from pathlib import Path

from .qt_ui import WorkerThread
from .watcher import RatingsFolderWatcher


def install_performance_ui_patch(window_cls) -> None:
    """Patch inherited legacy UI hot spots without duplicating the premium window.

    The original base window was written for a tiny catalog. It performs a ratings-folder
    scan on the GUI thread every 15 seconds and calculates candidate count with a full
    260k-row join. Those operations are harmless on SSD test runners but visibly stall a
    portable install stored on a mechanical HDD.
    """
    original_init = window_cls.__init__

    def __init__(self, service):
        self.ratings_watch_worker = None
        original_init(self, service)
        # Automatic IMDb detection does not need a 15-second polling cadence. One minute is
        # still effectively immediate for a manually exported ratings.csv and removes a
        # recurring source of UI/disk pressure.
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

    def scan_ratings_folder(self):
        if not self.db.get_setting("auto_watch_enabled", False):
            return
        worker = getattr(self, "ratings_watch_worker", None)
        if worker is not None and worker.isRunning():
            return

        folder = self.db.get_setting("ratings_folder", str(Path.home() / "Downloads"))

        def fn(_progress):
            # RatingsFolderWatcher already rebuilds the profile exactly once when a changed
            # export is imported; the UI callback must not rebuild it again.
            return RatingsFolderWatcher(self.db, folder).scan()

        worker = WorkerThread(fn, self)
        self.ratings_watch_worker = worker

        def success(results):
            self.ratings_watch_worker = None
            if not results:
                return
            r = results[0]
            self.set_status(
                f"Export IMDb nou importat: {len(r.new_ratings)} ratinguri noi, "
                f"{len(r.changed_ratings)} modificate."
            )
            if self.current_page == "ratings":
                self.show_page("ratings")

        def failure(message):
            self.ratings_watch_worker = None
            self.s.log.error("ratings watcher failed: %s", message)
            # Do not interrupt/rebuild the current page. The user can still import manually.
            self.set_status("Monitorizarea IMDb a întâmpinat o eroare.")

        worker.success.connect(success)
        worker.failure.connect(failure)
        worker.start()

    window_cls.__init__ = __init__
    window_cls.catalog_count = catalog_count
    window_cls.scan_ratings_folder = scan_ratings_folder
